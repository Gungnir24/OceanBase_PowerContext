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
from pathlib import Path

from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, SubmitSourceObservation, open_builtin_runtime
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceDefinitionRegistry, manifest_for_definition, project_source_for_transport
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from tests.e2e.test_pdf_page_retrieval import _DuplicatePagePipeline, _flush, _locations
from tests.test_pdf_pages import make_pdf


def test_company_spacing_and_plural_statements_keep_exact_document_and_primary_pages(tmp_path: Path) -> None:
    texts = (
        "Consolidated Statements of Operations and Comprehensive Income\n2024 2023\nRevenue 500 400\nOperating income 70 60\nNet income 40 30",
        "Consolidated Balance Sheets\n2024 2023\nCurrent assets 80 70\nCurrent liabilities 40 35",
        "Consolidated Statements of Cash Flows\n2024 2023\nCash from operating activities 70 60\nInvesting activities 20 10",
        "Index to Consolidated Financial Statements\nConsolidated Statements of Operations and Comprehensive Income\nConsolidated Balance Sheets\nConsolidated Statements of Cash Flows",
        *(
            f"Management commentary {number}\n"
            + "North Star Holdings restructuring costs are directly outlined in the income statements. " * 10
            for number in range(25)
        ),
    )
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'companies.db'}"))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    sources: dict[str, PDFPageInput] = {}

    async def scenario() -> None:
        async with open_builtin_runtime(config, candidate_pipeline=_DuplicatePagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = (
                await runtime.scopes.create(
                    ScopeDraft(title="Companies", summary="Shared names", idempotency_key="companies")
                )
            ).scope_id
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for document in ("NORTHSTARHOLDINGS_2024_10K", "HOLDINGS_2024_10K", "STAR_2024_10K"):
                pages = parse_pdf_document(PDFDocumentInput(document_id=document, content=make_pdf(texts)))
                for page in pages:
                    source = await registry.resolve(page)
                    sources[source.name] = page
                    await runtime.ingestion.submit(
                        SubmitSourceObservation(
                            scope_id=scope, observation=project_source_for_transport(registry, source)
                        )
                    )
            await _flush(runtime.memory.for_scope(scope), 3 * len(texts))
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope)
            cases = (
                ("What restructuring costs are directly outlined in North Star Holdings income statements?", 1),
                ("Using North-Star Holdings cash flows, what was cash used for investing?", 3),
                ("What is the current ratio for NorthStar Holdings?", 2),
            )
            for question, expected in cases:
                citations = await search_pdf_pages(memory, question, limit=1)
                assert len(citations) == 1
                locations = await _locations(memory, citations, sources)
                assert {(document, page) for document, _, page in locations} == {
                    ("NORTHSTARHOLDINGS_2024_10K", expected)
                }

    asyncio.run(scenario())
