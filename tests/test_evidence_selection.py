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
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput, MemoryHit
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.prepared_context import (
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY
from powercontext.builtin.scope import ScopeDraft


def candidates(*texts, scope="scope-one"):
    reference = ArtifactRef(family="memory", artifact_id="memory-original-revision", revision=3)
    return PreparedMemoryCandidates(
        scope_id=scope,
        memory_ref=reference,
        hits=tuple(
            MemoryHit(
                memory_ref=reference,
                entry_id=f"entry-{index}",
                entry_version_id=f"version-{index}",
                text=text,
                score=1.0,
                matched_by=("fts",),
            )
            for index, text in enumerate(texts)
        ),
    )


def build(query, groups, budget=1400, **settings):
    request = PrepareContextRequest(query=query, max_bytes=budget, evidence_selection=True, **settings)
    return PreparedContextBuilder().build_scopes_result(
        request=request, current_scope_id="scope-one", memory_candidates=groups
    )


def validate(result, groups, budget):
    content = result.context.content or ""
    assert result.context.content_bytes == len(content.encode("utf-8")) <= budget
    assert len(result.origins) == len(result.fragments)
    originals = {
        (
            group.scope_id,
            hit.memory_ref.artifact_id,
            hit.memory_ref.revision,
            hit.entry_id,
            hit.entry_version_id,
        ): hit.text
        for group in groups
        for hit in group.hits
    }
    if not content:
        assert result.context.status == "empty" and result.fragments == result.origins == ()
        return
    assert content.startswith(TRUST_POLICY + "\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n")
    assert content.endswith("\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1")
    payload = json.loads(content.splitlines()[-2])
    assert payload["trust"] == "untrusted_history"
    assert len(payload["items"]) == len(result.fragments)
    for item, fragment, origin in zip(payload["items"], result.fragments, result.origins, strict=True):
        address = fragment.address
        artifact = address.memory.artifact
        original = originals[
            (
                address.memory.scope_id,
                artifact.artifact_id,
                artifact.revision,
                address.entry_id,
                address.entry_version_id,
            )
        ]
        assert type(fragment.start) is int and type(fragment.end) is int
        assert 0 <= fragment.start < fragment.end <= len(original)
        assert fragment.text == original[fragment.start : fragment.end] == item["content"]
        assert origin == address
        assert item["start"] == fragment.start and item["end"] == fragment.end
        assert item["citation"] == {
            "scope_id": address.memory.scope_id,
            "memory_ref": artifact.model_dump(mode="json"),
            "entry_id": address.entry_id,
            "entry_version_id": address.entry_version_id,
        }


def test_queries_select_complete_late_sentences_and_two_separated_passages():
    cobalt = "Cobalt flux increases at 37 degrees."
    amber = "Amber threshold decreases to 19 units."
    padding = "Unrelated administrative history. " * 100
    text = padding + cobalt + " " + padding + amber
    groups = (candidates(text, "Administrative guidelines describe unrelated billing procedures."),)
    for query, expected in (("cobalt flux increases", cobalt), ("amber threshold decreases", amber)):
        result = build(query, groups, 1200)
        validate(result, groups, 1200)
        start = text.index(expected)
        assert any(fragment.start <= start and fragment.end >= start + len(expected) for fragment in result.fragments)
    combined = build("cobalt flux amber threshold", groups)
    validate(combined, groups, 1400)
    assert len(combined.fragments) >= 2
    for expected in (cobalt, amber):
        start = text.index(expected)
        assert any(fragment.start <= start and fragment.end >= start + len(expected) for fragment in combined.fragments)


@pytest.mark.parametrize("budget", [512, 600, 700, 800, 900, 1000, 1200, 1400, 1800, 3200])
@pytest.mark.parametrize(
    "text,query",
    [
        (
            'Sensor "quotes" and backslashes \\ persist.\r\n海洋观测 station 温度为37°C。\nA second sensor reading is 41°C.',
            "海洋观测 sensor",
        ),
        ("日常记录" * 800 + "深海传感器阈值为三十七度" + "日常记录" * 200, "深海传感器阈值"),
        (
            "routine notes " * 450 + "cobalt calibration measures 37 degrees " + "routine notes " * 200,
            "cobalt calibration",
        ),
    ],
)
def test_full_unicode_json_and_citation_bytes_are_bounded(budget, text, query):
    groups = (candidates(text, scope="scope-海洋"),)
    result = build(query, groups, budget)
    validate(result, groups, budget)
    if budget >= 1200:
        assert result.fragments
        assert any(any(term in fragment.text for term in query.split()) for fragment in result.fragments)


def test_exact_byte_boundary_and_probe_match_actual_delivery():
    groups = (candidates("Cobalt calibration is stable at 37 degrees."),)
    full = build("cobalt calibration", groups, 32768)
    exact_bytes = full.context.content_bytes
    assert exact_bytes >= 512
    exact = build("cobalt calibration", groups, exact_bytes)
    assert exact.context == full.context
    below = build("cobalt calibration", groups, exact_bytes - 1)
    validate(below, groups, exact_bytes - 1)
    assert below.context.content != full.context.content
    for budget in (512, exact_bytes, 1800):
        request = PrepareContextRequest(query="cobalt calibration", max_bytes=budget, evidence_selection=True)
        builder = PreparedContextBuilder()
        delivered = builder.build_scopes_result(request=request, current_scope_id="scope-one", memory_candidates=groups)
        probe = builder.probe_budget(request=request, current_scope_id="scope-one", memory_candidates=groups)
        assert probe.delivered_items == len(delivered.fragments)
        assert probe.unused_bytes == budget - delivered.context.content_bytes
        assert probe.dropped_items == delivered.omissions.dropped_items


def test_default_and_explicit_false_keep_original_prefix_rendering():
    group = candidates("A station records temperature.")
    builder = PreparedContextBuilder()
    implicit = builder.build_scopes_result(
        request=PrepareContextRequest(query="temperature", max_bytes=1200),
        current_scope_id=group.scope_id,
        memory_candidates=(group,),
    )
    explicit = builder.build_scopes_result(
        request=PrepareContextRequest(query="different query", max_bytes=1200, evidence_selection=False),
        current_scope_id=group.scope_id,
        memory_candidates=(group,),
    )
    assert implicit == explicit
    assert implicit.fragments == ()
    assert implicit.context.content is not None
    payload = json.loads(implicit.context.content.splitlines()[-2])
    assert payload == {
        "trust": "untrusted_history",
        "items": [
            {
                "citation": {
                    "memory_ref": group.memory_ref.model_dump(mode="json"),
                    "entry_id": "entry-0",
                    "entry_version_id": "version-0",
                },
                "content": "A station records temperature.",
                "truncated": False,
            }
        ],
    }
    for invalid in ("true", 1, None):
        with pytest.raises(ValidationError):
            PrepareContextRequest.model_validate({"query": "temperature", "evidence_selection": invalid})


def test_invalid_candidates_fail_before_selection_and_duplicate_identities_do_not_repeat():
    group = candidates("A cobalt sensor is calibrated.")
    duplicated = PreparedMemoryCandidates(scope_id=group.scope_id, memory_ref=group.memory_ref, hits=group.hits * 2)
    result = build("cobalt", (duplicated,))
    validate(result, (duplicated,), 1400)
    assert len(result.fragments) == 1
    for invalid in (
        PreparedMemoryCandidates(scope_id=group.scope_id, hits=group.hits),
        PreparedMemoryCandidates(
            scope_id=group.scope_id,
            memory_ref=ArtifactRef(family="memory", artifact_id="different", revision=3),
            hits=group.hits,
        ),
        PreparedMemoryCandidates(scope_id=group.scope_id, memory_ref=group.memory_ref, hits=group.hits * 17),
        PreparedMemoryCandidates(
            scope_id=group.scope_id,
            memory_ref=group.memory_ref,
            hits=(*group.hits, group.hits[0].model_copy(update={"text": "changed body"})),
        ),
    ):
        with pytest.raises(PreparedContextInvariantError):
            build("cobalt", (invalid,))
    assert build("cobalt", ()).fragments == ()


def test_identical_text_in_distinct_scopes_keeps_exact_native_citations_after_restart(tmp_path):
    async def scenario():
        from powercontext.builtin.persistence.sqlite import SQLiteConfig

        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'selection.db'}"))
        groups = []
        async with open_builtin_runtime(config) as runtime:
            assert runtime.scopes is not None
            for index in range(2):
                scope = (
                    await runtime.scopes.create(
                        ScopeDraft(title="Evidence", summary="Independent scope", idempotency_key=f"scope-{index}")
                    )
                ).scope_id
                memory = runtime.memory.for_scope(scope)
                await memory.remember(
                    RememberMemoryRequest(
                        entries=(
                            MemoryEntryInput(kind="document", text="Shared cobalt sensor calibrates at 37 degrees."),
                        )
                    )
                )
                page = await memory.search(SearchMemoryRequest(query="cobalt sensor", mode="fts", limit=16))
                groups.append(PreparedMemoryCandidates(scope_id=scope, memory_ref=page.memory_ref, hits=page.hits))
        async with open_builtin_runtime(config) as runtime:
            result = build("cobalt sensor", groups, 1800)
            validate(result, groups, 1800)
            assert {fragment.address.memory.scope_id for fragment in result.fragments} == {
                group.scope_id for group in groups
            }
            for fragment in result.fragments:
                address = fragment.address
                citation = MemoryCitation(
                    memory_ref=address.memory.artifact,
                    entry_id=address.entry_id,
                    entry_version_id=address.entry_version_id,
                )
                entry = await runtime.memory.for_scope(address.memory.scope_id).get(
                    GetMemoryEntryRequest(citation=citation)
                )
                assert fragment.text == entry.entry.text[fragment.start : fragment.end]

    asyncio.run(scenario())


def test_unicode_line_separators_are_literal_evidence_inside_single_line_json():
    text = 'Sensor "evidence" contains a NEL\x85and a line separator\u2028and paragraph separator\u2029before calibration ends.'
    groups = (candidates(text),)
    result = build("sensor evidence calibration", groups, 32768)
    validate(result, groups, 32768)
    assert len(result.fragments) == 1
    assert result.fragments[0].text == text
    assert result.context.content is not None
    assert (
        "\\u0085" in result.context.content
        and "\\u2028" in result.context.content
        and "\\u2029" in result.context.content
    )
