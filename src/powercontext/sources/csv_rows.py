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

"""Offline CSV records captured as independently addressable evidence."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class CSVDocumentInput(BaseModel):
    """Complete UTF-8 CSV bytes and the exact column identifying each row."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One record with its original column order and unmodified strings."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured row whose evidence does not require the original CSV file."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_id(document_id: str) -> None:
    if not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be non-empty without surrounding whitespace")  # noqa: TRY003


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Encode an unambiguous row identity within the Source ID length limit."""

    _validate_document_id(document_id)
    if not primary_key_value.strip():
        raise ValueError("primary key values must not be blank")  # noqa: TRY003
    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def _primary_key_index(columns: tuple[str, ...], primary_key: str) -> int:
    if not columns or any(not column.strip() for column in columns):
        raise ValueError("CSV header must contain non-blank column names")  # noqa: TRY003
    if len(set(columns)) != len(columns):
        raise ValueError("CSV column names must be unique")  # noqa: TRY003
    if not primary_key or primary_key not in columns:
        raise ValueError("primary_key must exactly match a CSV column name")  # noqa: TRY003
    return columns.index(primary_key)


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Validate the entire document before returning any captured rows."""

    _validate_document_id(value.document_id)
    # newline="" retains CR, LF and CRLF inside quoted fields verbatim.
    with io.StringIO(value.content.decode("utf-8-sig"), newline="") as stream:
        reader = csv.reader(
            stream, delimiter=",", quotechar='"', doublequote=True, escapechar=None, skipinitialspace=False, strict=True
        )
        try:
            columns = tuple(next(reader, ()))
            key_index = _primary_key_index(columns, value.primary_key)
            rows: list[CSVRowInput] = []
            seen_keys: set[str] = set()
            for fields in reader:
                # An empty physical record is [], whereas a quoted empty field is [""].
                if not fields:
                    continue
                if len(fields) != len(columns):
                    raise ValueError("CSV record width must match the header")  # noqa: TRY003
                key = fields[key_index]
                csv_row_source_id(value.document_id, key)
                if key in seen_keys:
                    raise ValueError("CSV primary key values must be unique within the document")  # noqa: TRY003
                seen_keys.add(key)
                rows.append(
                    CSVRowInput(
                        document_id=value.document_id,
                        primary_key=value.primary_key,
                        primary_key_value=key,
                        columns=columns,
                        values=tuple(fields),
                    )
                )
        except csv.Error as error:
            raise ValueError(f"Malformed CSV: {error}") from error  # noqa: TRY003
    return tuple(rows)


class _CSVRowTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[CSVRowSource] = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type="csv-row",
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


class _CSVRowSourceDefinition:
    input_class: type[CSVRowInput] = CSVRowInput
    name = "csv-row"
    version = "1"
    projections: tuple[SourceProjection[CSVRowSource], ...] = (_CSVRowTextEvidenceProjection(),)
    source_class: type[CSVRowSource] = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        key_index = _primary_key_index(value.columns, value.primary_key)
        if len(value.values) != len(value.columns):
            raise ValueError("CSV record width must match the header")  # noqa: TRY003
        if value.values[key_index] != value.primary_key_value:
            raise ValueError("primary_key_value must match the captured record")  # noqa: TRY003
        identity = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=identity,
            definition_version=self.version,
            source_id=identity,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = _CSVRowSourceDefinition()

__all__ = [
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
