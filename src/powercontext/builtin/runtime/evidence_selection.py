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

"""Offline literal passage ranking and selection under complete rendering costs."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

if TYPE_CHECKING:
    from powercontext.builtin.runtime.prepared_context import MemoryEntryAddress

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARY = re.compile(r"""[.!?](?:["'”’\)\]]+)?(?=\s|$)|[。！？](?:["'”’\)\]]+)?|[\r\n\u2028\u2029]+""")  # noqa: RUF001
_STOP = frozenset([
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "during",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "of",
    "on",
    "or",
    "our",
    "than",
    "that",
    "the",
    "their",
    "these",
    "they",
    "this",
    "those",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "will",
    "with",
    "would",
    "we",
])
_GENERIC = frozenset([
    "associated",
    "association",
    "effect",
    "effects",
    "demonstrate",
    "demonstrates",
    "study",
    "studies",
    "show",
    "shows",
    "suggest",
    "suggests",
    "properties",
])
_ABBREVIATIONS = frozenset([
    "al",
    "approx",
    "ca",
    "cf",
    "dr",
    "e",
    "eq",
    "fig",
    "figs",
    "g",
    "i",
    "mr",
    "mrs",
    "ms",
    "no",
    "nos",
    "prof",
    "st",
    "vs",
    "vol",
])
_RESULTS = frozenset([
    "found",
    "findings",
    "observed",
    "results",
    "conclusion",
    "conclusions",
    "demonstrated",
    "revealed",
    "showed",
    "significantly",
])
_PREFIX = TRUST_POLICY + '\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n{"trust":"untrusted_history","items":['
_SUFFIX = "]}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"
_EMPTY_BYTES = len((_PREFIX + _SUFFIX).encode("utf-8"))


@dataclass(frozen=True)
class MemoryEvidenceFragment:
    """An unchanged slice of one exact, scope-qualified Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class EvidenceDocument:
    address: MemoryEntryAddress
    text: str


@dataclass(frozen=True)
class _Sentence:
    start: int
    end: int
    terms: Counter[str]


@dataclass(frozen=True)
class _Plan:
    cost: int = 0
    value: float = 0.0
    fragments: tuple[MemoryEvidenceFragment, ...] = ()


def _encoded(fragment: MemoryEvidenceFragment) -> str:
    address = fragment.address
    encoded = json.dumps(
        {
            "citation": {
                "scope_id": address.memory.scope_id,
                "memory_ref": address.memory.artifact.model_dump(mode="json"),
                "entry_id": address.entry_id,
                "entry_version_id": address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # JSON may contain these characters literally, but the envelope is line-delimited.
    return encoded.replace("\x85", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_evidence(fragments: Sequence[MemoryEvidenceFragment]) -> str:
    """Render the exact same bytes whose per-item costs drive selection."""
    return _PREFIX + ",".join(_encoded(fragment) for fragment in fragments) + _SUFFIX


def _stem(word: str) -> str:
    # Small, language-independent call sites; only English suffixes are folded.
    if not word.isascii() or not word.isalpha() or len(word) < 4:
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix, replacement in (
        ("ization", "ize"),
        ("ational", "ate"),
        ("tional", "tion"),
        ("ation", "ate"),
        ("ition", "it"),
        ("ness", ""),
        ("ment", ""),
        ("ing", ""),
        ("ed", ""),
        ("ly", ""),
    ):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            word = word[: -len(suffix)] + replacement
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    if len(word) > 4 and word[-1] == word[-2] and word[-1] in "bdgmnprt":
        word = word[:-1]
    return word


def _terms(text: str) -> list[str]:
    result = []
    for match in _WORD.finditer(unicodedata.normalize("NFKC", text).casefold()):
        word = match.group()
        if word in _STOP:
            continue
        if all("\u4e00" <= char <= "\u9fff" for char in word) and len(word) > 1:
            result.extend(word[index : index + 2] for index in range(len(word) - 1))
        else:
            result.append(_stem(word))
    return result


def _sentences(text: str) -> tuple[_Sentence, ...]:
    boundaries = []
    for match in _BOUNDARY.finditer(text):
        if text[match.start()] == ".":
            previous = re.search(r"([A-Za-z]+)$", text[: match.start()])
            if previous is not None and (previous[1].casefold() in _ABBREVIATIONS or len(previous[1]) == 1):
                continue
        boundaries.append(match.end())
    boundaries.append(len(text))
    start = 0
    sentences = []
    for end in boundaries:
        while start < end and text[start].isspace():
            start += 1
        trimmed_end = end
        while trimmed_end > start and text[trimmed_end - 1].isspace():
            trimmed_end -= 1
        if start < trimmed_end:
            sentences.append(_Sentence(start, trimmed_end, Counter(_terms(text[start:trimmed_end]))))
        start = end
    return tuple(sentences)


def _score(terms: Counter[str], weights: dict[str, float], average: float, document_coverage: float) -> float:
    total = sum(weights.values())
    if not total:
        return 0.0
    overlap = sum(weight for word, weight in weights.items() if word in terms) / total
    normalization = 1.2 * (0.5 + 0.5 * sum(terms.values()) / max(1.0, average))
    bm25 = (
        sum(weight * terms[word] / (terms[word] + normalization) for word, weight in weights.items() if word in terms)
        / total
    )
    return (0.7 * overlap**1.35 + 0.3 * bm25) * (0.2 + 0.8 * document_coverage**1.3)


def _fragment(document: EvidenceDocument, start: int, end: int) -> MemoryEvidenceFragment:
    return MemoryEvidenceFragment(document.address, start, end, document.text[start:end])


def _option(fragments: tuple[MemoryEvidenceFragment, ...], value: float) -> _Plan:
    # A comma is charged for each item. The shared envelope refunds the first.
    return _Plan(sum(len(_encoded(item).encode("utf-8")) + 1 for item in fragments), value, fragments)


def _frontier(plans: Iterable[_Plan], limit: int = 192) -> list[_Plan]:
    frontier = []
    best = -1.0
    for plan in sorted(plans, key=lambda plan: (plan.cost, -plan.value)):
        if plan.value > best + 1e-12:
            frontier.append(plan)
            best = plan.value
    if len(frontier) <= limit:
        return frontier
    # Bound the budget frontier even for unusually large candidate bodies.
    return [frontier[index * (len(frontier) - 1) // (limit - 1)] for index in range(limit)]


def _long_sentence_options(
    document: EvidenceDocument, sentence: _Sentence, weights: dict[str, float], average: float, coverage: float
) -> list[_Plan]:
    words = list(re.finditer(r"\S+", document.text[sentence.start : sentence.end]))
    anchors = [index for index, word in enumerate(words) if set(_terms(word.group())) & weights.keys()]
    anchors = anchors[:16] + anchors[-16:]
    options = []
    seen = set()
    for anchor in anchors:
        for width in (12, 24, 48, 96):
            left = max(0, anchor - width // 2)
            right = min(len(words), left + width)
            start, end = sentence.start + words[left].start(), sentence.start + words[right - 1].end()
            if (start, end) in seen:
                continue
            seen.add((start, end))
            fragment = _fragment(document, start, end)
            score = _score(Counter(_terms(fragment.text)), weights, average, coverage)
            options.append(_option((fragment,), score**1.4))
    cjk = [word for word in weights if not word.isascii()]
    if cjk:
        pattern = re.compile("|".join(re.escape(word) for word in sorted(cjk)))
        for match in list(pattern.finditer(document.text[sentence.start : sentence.end]))[:32]:
            for width in (96, 192, 384):
                start = max(sentence.start, sentence.start + match.start() - width // 2)
                end = min(sentence.end, start + width)
                fragment = _fragment(document, start, end)
                score = _score(Counter(_terms(fragment.text)), weights, average, coverage)
                options.append(_option((fragment,), score**1.4))
    return options


def _document_options(
    document: EvidenceDocument,
    sentences: tuple[_Sentence, ...],
    weights: dict[str, float],
    average: float,
    coverage: float,
    capacity: int,
) -> list[_Plan]:
    raw = [_score(sentence.terms, weights, average, coverage) for sentence in sentences]
    scores = []
    for index, sentence in enumerate(sentences):
        position = index / max(1, len(sentences) - 1)
        empirical = bool(set(_terms(document.text[sentence.start : sentence.end])) & {_stem(word) for word in _RESULTS})
        score = raw[index] * (1 + 0.06 * position + 0.06 * empirical)
        # Adjacent explanatory statements may use pronouns instead of claim terms.
        neighboring = max(raw[max(0, index - 1) : index] + raw[index + 1 : index + 2], default=0.0)
        scores.append(max(score, 0.12 * neighboring) ** 1.4)
    options = []
    singles = []
    for left, sentence in enumerate(sentences):
        for right in range(left, min(left + 4, len(sentences))):
            value = sum(scores[left : right + 1])
            if value <= 0:
                continue
            fragment = _fragment(document, sentence.start, sentences[right].end)
            option = _option((fragment,), value)
            if option.cost <= capacity:
                options.append(option)
                if right == left:
                    singles.append(option)
        complete = _option((_fragment(document, sentence.start, sentence.end),), 0.0)
        if complete.cost > capacity and raw[left] > 0:
            options.extend(_long_sentence_options(document, sentence, weights, average, coverage))
    options.extend(_separated_options(document, singles))
    return _frontier([_Plan(), *(option for option in options if 0 < option.cost <= capacity)], limit=64)


def _separated_options(document: EvidenceDocument, singles: Sequence[_Plan]) -> list[_Plan]:
    """Two separated passages retain independent exact citations."""
    options = []
    best_singles = sorted(singles, key=lambda option: option.value / option.cost**0.4, reverse=True)[:12]
    for index, first in enumerate(best_singles):
        for second in best_singles[index + 1 :]:
            pair = tuple(sorted((*first.fragments, *second.fragments), key=lambda fragment: fragment.start))
            if pair[0].end < pair[1].start and document.text[pair[0].end : pair[1].start].strip():
                options.append(_option(pair, first.value + second.value))
    return options


def select_evidence(
    query: str, documents: Sequence[EvidenceDocument], max_bytes: int
) -> tuple[MemoryEvidenceFragment, ...]:
    """Rank all original sentences, then solve the costed document-option frontier."""
    capacity = max_bytes - _EMPTY_BYTES + 1
    if capacity <= 0:
        return ()
    query_terms = sorted(set(_terms(query)))
    if not query_terms:
        return ()
    sentences = [_sentences(document.text) for document in documents]
    document_terms = [set().union(*(sentence.terms.keys() for sentence in group)) for group in sentences]
    sentence_frequency = Counter(word for group in sentences for sentence in group for word in sentence.terms)
    document_frequency = Counter(word for terms in document_terms for word in terms)
    count = sum(len(group) for group in sentences)
    average = sum(sum(sentence.terms.values()) for group in sentences for sentence in group) / max(1, count)
    generic = {_stem(word) for word in _GENERIC}
    weights = {
        word: math.sqrt(
            (0.5 + math.log1p((len(documents) + 0.5) / (document_frequency[word] + 0.5)))
            * (0.6 + math.log1p((count + 0.5) / (sentence_frequency[word] + 0.5)))
        )
        * (0.25 if word in generic else 1.0)
        for word in query_terms
    }
    total = sum(weights.values())
    frontier = [_Plan()]
    for document, group, terms in zip(documents, sentences, document_terms, strict=True):
        coverage = sum(weight for word, weight in weights.items() if word in terms) / total
        options = _document_options(document, group, weights, average, coverage, capacity)
        frontier = _frontier(
            _Plan(plan.cost + option.cost, plan.value + option.value, plan.fragments + option.fragments)
            for plan in frontier
            for option in options
            if plan.cost + option.cost <= capacity
        )
    selected = max(frontier, key=lambda plan: (plan.value, -plan.cost)).fragments
    return selected
