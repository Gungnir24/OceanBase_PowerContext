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
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.artifacts.memory.protocols import MemoryCandidateRequest
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    BuiltinRuntime,
    CommitConnectorCheckpoint,
    GetMemoryEntryRequest,
    SearchMemoryRequest,
    SubmitSourceObservation,
    open_builtin_contexts,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.errors import ConnectorSubmissionRejectedError
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    ConnectorBinding,
    ConnectorRunResult,
    ConnectorRunStatus,
    SourceDefinitionRegistry,
    SourceObservation,
    SourceRef,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)


class _NativeSink:
    def __init__(self, runtime: BuiltinRuntime, *, fail_event: str | None, reject_event: str | None) -> None:
        self.runtime = runtime
        self.registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        self.fail_event = fail_event
        self.reject_event = reject_event

    async def submit(
        self, binding: ConnectorBinding, item_id: str, definition_name: str, value: object, /
    ) -> SourceRef:
        assert isinstance(value, PiEventInput)
        if value.event_id == self.fail_event:
            raise OSError("Simulated source sink failure")  # noqa: TRY003
        if value.event_id == self.reject_event:
            raise ConnectorSubmissionRejectedError("Simulated source rejection")  # noqa: TRY003
        source = await self.registry.resolve(value)
        return (
            await self.runtime.ingestion.submit(
                SubmitSourceObservation(
                    scope_id=binding.scope_id, observation=project_source_for_transport(self.registry, source)
                )
            )
        ).source_ref


class _NativeCheckpoints:
    def __init__(self, runtime: BuiltinRuntime) -> None:
        self.runtime = runtime

    async def load(self, binding: ConnectorBinding, /) -> JsonValue | None:
        return (await self.runtime.ingestion.checkpoint(binding)).checkpoint

    async def save(
        self, binding: ConnectorBinding, checkpoint: JsonValue | None, /, *, expected: JsonValue | None
    ) -> None:
        await self.runtime.ingestion.commit(
            CommitConnectorCheckpoint(binding=binding, checkpoint=checkpoint, expected=expected)
        )


def _config(database: Path) -> BuiltinConfig:
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{database}"))


async def _run(
    database: Path,
    path: Path,
    leaf: str | None = None,
    *,
    fail_event: str | None = None,
    reject_event: str | None = None,
) -> ConnectorRunResult:
    async with open_builtin_runtime(_config(database)) as runtime:
        assert runtime.scopes is not None
        scope = await runtime.scopes.create(
            ScopeDraft(title="Pi history", summary="Evidence", idempotency_key="pi-history")
        )
        binding = ConnectorBinding(
            scope_id=scope.scope_id, binding_id="pi", connector_name="pi-session-history", connector_version="1"
        )
        await runtime.ingestion.register(manifest_for_definition(PI_EVENT_SOURCE_DEFINITION))
        return await ConnectorLifecycle(
            sink=_NativeSink(runtime, fail_event=fail_event, reject_event=reject_event),
            checkpoints=_NativeCheckpoints(runtime),
        ).run(PiSessionConnector(path, leaf), binding)


async def _snapshot(database: Path, binding: ConnectorBinding) -> tuple[JsonValue | None, dict[str, SourceObservation]]:
    async with open_builtin_runtime(_config(database)) as runtime:
        checkpoint = (await runtime.ingestion.checkpoint(binding)).checkpoint
    sources = {}
    async with open_builtin_contexts(_config(database)) as contexts:
        entries = await (await contexts.get(binding.scope_id)).sources.journal.entries()
        for entry in entries:
            assert isinstance(entry.source, SourceObservation)
            sources[entry.source_ref.source_id] = entry.source
    return checkpoint, sources


def _message(event_id: str, parent: str | None, content: str) -> dict[str, Any]:
    return {"type": "message", "id": event_id, "parentId": parent, "message": {"role": "user", "content": content}}


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    header = {"type": "session", "version": 3, "id": "native-tree"}
    path.write_bytes("\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]).encode("utf-8-sig"))


def test_native_checkpoint_survives_rejection_partial_failure_and_off_branch_rewrite(tmp_path: Path) -> None:
    path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
    records: list[dict[str, Any]] = [
        _message("root", None, "Shared evidence"),
        _message("left", "root", "Left evidence"),
        {"type": "model_change", "id": "control", "parentId": "root", "modelId": "local"},
        _message("right", "control", "Right evidence"),
    ]
    _write(path, records)

    async def scenario() -> None:
        first = await _run(database, path, "left")
        before = await _snapshot(database, first.binding)
        rejected = await _run(database, path, "right", reject_event="right")
        assert rejected.status is ConnectorRunStatus.INCOMPLETE
        assert rejected.committed_checkpoint == first.committed_checkpoint
        assert await _snapshot(database, first.binding) == before

        right = await _run(database, path, "right")
        assert len(right.items) == 1
        assert right.previous_checkpoint == first.committed_checkpoint
        records.extend([_message("tail-1", "right", "Tail one"), _message("tail-2", "tail-1", "Tail two")])
        _write(path, records)
        with pytest.raises(OSError, match="sink failure"):
            await _run(database, path, "tail-2", fail_event="tail-2")
        checkpoint, partial = await _snapshot(database, first.binding)
        assert checkpoint == right.committed_checkpoint
        assert len(partial) == 4

        # The accepted first item survives the failed run even though its checkpoint did not advance.
        records[-2]["extra"] = {"changed": True}
        _write(path, records)
        with pytest.raises(ValueError, match="persisted Pi event"):
            await _run(database, path, "tail-2")
        assert await _snapshot(database, first.binding) == (checkpoint, partial)
        del records[-2]["extra"]
        _write(path, records)
        completed = await _run(database, path, "tail-2")
        assert len(completed.items) == 2
        stable = await _snapshot(database, first.binding)
        assert len(stable[1]) == 5
        assert stable[0] == completed.committed_checkpoint

        good_bytes = path.read_bytes()
        path.write_bytes(good_bytes + b'\n{"malformed"')
        with pytest.raises(ValueError):
            await _run(database, path, "tail-2")
        assert await _snapshot(database, first.binding) == stable
        path.write_bytes(good_bytes)

        # The left branch is not selected now, but its captured record is still immutable.
        records[1]["details"] = {"rewrite": "metadata only"}
        _write(path, records)
        with pytest.raises(ValueError, match="history"):
            await _run(database, path, "tail-2")
        assert await _snapshot(database, first.binding) == stable
        del records[1]["details"]
        _write(path, records)
        replay = await _run(database, path, "tail-2")
        assert replay.items == ()
        assert replay.committed_checkpoint == stable[0]

        # Changing JSON whitespace and key order does not change the parsed original record.
        original = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()]
        path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in original), encoding="utf-8")
        assert (await _run(database, path, "tail-2")).items == ()

    asyncio.run(scenario())


class _ChunkedEvidencePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        entries = []
        for source in request.sources:
            assert isinstance(source, SourceObservation)
            evidence = TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY))
            for offset in range(0, len(evidence.content), 1500):
                entries.append(
                    MemoryEntryInput(
                        kind=f"pi-part-{offset}", text=evidence.content[offset : offset + 1500], sources=(source,)
                    )
                )
        return tuple(entries)


def test_full_tool_results_and_every_event_citation_survive_process_restart(tmp_path: Path) -> None:
    path, database = tmp_path / "session.jsonl", tmp_path / "history.db"
    records: list[dict[str, Any]] = [
        _message("root", None, "RootBeacon 原始请求"),
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "assistant",
            "parentId": "control",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "AssistantBeacon"},
                    {
                        "type": "toolCall",
                        "name": "read",
                        "id": "call-1",
                        "arguments": {"token": "CallBeacon", "a": "雪"},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "assistant",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call-1",
                "content": "ResultBeacon " + "完整工具输出\n" * 5000 + " ResultTailBeacon",
                "details": {"bytes": 50000},
            },
        },
        {
            "type": "compaction",
            "id": "compact",
            "parentId": "result",
            "summary": "CompactionBeacon",
            "firstKeptEntryId": "compact",
        },
        {
            "type": "branch_summary",
            "id": "branch",
            "parentId": "compact",
            "summary": "BranchBeacon",
            "fromId": "result",
        },
        {"type": "custom_message", "id": "custom", "parentId": "branch", "content": "CustomBeacon"},
        {
            "type": "message",
            "id": "bash",
            "parentId": "custom",
            "message": {
                "role": "bashExecution",
                "command": "printf ShellBeacon",
                "output": "ShellBeacon",
                "content": [],
            },
        },
    ]
    _write(path, records)
    submitted = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, pathlib, runpy, sys; module = runpy.run_path(sys.argv[1]); result = asyncio.run(module['_run'](pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))); print(result.binding.scope_id)",
            str(Path(__file__).resolve()),
            str(database),
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert submitted.returncode == 0, submitted.stderr
    scope_id = submitted.stdout.strip()
    binding = ConnectorBinding(
        scope_id=scope_id, binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )
    events = parse_pi_session(PiSessionInput(content=path.read_bytes()))
    expected = {event.event_id: event for event in events}

    async def scenario() -> None:
        checkpoint, stored = await _snapshot(database, binding)
        assert checkpoint is not None
        assert len(stored) == len(events) == 7
        for source in stored.values():
            event = PiEventInput.model_validate(source.payload)
            assert event == expected[event.event_id]
            assert TextEvidence.model_validate(source.projection(TEXT_EVIDENCE_PROJECTION_KEY)).content == event.content
        result = expected["result"]
        assert len(result.content.encode("utf-8")) > 8192

        # Replay from a new runtime uses the durable checkpoint and submits no shared events.
        replay = await _run(database, path)
        assert replay.items == () and replay.previous_checkpoint == checkpoint
        async with open_builtin_runtime(_config(database), candidate_pipeline=_ChunkedEvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            flushed = 0
            for _ in range(len(events) + 1):
                outcome = await memory.flush()
                if not outcome.processed:
                    break
                flushed += outcome.source_count
            assert flushed == len(events)

        queries = {
            "root": "RootBeacon",
            "assistant": "AssistantBeacon",
            "result": "ResultTailBeacon",
            "compact": "CompactionBeacon",
            "branch": "BranchBeacon",
            "custom": "CustomBeacon",
            "bash": "ShellBeacon",
        }
        async with open_builtin_runtime(_config(database), candidate_pipeline=_ChunkedEvidencePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope_id)
            for event_id, query in queries.items():
                search = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=100))
                found = False
                for hit in search.hits:
                    citation = MemoryCitation(
                        memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id
                    )
                    entry = await memory.get(GetMemoryEntryRequest(citation=citation))
                    for reference in entry.entry.sources:
                        evidence = TextEvidence.model_validate(
                            stored[reference.source_id].projection(TEXT_EVIDENCE_PROJECTION_KEY)
                        )
                        if evidence.metadata["event_id"] == event_id:
                            assert query in entry.entry.text
                            assert evidence.content == expected[event_id].content
                            found = True
                assert found, f"No exact citation recovered for {event_id}"

    asyncio.run(scenario())
