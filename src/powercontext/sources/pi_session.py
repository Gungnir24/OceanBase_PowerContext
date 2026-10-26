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

"""Capture original Pi v3 branch history through durable Connector runs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.errors import SourceConflictError
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import SourceDefinition, SourceProjection
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence


class PiSessionInput(BaseModel):
    """Complete UTF-8 JSONL bytes and an optional exact branch endpoint."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible original event, including all of its native record fields."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """An immutable captured event independent of the source session file."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


def _nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")  # noqa: TRY003
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")  # noqa: TRY003


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_identity(session_id: str, event_id: str) -> str:
    # A JSON pair has no delimiter ambiguity and remains bounded for long native IDs.
    pair = (_nonempty_string(session_id, "session id"), _nonempty_string(event_id, "event id"))
    return "pi:" + hashlib.sha256(_canonical_json(pair).encode("utf-8")).hexdigest()


def _record_digest(record: dict[str, JsonValue]) -> str:
    return hashlib.sha256(_canonical_json(record).encode("utf-8")).hexdigest()


def _parse_archive(value: PiSessionInput) -> tuple[str, dict[str, dict[str, JsonValue]]]:
    session_id: str | None = None
    records: dict[str, dict[str, JsonValue]] = {}
    with io.StringIO(value.content.decode("utf-8-sig"), newline="") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line, parse_constant=_reject_constant)
            if not isinstance(record, dict):
                raise ValueError("Pi JSONL records must be objects")  # noqa: TRY003, TRY004
            if session_id is None:
                if record.get("type") != "session" or type(record.get("version")) is not int or record["version"] != 3:
                    raise ValueError("The first Pi record must be a version 3 session header")  # noqa: TRY003
                session_id = _nonempty_string(record.get("id"), "session id")
                continue
            event_id = _nonempty_string(record.get("id"), "event id")
            event_type = _nonempty_string(record.get("type"), "event type")
            if event_type == "session" or event_id in records:
                raise ValueError("Pi event IDs must be unique and the session header must be first")  # noqa: TRY003
            if "parentId" not in record:
                raise ValueError("Pi events must declare parentId")  # noqa: TRY003
            parent = record["parentId"]
            if parent is not None and parent != "" and (not isinstance(parent, str) or parent not in records):
                raise ValueError("Pi event parentId must reference an earlier event")  # noqa: TRY003
            records[event_id] = record
    if session_id is None:
        raise ValueError("Pi session header is missing")  # noqa: TRY003
    return session_id, records


def _visible_text(content: JsonValue, *, tool_calls: bool = False) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(text := block.get("text"), str):
            parts.append(text)
        elif tool_calls and block.get("type") == "toolCall":
            name = _nonempty_string(block.get("name"), "tool name")
            call_id = _nonempty_string(block.get("id"), "tool call id")
            parts.append(f"[tool_call {name} {call_id}] {_canonical_json(block.get('arguments', {}))}")
    return "\n".join(parts)


def _visible_body(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    kind = record["type"]
    if kind == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _visible_text(message.get("content"), tool_calls=True)
        if role == "toolResult" and body.strip():
            tool = _nonempty_string(message.get("toolName"), "tool result name")
            call = _nonempty_string(message.get("toolCallId"), "tool result call id")
            body = f"[tool_result {tool} {call}] {body}"
        elif role == "bashExecution":
            command, output = message.get("command", ""), message.get("output", "")
            if not isinstance(command, str) or not isinstance(output, str):
                raise ValueError("Pi bash commands and outputs must be strings")  # noqa: TRY003
            body = f"$ {command}\n{output}" if command.strip() or output.strip() else ""
    elif kind in {"compaction", "branch_summary"}:
        role = str(kind)
        body = _visible_text(record.get("summary"))
    elif kind == "custom_message":
        role = "custom"
        body = _visible_text(record.get("content"))
    else:
        return None
    return (role, body) if body.strip() else None


def _branch_events(
    session_id: str, records: dict[str, dict[str, JsonValue]], leaf_id: str | None
) -> tuple[tuple[PiEventInput, ...], tuple[str, ...]]:
    if leaf_id is None:
        leaf_id = next(reversed(records), None)
    if leaf_id is None:
        return (), ()
    if leaf_id not in records:
        raise ValueError("The selected Pi leaf does not exist")  # noqa: TRY003
    path: list[str] = []
    current: str | None = leaf_id
    while current:
        path.append(current)
        parent = records[current]["parentId"]
        current = parent if isinstance(parent, str) else None
    path.reverse()
    events: list[PiEventInput] = []
    for event_id in path:
        record = records[event_id]
        visible = _visible_body(record)
        if visible is None:
            continue
        role, body = visible
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=event_id,
                parent_id=cast(str | None, record["parentId"]),
                event_type=cast(str, record["type"]),
                role=role,
                content=f"{role}: {body}",
                record=record,
            )
        )
    return tuple(events), tuple(path)


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Validate the complete archive and return original visible branch events."""

    session_id, records = _parse_archive(value)
    events, _ = _branch_events(session_id, records, value.leaf_id)
    return events


class _PiTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class: type[PiEventSource] = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class _PiEventDefinition:
    name = "pi-event"
    version = "1"
    input_class: type[PiEventInput] = PiEventInput
    source_class: type[PiEventSource] = PiEventSource
    projections: tuple[SourceProjection[PiEventSource], ...] = (_PiTextEvidenceProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = _event_identity(value.session_id, value.event_id)
        return PiEventSource(name=identity, source_id=identity, definition_version=self.version, **value.model_dump())

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = _PiEventDefinition()


class _PiCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    sessions: dict[str, dict[str, str]] = Field(default_factory=dict)


class PiSessionConnector:
    """Submit unseen branch events, leaving checkpoint commits to the lifecycle."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = await asyncio.to_thread(self.path.read_bytes)
        value = PiSessionInput(content=content, leaf_id=self.leaf_id)
        session_id, records = _parse_archive(value)
        events, path = _branch_events(session_id, records, value.leaf_id)
        progress = (
            _PiCheckpoint(version=1) if session.checkpoint is None else _PiCheckpoint.model_validate(session.checkpoint)
        )
        imported = progress.sessions.get(session_id, {})
        for event_id, digest in imported.items():
            if event_id not in records or _record_digest(records[event_id]) != digest:
                raise ValueError("Previously captured Pi history has been rewritten or removed")  # noqa: TRY003
        # Hash the selected path before submitting anything, including its control nodes.
        next_records = imported | {event_id: _record_digest(records[event_id]) for event_id in path}
        for event in events:
            if event.event_id not in imported:
                try:
                    await session.submit(_event_identity(session_id, event.event_id), "pi-event", event)
                except SourceConflictError as error:
                    raise ValueError("A previously persisted Pi event has changed") from error  # noqa: TRY003
        progress.sessions[session_id] = next_records
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=progress.model_dump(mode="json"))


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
