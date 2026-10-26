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

from __future__ import annotations

import asyncio
import hashlib
import io
import re

import pytest
from jsonschema import Draft202012Validator
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    PDFPageSource,
    parse_pdf_document,
)


def make_pdf(
    texts: tuple[str | None, ...], *, password: str | None = None, corrupt_last: bool = False, title: str = "Report"
) -> bytes:
    """Generate small independent text fixtures with physical blank pages."""

    writer = PdfWriter()
    writer.add_metadata({"/Title": title})
    for index, text in enumerate(texts):
        page = writer.add_blank_page(width=600, height=800)
        if text is None:
            continue
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
        })
        lines = []
        for line in text.splitlines():
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"({escaped}) Tj T*")
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 12 Tf 14 TL 40 750 Td " + " ".join(lines) + " ET").encode("latin-1"))
        if corrupt_last and index == len(texts) - 1:
            stream.set_data(b"BT /F1 12 Tf (unterminated")
        page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(user_password=password, owner_password="owner-password")  # noqa: S106
    with io.BytesIO() as output:
        writer.write(output)
        return output.getvalue()


def test_every_physical_page_keeps_text_numbers_and_blank_page_boundaries() -> None:
    content = make_pdf((
        "Printed page 99\nRevenue 1,234.50\nOperating income (12.75)",
        None,
        "Printed page 101\nCash 007\nDebt 8,001",
    ))
    rows = parse_pdf_document(PDFDocumentInput(document_id=" Report ID ", content=content))
    assert isinstance(rows, tuple) and len(rows) == 3
    assert [row.page_number for row in rows] == [1, 2, 3]
    assert rows[1].text == ""
    assert re.findall(r"[a-z]+|\d+", rows[0].text.lower()) == [
        "printed",
        "page",
        "99",
        "revenue",
        "1",
        "234",
        "50",
        "operating",
        "income",
        "12",
        "75",
    ]
    assert "8,001" in rows[2].text and "1,234.50" not in rows[2].text
    assert all(row.document_id == " Report ID " for row in rows)
    assert all(row.document_sha256 == hashlib.sha256(content).hexdigest() for row in rows)


@pytest.mark.parametrize("document_id", ["", " ", "\t\r\n", "\u2003"])
def test_blank_document_ids_are_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=make_pdf((None,))))


@pytest.mark.parametrize("content", [b"", b"random bytes", b"%PDF-1.7\n", b"\x00\xff", b"%PDF-1.7\n%%EOF"])
def test_invalid_pdf_bytes_fail_with_value_error(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="document", content=content))


def test_damaged_later_page_fails_without_partial_results() -> None:
    content = make_pdf(("Valid first page", "Broken second page"), corrupt_last=True)
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="damaged", content=content))


@pytest.mark.parametrize("password", ["", "required-password"])
def test_permission_flags_only_accept_a_document_readable_without_a_password(password: str) -> None:
    value = PDFDocumentInput(
        document_id="permissions", content=make_pdf(("Permission protected text", None), password=password)
    )
    if password:
        with pytest.raises(ValueError):
            parse_pdf_document(value)
    else:
        pages = parse_pdf_document(value)
        assert len(pages) == 2 and pages[1].text == ""
        assert "Permission protected text" in pages[0].text


def test_revision_identity_uses_full_pdf_bytes_and_retains_original_unicode_text() -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    original = make_pdf(("Original report",), title="Original")
    revised = make_pdf(("Original report",), title="Metadata revision")
    first = parse_pdf_document(PDFDocumentInput(document_id="报告:2025", content=original))[0]
    second = parse_pdf_document(PDFDocumentInput(document_id="报告:2025", content=revised))[0]
    assert first.text == second.text
    source = asyncio.run(registry.resolve(first))
    assert source.name != asyncio.run(registry.resolve(second)).name
    assert source.name == asyncio.run(registry.resolve(first)).name
    assert len(source.name) <= 256
    value = first.model_copy(update={"document_id": "文档:" * 500, "text": "资产 001.50\n负债 空字段"})
    source = asyncio.run(registry.resolve(value))
    assert isinstance(source, PDFPageSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert len(source.name) <= 256
    restored = PDFPageSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == value
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.content == f"{value.document_id}\n{value.text}"
    assert evidence.source_type == "pdf-page" and evidence.source_id == source.name
    assert evidence.metadata == {
        "document_id": value.document_id,
        "document_sha256": value.document_sha256,
        "page_number": 1,
    }
    manifest = manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION)
    assert (manifest.name, manifest.version) == ("pdf-page", "1")
    assert PDF_PAGE_SOURCE_DEFINITION.input_class is PDFPageInput
    observation = project_source_for_transport(registry, source)
    Draft202012Validator(manifest.source_schema).validate(observation.payload)
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == evidence.model_dump(mode="json")


@pytest.mark.parametrize(("digest", "page_number"), [("bad", 1), ("A" * 64, 1), ("0" * 64, 0), ("0" * 64, -1)])
def test_direct_page_resolution_validates_revision_and_physical_page(digest: str, page_number: int) -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    with pytest.raises(ValueError):
        asyncio.run(
            registry.resolve(
                PDFPageInput(document_id="doc", document_sha256=digest, page_number=page_number, text="text")
            )
        )
