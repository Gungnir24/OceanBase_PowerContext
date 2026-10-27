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

"""Offline lexical PDF page ranking over public, scope-bound Memory reads."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from typing import TYPE_CHECKING

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime.models import MemoryEntryRecord, SearchMemoryRequest

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication

_WORD = re.compile(r"[a-z]+|\d+")
_STOP = frozenset(
    "a an the of to in on for and or as at by is are was were be been being has have had "  # noqa: SIM905
    "what which how does do did can could would should from with that this these those its it "
    "please answer respond give shown using use only details detail assuming assume perspective "
    "investment analyst question calculate calculated define relying reported primarily fy usd million millions "
    "who among between most least if not then why much any something financial ask company like state explain "
    "compare compared based relevant measure metric useful profile reasonably healthy improved improving declined "
    "improve change changed drove direct directly explicitly outlined quantity details".split()
)
_CONCEPTS = (
    (
        "geography geographic geographical geographies region",
        "geographic segment international country countries region market",
    ),
    ("legal litigation lawsuit battle proceeding", "legal litigation proceeding contingency contingencies lawsuit"),
    ("capex", "capital expenditure purchase property plant equipment"),
    ("ebitda", "operating income depreciation amortization"),
    ("revenue sale turnover", "revenue sale"),
    ("income earning profit", "income earning profit operation"),
    ("restructure restructuring reorganization", "restructuring impairment charge"),
    ("inventory inventories", "inventory inventories"),
    ("receivable receivables", "account receivable"),
    ("debt borrowing leverage", "debt borrowing obligation"),
    ("dividend distribution", "dividend distribution"),
    ("cashflow", "cash flow operating investing financing"),
    ("liquidity quick", "cash equivalent current asset current liability receivable inventory"),
    ("tax taxation", "income tax effective rate provision pretax"),
    ("customer client buyer", "customer client concentration revenue sale contract"),
    ("category categories product service", "segment product service revenue sale"),
    ("retention retain attrition", "retention attrition renewal customer card member account"),
    ("acquisition acquire", "acquisition acquired purchase business combination"),
    ("capital intensive intensity", "capital expenditure property plant equipment asset revenue depreciation"),
    ("turnover inventory", "cost sale inventory current asset"),
    ("growth", "growth organic sale revenue acquisition divestiture"),
)


@lru_cache(maxsize=8192)
def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "xes", "ches", "shes", "zes")):
        word = word[:-2]
    elif len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    if len(word) > 6 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 5 and word.endswith("ed"):
        word = word[:-2]
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def _terms(text: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = re.sub(r"cash[\s-]+flows?", "cashflow cash flow", normalized)
    return tuple(
        _stem(word) for word in _WORD.findall(normalized) if word not in _STOP and (len(word) > 1 or word.isdigit())
    )


def _query_weights(question: str) -> dict[str, float]:
    weights = dict.fromkeys(_terms(question), 1.0)
    original = set(weights)
    for triggers, expansion in _EXPANSIONS:
        if original.intersection(triggers):
            for term in expansion:
                weights.setdefault(term, 0.45)
    return weights


_EXPANSIONS = tuple((frozenset(_terms(triggers)), _terms(expansion)) for triggers, expansion in _CONCEPTS)
_STATEMENTS = {
    "income": re.compile(
        r"(?:consolidated\s+)?statements?\s+of\s+(?:net\s+)?(?:income|operations|earnings)"
        r"(?:\s+and\s+comprehensive\s+(?:income|loss))?",
        re.I,
    ),
    "cash": re.compile(r"(?:consolidated\s+)?statements?\s+of\s+cash\s+flows?", re.I),
    "balance": re.compile(r"(?:consolidated\s+)?(?:balance\s+sheets?|statements?\s+of\s+financial\s+position)", re.I),
}


@dataclass(frozen=True, slots=True)
class _PageTerms:
    document: str
    counts: Counter[str]
    length: int
    phrases: str
    statements: frozenset[str]


@lru_cache(maxsize=2048)
def _page_terms(text: str) -> _PageTerms:
    document, _, body = text.partition("\n")
    terms = _terms(body)
    # Contents pages and prose references are not statement headings.
    header = body[:800]
    statements = frozenset(
        name
        for name, pattern in _STATEMENTS.items()
        if any(
            not header[match.end() :].split("\n", 1)[0].strip(" \t\r:")
            or header[match.end() :].lstrip(" \t").startswith("(")
            for match in pattern.finditer(header)
        )
    )
    if re.search(r"\bindex\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements\b", header, re.I):
        statements = frozenset()
    return _PageTerms(document, Counter(terms), len(terms), " " + " ".join(terms) + " ", statements)


def _requested_statements(terms: set[str], question: str) -> frozenset[str]:
    families = set()
    if terms.intersection(_terms("cashflow ebitda capex capital intensive")):
        families.add("cash")
    if (
        terms.intersection(_terms("income earnings profit margin ebitda tax turnover capital"))
        or "p&l" in question.lower()
    ):
        families.add("income")
    if terms.intersection(_terms("balance assets liabilities equity quick liquidity inventory capital")):
        families.add("balance")
    if re.search(r"\bcurrent\s+ratio\b", question, re.I):
        families.add("balance")
    return frozenset(families)


def _rank_pages(
    records: Sequence[MemoryEntryRecord], question: str, native_ranks: dict[str, int]
) -> tuple[MemoryEntryRecord, ...]:
    pages = [_page_terms(record.entry.text) for record in records]
    average = sum(page.length for page in pages) / max(1, len(pages))
    words = _terms(question)
    weights = _query_weights(question)
    frequency = {term: sum(term in page.counts for page in pages) for term in weights}
    question_words = re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", question).casefold())
    names = {"".join(question_words[i : i + size]) for i in range(len(question_words)) for size in range(1, 7)}
    companies = {page.document: _document_company(page.document) for page in pages}
    matched = {document for document, company in companies.items() if company in names}
    if matched:
        # Prefer the complete company name to another company's shared word.
        longest = max(len(companies[document]) for document in matched)
        matched = {document for document in matched if len(companies[document]) == longest}
    aliases = {
        _stem(word.casefold())
        for word in re.findall(r"\b[A-Z][A-Za-z]{1,9}\b", question)
        if sum(char.isupper() for char in word) > 1
    }
    aliases.difference_update(_terms("DPO COGS EPS ROE ROA ROIC EBIT EBITDA GAAP USD M&A D&A"))
    entity_aliases = set()
    if not matched:
        for alias in aliases:
            documents = {page.document for page in pages if alias in page.counts}
            if len(documents) == 1 and sum(alias in page.counts for page in pages) >= 2:
                matched.update(documents)
                entity_aliases.add(alias)
    company_terms = {term for term in weights if any(term in companies[document] for document in matched)}
    company_terms.update(entity_aliases)
    weights = {
        term: weight * (0.05 if term.isdigit() or term in company_terms else 1.0) for term, weight in weights.items()
    }
    weights = {
        term: weight * math.log1p((len(pages) - frequency[term] + 0.5) / (frequency[term] + 0.5))
        for term, weight in weights.items()
    }
    phrases = tuple(f" {a} {b} " for a, b in pairwise(words) if a not in company_terms and b not in company_terms)
    requested = _requested_statements(set(words), question)
    scores = []
    for index, (record, page) in enumerate(zip(records, pages, strict=True)):
        if matched and page.document not in matched:
            continue
        norm = 1.2 * (0.25 + 0.75 * page.length / max(1.0, average))
        score = sum(
            weight * page.counts[term] * 2.2 / (page.counts[term] + norm)
            for term, weight in weights.items()
            if page.counts[term]
        )
        score += sum(3.0 for phrase in phrases if phrase in page.phrases)
        score += 0.1 / (1 + native_ranks.get(record.entry.entry_id, 1000))
        scores.append((score, index))
    scores.sort(key=lambda row: (-row[0], records[row[1]].entry.entry_id))
    # Reserve a small budget for complementary statements needed by ratios and
    # multi-statement questions; notes still compete on their own lexical score.
    selected = []
    for family in sorted(requested):
        candidates = [
            index
            for score, index in scores
            if family in pages[index].statements and (not matched or pages[index].document in matched)
        ]
        selected.extend(index for index in candidates[:2] if index not in selected)
    reserved = set(selected)
    selected.extend(index for score, index in scores if score > 0.01 and index not in reserved)
    return tuple(records[index] for index in selected)


def _document_company(document: str) -> str:
    name = re.split(r"[_ -](?:19|20)\d{2}", document, maxsplit=1)[0]
    return "".join(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKC", name).casefold()))


async def search_pages(memory: ScopedMemoryApplication, question: str, limit: int) -> tuple[MemoryCitation, ...]:
    if type(limit) is not int or limit < 1:
        raise ValueError("PDF search limit must be a positive integer")  # noqa: TRY003
    if not isinstance(question, str) or not question.strip():
        raise ValueError("PDF search question must not be blank")  # noqa: TRY003
    # Read current records on every call; cached terms never carry citations or scope state.
    page = await memory.list()
    records = tuple(
        record for record in page.entries if any(ref.source_type == "pdf-page" for ref in record.entry.sources)
    )
    if not records:
        return ()
    native = await memory.search(
        SearchMemoryRequest(query=question, mode="fts", limit=min(len(records), max(64, limit * 4)))
    )
    ranks = {hit.entry_id: rank for rank, hit in enumerate(native.hits)}
    selected = []
    seen: set[str] = set()
    for record in _rank_pages(records, question, ranks):
        identities = {ref.source_id for ref in record.entry.sources if ref.source_type == "pdf-page"}
        if identities.intersection(seen):
            continue
        selected.append(record.citation)
        seen.update(identities)
        if len(selected) == limit:
            break
    return tuple(selected)
