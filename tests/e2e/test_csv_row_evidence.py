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
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.artifacts.memory.protocols import MemoryCandidateRequest
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceObservation,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import CSV_ROW_SOURCE_DEFINITION, CSVDocumentInput, CSVRowInput, parse_csv_document


def _rows() -> tuple[CSVRowInput, ...]:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("note", " id ", "empty", "numeric", "flag"))
    writer.writerows(
        (f'Beacon{i:02d} 西安,"机场"\r\n第二行', f" key:{i:02d}/雪 ", "", "001.00", "false") for i in range(12)
    )
    return parse_csv_document(
        CSVDocumentInput(document_id="空港:记录/表", primary_key=" id ", content=output.getvalue().encode("utf-8-sig"))
    )


async def _ingest(database: str) -> str:
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    observations = [project_source_for_transport(registry, await registry.resolve(row)) for row in reversed(_rows())]
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    async with open_builtin_runtime(config) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="CSV rows", summary="Captured evidence", idempotency_key="csv-row-evidence")
        )
        await runtime.ingestion.register(manifest_for_definition(CSV_ROW_SOURCE_DEFINITION))
        for observation in (*observations, observations[0]):
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
    return scope.scope_id


class _RowPipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(
                kind="csv-row",
                text=TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content,
                sources=(source,),
            )
            for source in request.sources
            if isinstance(source, SourceObservation)
        )


def test_csv_rows_survive_process_exit_and_exact_citation_lookup(tmp_path: Path) -> None:
    database = tmp_path / "csv-rows.db"
    submitted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, runpy, sys; "
            "module = runpy.run_path(sys.argv[1]); "
            "print(asyncio.run(module['_ingest'](sys.argv[2])))",
            str(Path(__file__).resolve()),
            str(database),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    scope_id = submitted.stdout.strip()
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))

    async def retrieve() -> None:
        expected = {}
        for row in _rows():
            source = await registry.resolve(row)
            expected[source.name] = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))

        async with open_builtin_runtime(config, candidate_pipeline=_RowPipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            flushed = 0
            for _ in range(13):
                result = await memory.flush()
                if not result.processed:
                    break
                flushed += result.source_count
            assert flushed == 12

        async with open_builtin_contexts(config) as contexts:
            journal = await (await contexts.get(scope_id)).sources.journal.entries()
            assert len(journal) == 12
            for entry in journal:
                assert isinstance(entry.source, SourceObservation)
                evidence = TextEvidence.model_validate(entry.source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
                assert evidence == expected[entry.source_ref.source_id]
                assert list(json.loads(evidence.content)) == ["note", " id ", "empty", "numeric", "flag"]

        async with open_builtin_runtime(config, candidate_pipeline=_RowPipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            for index, row in enumerate(_rows()):
                search = await memory.search(SearchMemoryRequest(query=f"Beacon{index:02d}", mode="fts", limit=20))
                target = await registry.resolve(row)
                found = False
                for hit in search.hits:
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    result = await memory.get(GetMemoryEntryRequest(citation=citation))
                    if any(reference.source_id == target.name for reference in result.entry.sources):
                        assert result.entry.text == expected[target.name].content
                        found = True
                assert found, f"Exact citation for {target.name} was not retrievable"

    asyncio.run(retrieve())
