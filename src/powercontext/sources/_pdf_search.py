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

"""Offline query expansion and lexical reranking of real scoped Memory hits."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime import GetMemoryEntryRequest, MemoryEntryRecord, MemorySearchPage, SearchMemoryRequest

_WORDS = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "has",
    "have",
    "had",
    "i",
    "we",
    "you",
    "it",
    "its",
    "our",
    "your",
    "this",
    "that",
    "these",
    "those",
    "please",
    "s",
    "t",
    "answer",
    "question",
    "respond",
    "calculate",
    "calculated",
    "define",
    "give",
    "state",
    "assume",
    "assuming",
    "perspective",
    "investment",
    "analyst",
    "details",
    "shown",
    "using",
    "use",
    "only",
    "within",
    "based",
    "provide",
    "provided",
    "reported",
    "report",
    "company",
    "fiscal",
    "fy",
    "year",
    "years",
    "usd",
    "dollar",
    "dollars",
    "million",
    "millions",
    "amount",
    "quantity",
    "primarily",
    "important",
    "materially",
    "directly",
    "explicitly",
    "outlined",
    "less",
    "than",
    "then",
    "not",
    "if",
    "any",
    "unadjusted",
    "adjusted",
    "respectively",
    "following",
    "total",
])
_DOCUMENT_WORDS = frozenset([
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "company",
    "co",
    "ltd",
    "limited",
    "plc",
    "form",
    "annual",
    "report",
    "k",
    "q",
    "10k",
    "10q",
])

# General financial terminology, independent of any document, question or evidence page.
# Each entry contains trigger phrases and alternative terms used in annual reports.
_CONCEPTS = (
    (("ebitda",), ("operating", "income", "earnings", "profit", "depreciation", "amortization")),
    (("ebit",), ("operating", "income", "earnings", "profit")),
    (("capex", "capital expenditure", "capital expenditures"), ("capital", "expenditures", "property", "equipment")),
    (
        ("d a", "depreciation", "amortization", "amortisation"),
        ("depreciation", "amortization", "amortisation", "depletion"),
    ),
    (("cashflow", "cash flow", "cash flows"), ("cash", "flows", "operating", "financing")),
    (("income statement", "income statements", "p l"), ("income", "earnings", "statements", "operations")),
    (("balance sheet", "financial position"), ("balance", "sheets", "assets", "liabilities")),
    (
        ("geography", "geographies", "geographical", "geographic", "countries", "regions"),
        ("geographic", "geographical", "countries", "international", "regions", "segments"),
    ),
    (
        ("legal", "litigation", "lawsuit", "lawsuits", "battles"),
        ("litigation", "proceedings", "legal", "contingencies", "claims", "settlements"),
    ),
    (
        ("restructuring", "reorganization", "reorganisation"),
        ("restructuring", "charges", "reorganization", "severance"),
    ),
    (("revenue", "revenues", "sales", "turnover"), ("revenue", "revenues", "sales", "turnover")),
    (("profit", "profitability", "margin", "margins"), ("income", "earnings", "profit", "revenue")),
    (("liquidity", "current ratio", "working capital"), ("current", "assets", "liabilities", "cash")),
    (("debt", "borrowings", "indebtedness", "leverage"), ("debt", "borrowings", "notes", "loans")),
    (("receivable", "receivables"), ("accounts", "receivable", "receivables", "credit")),
    (("payable", "payables"), ("accounts", "payable", "payables", "creditors")),
    (("inventory", "inventories"), ("inventory", "inventories", "cost", "goods")),
    (("dividend", "dividends", "repurchase", "buyback"), ("dividends", "repurchases", "shareholders", "stock")),
    (("acquisition", "acquisitions", "merger"), ("acquisitions", "purchase", "goodwill", "business")),
    (("research", "r d"), ("research", "development", "expenses", "costs")),
    (("impairment",), ("impairment", "goodwill", "assets", "charges")),
    (("lease", "leases", "leasing"), ("lease", "leases", "operating", "obligations")),
    (("pension", "retirement"), ("pension", "retirement", "benefit", "obligations")),
)


class _SearchableMemory(Protocol):
    async def search(self, request: SearchMemoryRequest, /) -> MemorySearchPage: ...

    async def get(self, request: GetMemoryEntryRequest, /) -> MemoryEntryRecord: ...


def _words(text: str) -> list[str]:
    return _WORDS.findall(unicodedata.normalize("NFKC", text).casefold())


def _stem(term: str) -> str:
    if len(term) > 5 and term.endswith("ies"):
        return term[:-3] + "y"
    if len(term) > 5 and term.endswith("ing"):
        return term[:-3]
    if len(term) > 4 and term.endswith("ed"):
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us")):
        return term[:-1]
    return term


@dataclass
class _Candidate:
    citation: MemoryCitation
    text: str
    reciprocal_rank: float = 0.0
    entry: MemoryEntryRecord | None = None


def _page_source_id(entry: MemoryEntryRecord) -> str | None:
    references = entry.entry.sources
    if len(references) != 1 or references[0].source_type != "pdf-page":
        return None
    return references[0].source_id


async def _pdf_candidates(memory: _SearchableMemory, candidates: list[_Candidate]) -> list[_Candidate]:
    """Use actual PDF references before treating a text prefix as a document name."""

    documents: set[str] = set()
    for candidate in candidates:
        document = candidate.text.partition("\n")[0]
        if document not in documents:
            candidate.entry = await memory.get(GetMemoryEntryRequest(citation=candidate.citation))
            if _page_source_id(candidate.entry) is not None:
                documents.add(document)
    return [candidate for candidate in candidates if candidate.text.partition("\n")[0] in documents]


def _queries(question: str) -> tuple[list[str], dict[str, float]]:
    raw = _words(question)
    meaningful = list(
        dict.fromkeys(
            word
            for word in raw
            if word not in _STOP_WORDS
            and len(word) > 1
            and not word.isdigit()
            and re.fullmatch(r"fy\d{4}", word) is None
        )
    )
    normalized = " " + " ".join(raw) + " "
    active = [aliases for triggers, aliases in _CONCEPTS if any(f" {trigger} " in normalized for trigger in triggers)]
    weights = {_stem(word): 1.0 for word in meaningful}
    queries = [" ".join(meaningful[:24])]
    for aliases in active:
        for word in aliases:
            weights.setdefault(_stem(word), 0.45)
        # Two-term searches admit either synonym without the long-query coverage floor.
        queries.append(" ".join(aliases[:2]))
        if len(aliases) > 2:
            queries.append(" ".join(aliases[-2:]))
    section_queries = {
        "income": ("statements income", "statements operations", "statements earnings"),
        "cash": ("cash flows",),
        "balance": ("balance sheets", "financial position"),
        "business": ("reportable segments", "geographic operations"),
        "legal": ("legal proceedings", "litigation contingencies"),
    }
    for section in _wanted_sections(question):
        queries.extend(section_queries[section])
    # Preserve unusual domain terms that may otherwise lose admission in a long question.
    focused = sorted(meaningful, key=lambda word: (-len(word), meaningful.index(word)))[:4]
    queries.extend(focused)
    return list(dict.fromkeys(query for query in queries if query))[:24] or [question.strip()], weights


def _wanted_sections(question: str) -> tuple[str, ...]:
    query = " ".join(_words(question))

    # Word boundaries prevent "reported any" from being mistaken for D&A,
    # or an unrelated word ending in p followed by l from matching P&L.
    def contains(term: str) -> bool:
        return re.search(r"\b" + re.escape(term.strip()) + r"\b", query) is not None

    requested = []
    if any(
        contains(term)
        for term in (
            "income statement",
            "income statements",
            "statement of income",
            "statements of income",
            "statement of operations",
            "statements of operations",
            "statement of earnings",
            "statements of earnings",
            "p l",
            "ebitda",
            "ebit ",
            "operating income",
            "operating profit",
            "gross profit",
            "profit margin",
            "margin",
            "net income",
            "net sales",
            "revenue growth",
        )
    ):
        requested.append("income")
    if any(
        contains(term) for term in ("cash flow", "cash flows", "cashflow", "ebitda", "capex", "depreciation", "d a")
    ):
        requested.append("cash")
    if any(
        contains(term)
        for term in (
            "balance sheet",
            "balance sheets",
            "financial position",
            "current ratio",
            "quick ratio",
            "assets",
            "liabilities",
            "working capital",
            "cash ratio",
            "debt ratio",
        )
    ):
        requested.append("balance")
    if re.search(r"\bgeograph\w*\b", query) or any(
        contains(term) for term in ("countries", "regions", "primarily operate", "business model")
    ):
        requested.append("business")
    if any(contains(term) for term in ("legal", "litigation", "lawsuit", "battles")):
        requested.append("legal")
    return tuple(requested)


def _financial_headings(body: str) -> set[str]:
    """Recognize standalone titles, not a note referring to another statement."""
    lines = [line.strip().lower() for line in body.splitlines()[:24] if line.strip()]
    if re.search(r"\bindex to (?:the )?(?:consolidated )?financial statements\b", " ".join(lines)):
        return set()
    prefix = r"(?:(?:consolidated|condensed|combined|unaudited))*"
    suffix = r"(?:continued|unaudited)*"
    titles = {
        "income": r"(?:statementsof(?:net)?(?:income|earnings|operations|loss)|statementof(?:net)?(?:income|earnings|operations|loss)|incomestatements?|profitandlossstatements?)(?:loss|income|andcomprehensive(?:income|loss))?",
        "cash": r"(?:statementsofcashflows?|statementofcashflows?|cashflowstatements?)",
        "balance": r"(?:balancesheets?|statementsoffinancial(?:position|condition)|statementoffinancial(?:position|condition))",
    }
    found = set()
    for start in range(len(lines)):
        if any(len(_words(line)) >= 9 for line in lines[:start]):
            # A table column below explanatory prose can mention another
            # statement; it does not make this a primary financial statement.
            break
        for width in (1, 2, 3):
            title = re.sub(r"[^a-z]", "", " ".join(lines[start : start + width]))
            for kind, pattern in titles.items():
                if re.fullmatch(prefix + pattern + suffix, title):
                    found.add(kind)
    return found


def _section_types(text: str) -> set[str]:
    words = _words(text.partition("\n")[2])
    heading, body = " ".join(words[:80]), " ".join(words)
    if len(re.findall(r"\bitem\s*\d+", body)) >= 5:
        return set()
    patterns = {
        "business": r"our (?:company|business|operations|divisions|segments)|geographical operations|business overview",
        "legal": r"legal proceedings|litigation|commitments and contingencies",
    }
    result = _financial_headings(text.partition("\n")[2])
    for kind, pattern in patterns.items():
        match = re.search(pattern, heading)
        if match is not None and len(heading[: match.start()].split()) <= 16:
            result.add(kind)
    # Standard annual-report sections may start partway down a physical page.
    if re.search(r"\bitem\s*1\s+business\b", body):
        result.add("business")
    if re.search(r"\bitem\s*3\s+legal proceedings\b", body):
        result.add("legal")
    return result


def _heading_bonus(question: str, text: str) -> float:
    query = " ".join(_words(question))
    heading = " ".join(_words(text)[:120])
    bonus = 3.0 * len(set(_wanted_sections(question)) & _section_types(text))
    if any(term in query for term in ("geograph", "countries", "regions")):
        bonus += 1.5 * bool(re.search(r"business|segments?|geographic|international", heading))
    if any(term in query for term in ("legal", "litigation", "lawsuit", "battles")):
        bonus += 2.0 * bool(re.search(r"legal proceedings|litigation|contingencies", heading))
    return bonus


def _document_anchor(document: str, query_compounds: set[str]) -> float:
    names = tuple(
        dict.fromkeys(
            word for word in _words(document) if not word.isdigit() and word not in _DOCUMENT_WORDS and len(word) > 1
        )
    )
    if not names:
        return 0.0
    joined, acronym = "".join(names), "".join(word[0] for word in names)
    if joined in query_compounds or (len(acronym) > 1 and acronym not in _STOP_WORDS and acronym in query_compounds):
        return float(len(joined))
    matched = sum(len(name) for name in names if name in query_compounds)
    return matched * matched / len(joined)


def _query_compounds(question: str) -> set[str]:
    """Match a company with or without spaces and punctuation, using whole tokens."""
    words = _words(question)
    return {
        "".join(words[start:end])
        for start in range(len(words))
        for end in range(start + 1, min(start + 9, len(words)) + 1)
    }


def _rank_candidates(
    candidates: list[_Candidate], question: str, weights: dict[str, float], limit: int
) -> list[_Candidate]:
    if not candidates:
        return []
    counters = [Counter(_stem(word) for word in _words(candidate.text.partition("\n")[2])) for candidate in candidates]
    frequencies: Counter[str] = Counter()
    for counter in counters:
        frequencies.update(counter.keys())
    average = max(1.0, sum(sum(counter.values()) for counter in counters) / len(counters))
    query_compounds = _query_compounds(question)
    years = set(re.findall(r"(?:19|20)\d\d", question))
    scores: list[float] = []
    anchors: list[float] = []
    relevant: list[bool] = []
    for candidate, terms in zip(candidates, counters, strict=True):
        length = sum(terms.values())
        score = 0.0
        for term, weight in weights.items():
            frequency = terms[term]
            if frequency:
                inverse_frequency = math.log1p((len(counters) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += (
                    weight * inverse_frequency * frequency * 2.2 / (frequency + 1.2 * (0.5 + 0.5 * length / average))
                )
        bonus = _heading_bonus(question, candidate.text)
        relevant.append(score > 0.0 or bonus > 0.0)
        score += bonus
        score += 8.0 * candidate.reciprocal_rank
        document_words = set(_words(candidate.text.partition("\n")[0]))
        anchors.append(_document_anchor(candidate.text.partition("\n")[0], query_compounds))
        if years & document_words:
            score += 0.5
        scores.append(score)
    maximum_anchor = max(anchors)
    has_named_document = maximum_anchor >= 1.0
    minimum_anchor = maximum_anchor * 0.9 if has_named_document else 0.0
    scored = [
        (score * (0.25 + 0.75 * anchor / maximum_anchor) if has_named_document else score, candidate)
        for score, anchor, candidate, matches in zip(scores, anchors, candidates, relevant, strict=True)
        if anchor >= minimum_anchor and matches
    ]
    scored.sort(key=lambda item: (-item[0], item[1].citation.entry_version_id))
    return _diversify(scored, question, limit)


def _diversify(scored: list[tuple[float, _Candidate]], question: str, limit: int) -> list[_Candidate]:
    """Diversify a bounded prefix, preserving the remaining verified candidates as fallbacks."""

    order: list[int] = []
    for kind in _wanted_sections(question):
        matches = [index for index, (_, candidate) in enumerate(scored) if kind in _section_types(candidate.text)]
        order.extend(index for index in matches[:2] if index not in order)
    # Ratios and derived financial metrics may require inputs from more than the
    # explicitly named statement. Keep a small statement coverage reserve at
    # evidence-page retrieval budgets, leaving most places for lexical evidence.
    financial_terms = {
        "ratio",
        "margin",
        "turnover",
        "ebit",
        "ebitda",
        "profit",
        "income",
        "revenue",
        "sales",
        "assets",
        "liabilities",
        "debt",
        "equity",
        "capex",
        "capital",
        "dividend",
        "inventory",
        "receivable",
        "payable",
        "cost",
        "costs",
    }
    if limit >= 10 and financial_terms.intersection(_words(question)):
        for kind in ("income", "cash", "balance"):
            matches = [index for index, (_, candidate) in enumerate(scored) if kind in _section_types(candidate.text)]
            if matches and not any(index in order for index in matches):
                order.append(matches[0])
    order = order[:limit]
    terms = [{_stem(word) for word in _words(candidate.text) if word not in _STOP_WORDS} for _, candidate in scored]
    similarities = [0.0] * len(scored)
    remaining = set(range(min(400, len(scored)))) - set(order)
    for index in remaining:
        similarities[index] = max(
            (len(terms[index] & terms[seed]) / max(1, len(terms[index] | terms[seed])) for seed in order), default=0.0
        )
    for _ in range(min(len(remaining), max(40, limit * 2) - len(order))):
        best = max(remaining, key=lambda index: (scored[index][0] * (1.0 - 0.2 * similarities[index]), -index))
        remaining.remove(best)
        order.append(best)
        for index in remaining:
            overlap = len(terms[index] & terms[best]) / max(1, len(terms[index] | terms[best]))
            similarities[index] = max(similarities[index], overlap)
    chosen = set(order)
    return [scored[index][1] for index in order] + [
        candidate for index, (_, candidate) in enumerate(scored) if index not in chosen
    ]


async def search_pdf_pages(memory: _SearchableMemory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Search only the supplied Memory and return verified, distinct page citations."""

    if limit < 0:
        raise ValueError("PDF search limit must not be negative")  # noqa: TRY003
    if limit == 0 or not question.strip():
        return ()
    queries, weights = _queries(question)
    candidates: dict[tuple[str, int, str, str], _Candidate] = {}
    for query in queries:
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(256, min(1000, limit * 24))))
        _collect_candidates(candidates, page)
    admitted = await _pdf_candidates(memory, list(candidates.values()))
    # A company name is present in every page projection. Once a real hit identifies
    # the requested document, retrieve its pages before reranking. Global term
    # limits must not hide its short financial statements behind longer notes or
    # pages from other reports. These are still real scoped Memory search hits.
    compounds = _query_compounds(question)
    documents = {candidate.text.partition("\n")[0] for candidate in admitted}
    anchors = {document: _document_anchor(document, compounds) for document in documents}
    best = max(anchors.values(), default=0.0)
    if best >= 1.0:
        for document in sorted(doc for doc, anchor in anchors.items() if anchor >= best * 0.9):
            page = await memory.search(SearchMemoryRequest(query=document, mode="fts", limit=max(2048, limit * 64)))
            _collect_candidates(candidates, page)
        admitted = await _pdf_candidates(memory, list(candidates.values()))
    selected: list[MemoryCitation] = []
    seen: set[str] = set()
    for candidate in _rank_candidates(admitted, question, weights, limit):
        entry = candidate.entry
        if entry is None:
            entry = await memory.get(GetMemoryEntryRequest(citation=candidate.citation))
        source_id = _page_source_id(entry)
        if source_id is None or source_id in seen:
            continue
        seen.add(source_id)
        selected.append(candidate.citation)
        if len(selected) == limit:
            break
    return tuple(selected)


def _collect_candidates(candidates: dict[tuple[str, int, str, str], _Candidate], page: MemorySearchPage) -> None:
    """Retain exact Memory identities while combining recall queries."""
    for rank, hit in enumerate(page.hits, start=1):
        key = (hit.memory_ref.artifact_id, hit.memory_ref.revision, hit.entry_id, hit.entry_version_id)
        candidate = candidates.setdefault(
            key,
            _Candidate(
                citation=MemoryCitation(
                    memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                ),
                text=hit.text,
            ),
        )
        candidate.reciprocal_rank += 1.0 / (60.0 + rank)
