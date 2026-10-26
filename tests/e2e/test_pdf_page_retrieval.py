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

import pytest

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.errors import InvalidMemoryCandidateError
from powercontext.builtin.artifacts.memory.protocols import MemoryCandidateRequest
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    RememberMemoryRequest,
    ScopedMemoryApplication,
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
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)
from tests.test_pdf_pages import make_pdf


class _DuplicatePagePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            for kind in ("page-a", "page-b"):
                entries.append(MemoryEntryInput(kind=kind, text=evidence.content, sources=(source,)))
        return tuple(entries)


async def _flush(memory: ScopedMemoryApplication, expected: int) -> None:
    processed = 0
    for _ in range(expected + 1):
        result = await memory.flush()
        if not result.processed:
            break
        processed += result.source_count
    assert processed == expected


async def _locations(
    memory: ScopedMemoryApplication, citations: tuple[MemoryCitation, ...], sources: dict[str, PDFPageInput]
) -> set[tuple[str, str, int]]:
    locations = set()
    for citation in citations:
        entry = await memory.get(GetMemoryEntryRequest(citation=citation))
        assert len(entry.entry.sources) == 1
        reference = entry.entry.sources[0]
        assert reference.source_type == "pdf-page"
        page = sources[reference.source_id]
        assert entry.entry.text == f"{page.document_id}\n{page.text}".strip()
        locations.add((page.document_id, page.document_sha256, page.page_number))
    assert len(locations) == len(citations)
    return locations


def test_generic_financial_queries_deduplicate_pages_keep_scope_and_preserve_old_revisions(tmp_path: Path) -> None:
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    texts = (
        "Forward-looking statements\n"
        + "Future performance is uncertain.\n" * 12
        + "Item 1. Business\nThe business operates in several countries and international regions.\nNorth American and European segments.",
        "Legal proceedings\nCivil litigation includes ongoing claims and lawsuits with potential settlements.",
        "Consolidated Statements of Operations\n2024 2023\nNet sales 135 120\nOperating income 29 23\nNet earnings 18 16",
        "Consolidated Statements of Cash Flows\n2024 2023\nDepreciation and amortization 12 10\nCapital expenditures 9 7\nCash from operating activities 21 20",
        "Consolidated Balance Sheets\n2024 2023\nCurrent assets 80 70\nCurrent liabilities 40 35\nCash and equivalents 20 16",
        "Management commentary\nEBITDA and capex are discussed as management metrics. EBITDA capex. No statement data.",
    )
    texts += tuple(
        f"Management commentary note {index}\n"
        + (
            "EBITDA minus capex operating income depreciation amortization cash flow statements current analysis. "
            "Management discusses these operating metrics and forward expectations. "
        )
        * 8
        for index in range(30)
    )
    originals = {
        "ACME_2024_10K": make_pdf(texts, title="Acme annual report"),
        "BRAVO_2024_10K": make_pdf(texts, title="Bravo annual report"),
        "3M_2024_10K": make_pdf(texts, title="Alphanumeric brand annual report"),
        "ABACUS_BEV_2024_10K": make_pdf(texts, title="Two word brand annual report"),
    }
    sources: dict[str, PDFPageInput] = {}

    async def scenario() -> None:
        async with open_builtin_runtime(config, candidate_pipeline=_DuplicatePagePipeline()) as runtime:
            assert runtime.scopes is not None
            first = await runtime.scopes.create(
                ScopeDraft(title="Reports", summary="Mixed companies", idempotency_key="reports")
            )
            isolated = await runtime.scopes.create(
                ScopeDraft(title="Bravo", summary="Separate scope", idempotency_key="bravo")
            )
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for document_id, content in originals.items():
                for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=content)):
                    source = await registry.resolve(page)
                    sources[source.name] = page
                    observation = project_source_for_transport(registry, source)
                    await runtime.ingestion.submit(
                        SubmitSourceObservation(scope_id=first.scope_id, observation=observation)
                    )
                    await runtime.ingestion.submit(
                        SubmitSourceObservation(scope_id=first.scope_id, observation=observation)
                    )
                    if document_id.startswith("BRAVO"):
                        await runtime.ingestion.submit(
                            SubmitSourceObservation(scope_id=isolated.scope_id, observation=observation)
                        )
            await _flush(runtime.memory.for_scope(first.scope_id), len(texts) * len(originals))
            await _flush(runtime.memory.for_scope(isolated.scope_id), len(texts))
            # A strong lexical hit without a PDF Source is not an admissible page citation.
            await runtime.memory.for_scope(first.scope_id).remember(
                RememberMemoryRequest(
                    entries=(
                        MemoryEntryInput(
                            kind="note", text="Acme legal litigation proceedings lawsuit claims settlements"
                        ),
                        MemoryEntryInput(kind="note", text="Unlisted\nAcme legal litigation proceedings claims"),
                    )
                )
            )

        async with open_builtin_contexts(config) as contexts:
            assert len(await (await contexts.get(first.scope_id)).sources.journal.entries()) == len(texts) * len(
                originals
            )

        old_citation: MemoryCitation | None = None
        async with open_builtin_runtime(config, candidate_pipeline=_DuplicatePagePipeline()) as runtime:
            memory = runtime.memory.for_scope(first.scope_id)
            cases = (
                ("Which countries does Acme operate in?", {1}, 2, "ACME_2024_10K"),
                ("Does Acme have any ongoing legal battles?", {2}, 2, "ACME_2024_10K"),
                (
                    "What is Acme's EBITDA minus capex in fiscal 2024, using income and cash flow statements?",
                    {3, 4},
                    4,
                    "ACME_2024_10K",
                ),
                ("What is Acme's current ratio in 2024?", {5}, 2, "ACME_2024_10K"),
                ("What is 3M's current ratio in 2024?", {5}, 2, "3M_2024_10K"),
                ("What is AbacusBev's current ratio in 2024?", {5}, 2, "ABACUS_BEV_2024_10K"),
            )
            for question, target_pages, limit, target_document in cases:
                citations = await search_pdf_pages(memory, question, limit=limit)
                assert isinstance(citations, tuple) and 0 < len(citations) <= limit
                locations = await _locations(memory, citations, sources)
                matching_pages = {number for document_id, _, number in locations if document_id == target_document}
                assert target_pages <= matching_pages
                assert all(document_id == target_document for document_id, _, _ in locations)
                if target_pages == {2}:
                    old_citation = citations[0]
            assert await search_pdf_pages(memory, "Acme", limit=0) == ()
            assert await search_pdf_pages(memory, " \t", limit=20) == ()
            assert await search_pdf_pages(memory, "zxqvnomatchtoken", limit=20) == ()
            assert await search_pdf_pages(memory, "Unlisted legal proceedings", limit=3)
            with pytest.raises(ValueError):
                await search_pdf_pages(memory, "Acme", limit=-1)
            citations = await search_pdf_pages(
                runtime.memory.for_scope(isolated.scope_id), "Acme legal proceedings", limit=3
            )
            assert citations
            assert all(
                document_id == "BRAVO_2024_10K"
                for document_id, _, _ in await _locations(
                    runtime.memory.for_scope(isolated.scope_id), citations, sources
                )
            )

            # New document bytes create new page identities; existing exact citations still read the old revision.
            revised = make_pdf(
                tuple(text.replace("Civil litigation", "Resolved litigation") for text in texts), title="Revised report"
            )
            for page in parse_pdf_document(PDFDocumentInput(document_id="ACME_2024_10K", content=revised)):
                source = await registry.resolve(page)
                assert source.name not in sources
                sources[source.name] = page
                await runtime.ingestion.submit(
                    SubmitSourceObservation(
                        scope_id=first.scope_id, observation=project_source_for_transport(registry, source)
                    )
                )
            await _flush(memory, len(texts))

        async with open_builtin_runtime(config) as runtime:
            assert old_citation is not None
            entry = await runtime.memory.for_scope(first.scope_id).get(GetMemoryEntryRequest(citation=old_citation))
            assert "Civil litigation" in entry.entry.text and "Resolved litigation" not in entry.entry.text
            assert len(await _locations(runtime.memory.for_scope(first.scope_id), (old_citation,), sources)) == 1

    asyncio.run(scenario())


def test_dense_pdf_pages_keep_complete_memory_bodies_after_restart_and_search_real_statement_titles(
    tmp_path: Path,
) -> None:
    dense = ("Detailed financial disclosure line 0123456789. " * 400) + "\nDenseSentinel 90210"
    raw = make_pdf((
        "Consolidated Statements of Operations\n2025 2024\nRevenue 500 400\nOperating income 70 60\nNet income 40 30",
        "Footnote\nThe following table describes how hedging instruments are reported in other statements.\nConsolidated Statement of Income\n"
        + "Operating income and consolidated income statements are discussed in this note. " * 25,
        dense,
        None,
        "Consolidated Statements of Cash Flows\nOperating activities include cash payments for litigation contingencies.\n2025 2024\nNet cash from operations 70 60",
        "Legal Proceedings\nThere is ongoing material litigation and legal proceedings regarding a civil lawsuit and regulatory claims.",
    ))
    pages = parse_pdf_document(PDFDocumentInput(document_id="DENSE_CO_2025_10K", content=raw))
    assert len(pages[2].text.encode("utf-8")) > 8192
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'dense.db'}"))

    async def scenario() -> None:
        originals = {}
        async with open_builtin_runtime(config, candidate_pipeline=_DuplicatePagePipeline()) as runtime:
            assert runtime.scopes is not None
            scope = (
                await runtime.scopes.create(
                    ScopeDraft(title="Dense PDF", summary="Full-page evidence", idempotency_key="dense")
                )
            ).scope_id
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for page in pages:
                source = await registry.resolve(page)
                originals[source.name] = page
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope, observation=project_source_for_transport(registry, source))
                )
            memory = runtime.memory.for_scope(scope)
            await _flush(memory, len(pages))
            # The extension is evidence-specific: ordinary notes keep their original limit.
            before = (await memory.list()).memory_ref
            with pytest.raises(InvalidMemoryCandidateError):
                await memory.remember(RememberMemoryRequest(entries=(MemoryEntryInput(kind="note", text=dense),)))
            assert (await memory.list()).memory_ref == before
        async with open_builtin_runtime(config) as runtime:
            memory = runtime.memory.for_scope(scope)
            entries = await memory.list()
            assert len(entries.entries) == 2 * len(pages)
            for record in entries.entries:
                page = originals[record.entry.sources[0].source_id]
                assert record.entry.text == f"{page.document_id}\n{page.text}".strip()
            citations = await search_pdf_pages(memory, "Dense Co DenseSentinel", limit=20)
            assert await _locations(memory, citations, originals) == {
                ("DENSE_CO_2025_10K", pages[2].document_sha256, 3)
            }
            citation = citations[0]
            assert "90210" in (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text
            statement = await search_pdf_pages(memory, "What is Dense Co's operating income in 2025?", limit=1)
            assert await _locations(memory, statement, originals) == {
                ("DENSE_CO_2025_10K", pages[0].document_sha256, 1)
            }
            legal = await search_pdf_pages(
                memory, "Has Dense Co reported any materially important ongoing legal battles?", limit=1
            )
            assert await _locations(memory, legal, originals) == {("DENSE_CO_2025_10K", pages[5].document_sha256, 6)}

    asyncio.run(scenario())
