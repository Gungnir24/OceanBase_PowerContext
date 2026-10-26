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
import json

import pytest
from jsonschema import Draft202012Validator

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbf",
        b"\n",
        b"\nid,name\n1,one\n",
        b'""\n',
        b"id,\t\n1,one\n",
        b"id,name,name\n1,one,two\n",
        b'id,name\n1,good\n2,"closed"junk\n',
        b'id,name\n1,good\n2,"unfinished',
        b"id,name\n1,good\n2,extra,field\n",
        b"id,name\n1,good\n2\n",
        b"id,name\n1,good\n \t ,blank\n",
        b'id\n1\n""\n',
        b"id\n1\n \n",
        b"id,name\n1,good\n1,duplicate\n",
        b"id,name\n1,good\n2,\xff\n",
        b"id,name\n1,good\n2,\xed\xa0\x80\n",
        b"id,name\n1,good\n" + b"x" * 251 + b",long-key\n",
    ],
)
def test_invalid_documents_fail_atomically(content: bytes) -> None:
    document = CSVDocumentInput(document_id="d", primary_key="id", content=content)
    with pytest.raises(ValueError):
        parse_csv_document(document)


@pytest.mark.parametrize("document_id", ["", " ", " d", "d\t", "\u2003d", "d\n"])
def test_invalid_document_identity_is_rejected_even_without_rows(document_id: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key="id", content=b"id\n"))


@pytest.mark.parametrize("primary_key", ["", " ", "id", " ID "])
def test_primary_key_must_match_the_untrimmed_header(primary_key: str) -> None:
    with pytest.raises(ValueError):
        parse_csv_document(CSVDocumentInput(document_id="d", primary_key=primary_key, content=b" id ,name\n"))


def test_blank_records_and_distinct_untrimmed_headers_and_keys() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="d",
            primary_key=" id ",
            content=b'\xef\xbb\xbfid, id ,name\r\n\r\na, k ,\r\nb,k,""\n\nc,k ,third\r\n',
        )
    )
    assert isinstance(rows, tuple)
    assert tuple(row.primary_key_value for row in rows) == (" k ", "k", "k ")
    assert rows[0].columns == ("id", " id ", "name")
    assert rows[0].values == ("a", " k ", "")
    assert rows[1].values == ("b", "k", "")


def test_csv_reader_semantics_preserve_embedded_newlines_quotes_and_backslashes() -> None:
    rows = parse_csv_document(
        CSVDocumentInput(
            document_id="d",
            primary_key="id",
            content='id,note\r\n1,"a\r\nb\nc\rd\u2028e"\r\n2,a"b\\c\r\n3,"""quoted"",comma"\r\n'.encode(),
        )
    )
    assert tuple(row.values[1] for row in rows) == ("a\r\nb\nc\rd\u2028e", 'a"b\\c', '"quoted",comma')


def test_source_identity_encodes_unicode_delimiters_and_literal_percent_signs() -> None:
    assert csv_row_source_id("表:一", " /%+?雪 ") == "csv:%E8%A1%A8%3A%E4%B8%80:%20%2F%25%2B%3F%E9%9B%AA%20"
    assert csv_row_source_id("a:b", "c") != csv_row_source_id("a", "b:c")
    assert csv_row_source_id("d", "/") != csv_row_source_id("d", "%2F")
    assert csv_row_source_id("d", "~_-.09AZaz") == "csv:d:~_-.09AZaz"


@pytest.mark.parametrize("key", ["x" * 250, "雪" * 27 + "x" * 7])
def test_identity_limit_is_applied_after_percent_encoding(key: str) -> None:
    assert len(csv_row_source_id("d", key)) == 256
    with pytest.raises(ValueError):
        csv_row_source_id("d", key + "x")


@pytest.mark.parametrize("key", ["", " ", "\t\r\n", "\u2003"])
def test_blank_identity_keys_are_rejected(key: str) -> None:
    with pytest.raises(ValueError):
        csv_row_source_id("d", key)


def test_definition_read_projection_and_transport_preserve_the_complete_row() -> None:
    value = CSVRowInput(
        document_id="表",
        primary_key=" id ",
        primary_key_value=" 001 ",
        columns=("note", " id ", "empty", "flag", "number", "date"),
        values=("西安\n机场", " 001 ", "", "false", "001.00", "2026-01-01"),
    )
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(value))
    assert isinstance(source, CSVRowSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert source.name == source.source_id == "csv:%E8%A1%A8:%20001%20"
    restored = CSVRowSource.model_validate_json(source.model_dump_json())
    assert asyncio.run(registry.read(restored)) == value
    evidence = TextEvidence.model_validate(registry.project(restored, TEXT_EVIDENCE_PROJECTION_KEY))
    expected = {
        "note": "西安\n机场",
        " id ": " 001 ",
        "empty": "",
        "flag": "false",
        "number": "001.00",
        "date": "2026-01-01",
    }
    assert evidence.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    assert list(json.loads(evidence.content)) == list(value.columns)
    assert evidence.metadata == {
        "document_id": value.document_id,
        "primary_key": value.primary_key,
        "primary_key_value": value.primary_key_value,
        "columns": list(value.columns),
        "row": expected,
    }
    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    assert (manifest.name, manifest.version) == ("csv-row", "1")
    assert CSV_ROW_SOURCE_DEFINITION.input_class is CSVRowInput
    assert CSV_ROW_SOURCE_DEFINITION.source_class is CSVRowSource
    observation = project_source_for_transport(registry, source)
    Draft202012Validator(manifest.source_schema).validate(observation.payload)
    assert observation.projection(TEXT_EVIDENCE_PROJECTION_KEY) == evidence.model_dump(mode="json")


@pytest.mark.parametrize(
    ("columns", "values", "key"),
    [
        (("id", "name"), ("1",), "1"),
        (("id", "name"), ("1", "name"), "2"),
        (("id", "id"), ("1", "1"), "1"),
        ((" id ",), ("1",), "1"),
    ],
)
def test_direct_row_resolution_rejects_inconsistent_records(
    columns: tuple[str, ...], values: tuple[str, ...], key: str
) -> None:
    row = CSVRowInput(document_id="d", primary_key="id", primary_key_value=key, columns=columns, values=values)
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    with pytest.raises(ValueError):
        asyncio.run(registry.resolve(row))
