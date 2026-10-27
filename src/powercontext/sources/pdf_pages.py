# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Complete PDF pages as immutable captured evidence and scoped retrieval."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import zlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pypdf import PageObject, PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, NullObject, StreamObject
from typing_extensions import override

from powercontext.artifacts import MemoryCitation
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication


class PDFDocumentInput(BaseModel):
    """An entire original PDF and its caller-owned document identifier."""

    document_id: str = Field(strict=True)
    content: bytes = Field(strict=True)

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("PDF document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """One complete physical page, including pages with no extractable text."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(strict=True)
    document_sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    page_number: int = Field(strict=True, ge=1)
    text: str = Field(strict=True)

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        return PDFDocumentInput.require_document_id(value)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page atomically; unreadable PDFs raise ValueError."""

    digest = hashlib.sha256(value.content).hexdigest()
    try:
        texts = _extract_page_texts(value.content)
    except Exception as error:
        # pypdf can surface malformed streams/fonts/page trees as several different
        # exception types. Nothing escapes this boundary as a partially parsed tuple.
        raise ValueError("Unable to parse the complete PDF document") from error  # noqa: TRY003
    return tuple(
        PDFPageInput(document_id=value.document_id, document_sha256=digest, page_number=index, text=text)
        for index, text in enumerate(texts, start=1)
    )


def _extract_page_texts(content: bytes) -> tuple[str, ...]:
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("PDF requires a password")  # noqa: TRY003
    check = _ExtractionCheck()
    logger = logging.getLogger("pypdf")
    logger.addHandler(check)
    try:
        texts = tuple(_extract_page_text(page, check) for page in reader.pages)
    finally:
        logger.removeHandler(check)
    if check.failed:
        raise ValueError("PDF contains an unreadable text or form stream")  # noqa: TRY003
    page_tree = reader.root_object["/Pages"]
    if not isinstance(page_tree, DictionaryObject) or not texts or len(texts) != page_tree.get("/Count"):
        raise ValueError("PDF page tree count does not match its complete pages")  # noqa: TRY003
    return texts


_TEXT_OPERANDS = {
    b"BT": 0,
    b"ET": 0,
    b"Tm": 6,
    b"cm": 6,
    b"Tf": 2,
    b"Td": 2,
    b"TD": 2,
    b"Tc": 1,
    b"Tw": 1,
    b"Tz": 1,
    b"TL": 1,
    b"Tr": 1,
    b"Ts": 1,
    b"T*": 0,
    b"Tj": 1,
    b"TJ": 1,
    b"'": 1,
    b'"': 3,
    b"Do": 1,
}


class _ExtractionCheck(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.thread_id = threading.get_ident()
        self.failed = False

    @override
    def emit(self, record: logging.LogRecord) -> None:
        # pypdf logs and suppresses exceptions inside Form XObjects, even in strict mode.
        if record.thread == self.thread_id and record.getMessage().startswith("Impossible to decode XFormObject"):
            self.failed = True

    def visit(self, operator: bytes, operands: Sequence[object], *_: object) -> None:
        expected = _TEXT_OPERANDS.get(operator)
        if expected is not None and len(operands) != expected:
            self.failed = True


def _extract_page_text(page: PageObject, check: _ExtractionCheck) -> str:
    contents = page.get("/Contents")
    if contents is not None:
        contents = contents.get_object()
        streams = contents if isinstance(contents, ArrayObject) else (contents,)
        for stream in streams:
            _validate_content_stream(stream.get_object())
    resources = [page.get("/Resources")]
    visited = set()
    while resources:
        resource = resources.pop()
        if resource is None:
            continue
        resource = resource.get_object()
        if id(resource) in visited:
            continue
        visited.add(id(resource))
        xobjects = resource.get("/XObject")
        if xobjects is None:
            continue
        for reference in xobjects.get_object().values():
            form = reference.get_object()
            if form.get("/Subtype") == "/Form":
                _validate_content_stream(form)
                resources.append(form.get("/Resources"))
    return page.extract_text(visitor_operand_before=check.visit) or ""


def _validate_content_stream(stream: object) -> None:
    if isinstance(stream, NullObject):
        return
    if not isinstance(stream, StreamObject):
        raise TypeError("PDF page has an invalid content stream")  # noqa: TRY003
    filters = stream.get("/Filter")
    filters = list(filters) if isinstance(filters, ArrayObject) else [filters]
    if filters in (["/FlateDecode"], ["/Fl"]):
        # pypdf's tolerant Flate decoder can return empty bytes after a corrupt
        # stream. Validate its checksum rather than silently calling the page blank.
        zlib.decompress(stream._data)  # Encoded bytes have no public accessor.


def _page_identity(page: PDFPageInput) -> str:
    encoded = json.dumps(
        [page.document_id, page.document_sha256, page.page_number], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return "pdf:" + hashlib.sha256(encoded).hexdigest()


class PDFPageSource(Source):
    """Captured page whose identity changes when any original PDF byte changes."""

    model_config = ConfigDict(frozen=True)

    # Pydantic's frozen model forbids writes to these narrowed fields at runtime.
    definition_version: Literal["1"] = "1"  # ty: ignore[invalid-mutable-override]
    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED  # ty: ignore[invalid-mutable-override]
    page: PDFPageInput

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.name != _page_identity(self.page):
            raise ValueError("PDF page Source name does not match its exact page identity")  # noqa: TRY003
        return self


class _PDFPageAdapter:
    name = "pdf-page"
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(name=_page_identity(value), page=value)

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = cast(
    SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput],
    AdapterSourceDefinition(_PDFPageAdapter(), projections=(_PDFPageTextProjection(),)),
)


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Rank current, captured PDF pages from this Memory scope without model calls."""

    from powercontext.sources.pdf_search import search_pages

    if type(limit) is not int or limit < 0:
        raise ValueError("PDF search limit must be a nonnegative integer")  # noqa: TRY003
    if limit == 0:
        return ()
    if isinstance(question, str) and not question.strip():
        return ()
    return await search_pages(memory, question, limit)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]
