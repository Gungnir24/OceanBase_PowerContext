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

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.service import MemoryService
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    ContextAssembly,
    ContextAssemblySection,
    GetMemoryEntryRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
    SearchTopicMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import SourceLifecycleError, SourceLifecycleEvent
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import SourceRef


def event(node="root", sequence=1, available=False, reason="withdrawn"):
    return SourceLifecycleEvent(node_id=node, revision=7, sequence=sequence, available=available, reason=reason)


async def build(runtime, title="lifecycle", count=3):
    scope = (
        await runtime.scopes.create(ScopeDraft(title=title, summary="Evidence graph", idempotency_key=title))
    ).scope_id
    lifecycle = runtime.lifecycle.for_scope(scope)
    source = (
        await runtime.sources.for_scope(scope).capture(CaptureSource(source_id="root", content="source", metadata={}))
    ).source_ref
    await lifecycle.register_source("root", source, revision=7)
    citations = []
    for number in range(count):
        created = await runtime.memory.for_scope(scope).remember(
            RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"evidence{number} body"),))
        )
        assert created.entry is not None
        citation = created.entry.citation
        await lifecycle.register_memory(f"m{number}", citation, parents=("root",), policy="all")
        citations.append(citation)
    return scope, lifecycle, source, citations


async def topic(runtime, scope, title):
    created = await runtime.records.for_scope(scope).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": title, "summary": f"{title} summary", "detail": f"{title} detail"}),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_id", ""),
        ("node_id", " \t"),
        ("node_id", 3),
        ("reason", ""),
        ("reason", "\n"),
        ("reason", 3),
        ("revision", 0),
        ("revision", True),
        ("revision", "7"),
        ("revision", 7.0),
        ("sequence", -1),
        ("sequence", False),
        ("sequence", "1"),
        ("available", 0),
        ("available", "false"),
        ("extra", True),
    ],
)
def test_events_reject_coercion_blank_fields_and_extra_fields(field, value):
    payload = event().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        SourceLifecycleEvent.model_validate(payload)


def test_registration_rejects_missing_references_and_conflicting_graph_without_side_effects():
    async def scenario():
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope, lifecycle, source, citations = await build(runtime, count=1)
            head = (await runtime.memory.for_scope(scope).list()).memory_ref
            await lifecycle.register_source("root", source, revision=7)
            await lifecycle.register_memory("m0", citations[0], parents=("root",), policy="all")
            for parents in ((), ("missing",), ("root", "root"), ("new",)):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.register_memory("new", citations[0], parents=parents, policy="any")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("new", citations[0], parents=("root",), policy="unsupported")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source("root", source, revision=8)
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_source(
                    "missing", SourceRef(source_type="content", source_id="missing"), revision=1
                )
            bad_memory = MemoryCitation(
                memory_ref=citations[0].memory_ref, entry_id="missing", entry_version_id="missing"
            )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_memory("missing", bad_memory, parents=("root",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.register_topic(
                    "missing",
                    ArtifactRef(family="topic-memory", artifact_id="missing", revision=1),
                    parents=("root",),
                    policy="all",
                )
            with pytest.raises(SourceLifecycleError):
                await lifecycle.apply(event("m0"))
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("missing", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("root", purpose="unknown")
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == head
            assert await lifecycle.apply(event(available=True)) == ["m0", "root"]

    asyncio.run(scenario())


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_native_writes_resume_after_restart_without_repeating_completed_revisions(
    tmp_path, monkeypatch, after_commit
):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}"))
        original = MemoryService.forget
        calls = 0

        async def failing(self, memory, *, entries, reason=None):
            nonlocal calls
            calls += 1
            if calls == 2 and not after_commit:
                raise RuntimeError("injected-interruption")
            result = await original(self, memory, entries=entries, reason=reason)
            if calls == 2 and after_commit:
                raise RuntimeError("injected-interruption")
            return result

        async with open_builtin_runtime(config) as runtime:
            scope, lifecycle, _, citations = await build(runtime)
            topic_ref = await topic(runtime, scope, "retrytopic")
            await lifecycle.register_topic("t", topic_ref, parents=("m0", "m2"), policy="all")
            before = (await runtime.memory.for_scope(scope).list()).memory_ref
            assert before is not None
            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "forget", failing)
                with pytest.raises(RuntimeError, match="injected-interruption"):
                    await lifecycle.apply(event())
            partial = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert partial is not None
            assert partial.revision == before.revision + (2 if after_commit else 1)
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(event()) == []
            inactive = await runtime.memory.for_scope(scope).list(include_inactive=True)
            assert inactive.memory_ref is not None
            assert inactive.memory_ref.revision == before.revision + 3
            assert {entry.state for entry in inactive.entries} == {"inactive"}
            assert not (
                await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="retrytopic"))
            ).hits
            assert await lifecycle.apply(event()) == []
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == inactive.memory_ref
            for number, citation in enumerate(citations):
                assert await lifecycle.resolve(f"m{number}", purpose="audit") == citation
                assert (
                    await runtime.memory.for_scope(scope).get(GetMemoryEntryRequest(citation=citation))
                ).entry.text == f"evidence{number} body"
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.resolve(f"m{number}", purpose="generation")
            assert await lifecycle.resolve("t", purpose="audit") == topic_ref
            assert await lifecycle.apply(event(sequence=2, available=True, reason="restored")) == [
                "m0",
                "m1",
                "m2",
                "root",
                "t",
            ]
            active = await runtime.memory.for_scope(scope).list()
            assert active.memory_ref is not None
            assert active.memory_ref.revision == before.revision + 6
            assert {entry.entry.entry_id: entry.entry.entry_version_id for entry in active.entries} == {
                citation.entry_id: citation.entry_version_id for citation in citations
            }
            assert await lifecycle.resolve("t", purpose="generation") == topic_ref
            assert (
                await runtime.topic_memory.for_scope(scope).search(SearchTopicMemoryRequest(query="retrytopic"))
            ).hits
            for invalid in (event(), event(sequence=2, available=True, reason="different-reason")):
                with pytest.raises(SourceLifecycleError):
                    await lifecycle.apply(invalid)
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == active.memory_ref

    asyncio.run(scenario())


def test_interrupted_restore_and_registration_can_be_retried_without_body_revisions(tmp_path, monkeypatch):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'restore.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope, lifecycle, _, citations = await build(runtime, count=2)
            await lifecycle.apply(event())
            original = MemoryService.reactivate
            calls = 0

            async def fail_second(self, memory, *, entries, reason=None):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("restore-interrupted")
                return await original(self, memory, entries=entries, reason=reason)

            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "reactivate", fail_second)
                with pytest.raises(RuntimeError, match="restore-interrupted"):
                    await lifecycle.apply(event(sequence=2, available=True))
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope)
            assert await lifecycle.apply(event(sequence=2, available=True)) == ["m0", "m1", "root"]
            active = await runtime.memory.for_scope(scope).list()
            for number, citation in enumerate(citations):
                assert await lifecycle.resolve(f"m{number}", purpose="generation") == citation
            await lifecycle.register_memory("m0", citations[0], parents=("root",), policy="all")
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == active.memory_ref
            await lifecycle.apply(event(sequence=3))
            created = await runtime.memory.for_scope(scope).remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="late interrupted registration"),))
            )
            assert created.entry is not None
            original_forget = MemoryService.forget

            async def interrupted(self, memory, *, entries, reason=None):
                await original_forget(self, memory, entries=entries, reason=reason)
                raise RuntimeError("registration-interrupted")

            with monkeypatch.context() as patch:
                patch.setattr(MemoryService, "forget", interrupted)
                with pytest.raises(RuntimeError, match="registration-interrupted"):
                    await lifecycle.register_memory("late", created.entry.citation, parents=("root",), policy="all")
            before = (await runtime.memory.for_scope(scope).list()).memory_ref
            await lifecycle.register_memory("late", created.entry.citation, parents=("root",), policy="all")
            assert (await runtime.memory.for_scope(scope).list()).memory_ref == before
            assert not (await runtime.memory.for_scope(scope).list()).entries
            assert await lifecycle.resolve("late", purpose="audit") == created.entry.citation
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("late", purpose="generation")

    asyncio.run(scenario())


def test_scope_isolation_and_dependency_paths_through_topics_survive_restart(tmp_path):
    async def scenario():
        config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'scopes.db'}"))
        async with open_builtin_runtime(config) as runtime:
            scope_a, lifecycle, _, citations_a = await build(runtime, "a", count=2)
            scope_b, other, _, citations_b = await build(runtime, "b", count=2)
            ref = await topic(runtime, scope_a, "middle")
            await lifecycle.register_topic("middle", ref, parents=("m0",), policy="any")
            # A derived Memory may depend on a Topic, including through an any edge.
            created = await runtime.memory.for_scope(scope_a).remember(
                RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text="leaf evidence"),))
            )
            assert created.entry is not None
            await lifecycle.register_memory("leaf", created.entry.citation, parents=("middle", "m1"), policy="any")
            await lifecycle.apply(event())
            await other.apply(event(available=True))
        async with open_builtin_runtime(config) as runtime:
            lifecycle = runtime.lifecycle.for_scope(scope_a)
            other = runtime.lifecycle.for_scope(scope_b)
            assert not (await runtime.memory.for_scope(scope_a).list()).entries
            assert len((await runtime.memory.for_scope(scope_b).list()).entries) == 2
            for number, citation in enumerate(citations_b):
                assert await other.resolve(f"m{number}", purpose="generation") == citation
            with pytest.raises(SourceLifecycleError):
                await lifecycle.resolve("leaf", purpose="generation")
            for number, citation in enumerate(citations_a):
                assert await lifecycle.resolve(f"m{number}", purpose="audit") == citation
            await lifecycle.apply(event(sequence=2, available=True))
            assert (await runtime.memory.for_scope(scope_a).search(SearchMemoryRequest(query="leaf", mode="fts"))).hits
            prepared = await runtime.context.for_scope(scope_a).prepare(
                PrepareContextRequest(
                    query="middle",
                    max_bytes=4096,
                    assembly=ContextAssembly(sections=(ContextAssemblySection(family="topic-memory", limit=4),)),
                )
            )
            assert prepared.content is not None and "middle summary" in prepared.content

    asyncio.run(scenario())
