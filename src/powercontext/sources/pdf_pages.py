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

"""Complete offline PDF extraction and revision-specific page evidence."""

from __future__ import annotations

import hashlib
import io
import json
import re

from pydantic import BaseModel
from pypdf import PdfReader

from powercontext.sources._pdf_search import search_pdf_pages
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PDFDocumentInput(BaseModel):
    """A complete original PDF supplied without external file access."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """All extractable text on one physical page of one PDF revision."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured page that keeps its original document revision and location."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


def _validate_document_id(document_id: str) -> None:
    if not document_id.strip():
        raise ValueError("PDF document_id must not be blank")  # noqa: TRY003


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page or fail without returning partial pages."""

    _validate_document_id(value.document_id)
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        with io.BytesIO(value.content) as stream:
            reader = PdfReader(stream, strict=True)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ValueError("The PDF cannot be opened with an empty password")  # noqa: TRY003, TRY301
            pages = tuple(
                PDFPageInput(
                    document_id=value.document_id,
                    document_sha256=digest,
                    page_number=index,
                    text=page.extract_text(extraction_mode="plain") or "",
                )
                for index, page in enumerate(reader.pages, start=1)
            )
    except Exception as error:
        # pypdf can report corrupt objects through several parser/codec exception types.
        raise ValueError("The complete PDF could not be parsed") from error  # noqa: TRY003
    return pages


def _page_identity(value: PDFPageInput) -> str:
    _validate_document_id(value.document_id)
    if re.fullmatch(r"[0-9a-f]{64}", value.document_sha256) is None or value.page_number < 1:
        raise ValueError("PDF pages require a lowercase SHA-256 digest and positive physical page number")  # noqa: TRY003
    components = json.dumps(
        (value.document_id, value.document_sha256, value.page_number), ensure_ascii=False, separators=(",", ":")
    )
    return "pdf:" + hashlib.sha256(components.encode("utf-8")).hexdigest()


class _PDFPageTextProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PDFPageSource] = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class _PDFPageDefinition:
    name = "pdf-page"
    version = "1"
    input_class: type[PDFPageInput] = PDFPageInput
    source_class: type[PDFPageSource] = PDFPageSource
    projections: tuple[SourceProjection[PDFPageSource], ...] = (_PDFPageTextProjection(),)

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = _page_identity(value)
        return PDFPageSource(name=identity, source_id=identity, definition_version=self.version, **value.model_dump())

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = _PDFPageDefinition()

__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "search_pdf_pages",
]
