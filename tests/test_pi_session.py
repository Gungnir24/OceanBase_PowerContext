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
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiEventSource,
    PiSessionInput,
    parse_pi_session,
)


def _archive(records: list[dict[str, Any]], leaf: str | None = None, session_id: str = "session") -> PiSessionInput:
    header = {"type": "session", "version": 3, "id": session_id}
    return PiSessionInput(
        content="\n".join(json.dumps(row, ensure_ascii=False) for row in [header, *records]).encode("utf-8"),
        leaf_id=leaf,
    )


def _message(event_id: str, parent: str | None = None, content: str = "visible") -> dict[str, Any]:
    return {"type": "message", "id": event_id, "parentId": parent, "message": {"role": "user", "content": content}}


@pytest.mark.parametrize(
    "raw",
    [
        b" ",
        b"{}",
        b'{"type":"session","version":3,"id":""}',
        b'{"type":"session","version":3,"id":5}',
        b'{"type":"session","version":3.0,"id":"session"}',
        b'{"type":"session","version":true,"id":"session"}',
        b'{"type":"session","version":3,"id":"session"}\nnull',
        b'{"type":"session","version":3,"id":"session"}\n[]',
        b'{"type":"session","version":3,"id":"session"}\n{"bad":NaN}',
        b'{"type":"session","version":3,"id":"session"}\n{"bad":Infinity}',
        b'{"type":"session","version":3,"id":"session"}\n{"bad":-Infinity}',
        b'{"type":"session","version":3,"id":"session"}\n{"bad":',
        b'{"type":"session","version":3,"id":"session"}\n\xed\xa0\x80',
    ],
)
def test_malformed_archives_fail_with_value_error(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


@pytest.mark.parametrize(
    "bad_record",
    [
        {"type": "model_change", "id": "root", "parentId": None},
        {"type": "model_change", "id": "later"},
        {"type": "model_change", "id": "later", "parentId": "future"},
        {"type": "model_change", "id": "later", "parentId": "later"},
        {"type": "model_change", "id": "later", "parentId": 0},
        {"type": "model_change", "id": "later", "parentId": []},
        {"type": "model_change", "id": "", "parentId": None},
        {"type": "model_change", "id": 12, "parentId": None},
        {"id": "later", "parentId": None},
        {"type": "session", "version": 3, "id": "another", "parentId": None},
    ],
)
def test_invalid_unselected_nodes_are_validated_before_returning_events(bad_record: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(_archive([_message("root"), bad_record], "root"))


def test_header_only_and_control_leaf_preserve_branch_connections() -> None:
    assert parse_pi_session(_archive([])) == ()
    with pytest.raises(ValueError):
        parse_pi_session(_archive([], "missing"))
    records = [
        _message("root", content="Original evidence"),
        _message("left", "root", "Left"),
        {"type": "model_change", "id": "control", "parentId": "root"},
        {"type": "compaction", "id": "compact", "parentId": "control", "summary": "Summary"},
        {"type": "context_edit", "id": "edit", "parentId": "compact", "targetId": "root", "replacement": None},
        {"type": "branch_summary", "id": "summary", "parentId": "edit", "summary": "Branch"},
        {"type": "label", "id": "end", "parentId": "summary", "targetId": "root"},
    ]
    events = parse_pi_session(_archive(records))
    assert [event.event_id for event in events] == ["root", "compact", "summary"]
    assert [event.content for event in events] == [
        "user: Original evidence",
        "compaction: Summary",
        "branch_summary: Branch",
    ]
    assert events[1].parent_id == "control"
    assert parse_pi_session(_archive(records, "left"))[-1].content == "user: Left"
    assert parse_pi_session(_archive([*records, _message("new-root", content="Separate")]))[0].event_id == "new-root"


def test_text_blocks_and_tool_arguments_preserve_unicode_and_whitespace() -> None:
    record = {
        "type": "message",
        "id": "call",
        "parentId": None,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "  第一行\r\n\u2028next  "},
                {"type": "thinking", "thinking": "not evidence"},
                {"type": "text", "text": ""},
                {"type": "image", "data": "not text"},
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "read",
                    "arguments": {"z": 0, "a": {"雪": "西安", "b": 2}},
                },
                {"type": "text", "text": "  final  "},
            ],
        },
        "details": {"arbitrary": ["retained", False, None, 1.25]},
    }
    value = _archive([record])
    events = parse_pi_session(value.model_copy(update={"content": b"\xef\xbb\xbf" + value.content}))
    assert len(events) == 1
    assert events[0].content == (
        'assistant:   第一行\r\n\u2028next  \n\n[tool_call read call-1] {"a":{"b":2,"雪":"西安"},"z":0}\n  final  '
    )
    assert events[0].record == record


def test_only_events_without_visible_body_are_omitted() -> None:
    records = [
        _message("root", content="Before"),
        {
            "type": "message",
            "id": "image",
            "parentId": "root",
            "message": {"role": "user", "content": [{"type": "image"}]},
        },
        {
            "type": "message",
            "id": "thinking",
            "parentId": "image",
            "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden"}]},
        },
        _message("blank", "thinking", " \t\r\n"),
        {"type": "custom", "id": "control", "parentId": "blank", "data": {"text": "state"}},
        {
            "type": "custom_message",
            "id": "custom",
            "parentId": "control",
            "content": [{"type": "image"}, {"type": "text", "text": " Original "}],
        },
    ]
    events = parse_pi_session(_archive(records))
    assert [event.event_id for event in events] == ["root", "custom"]
    assert events[1].content == "custom:  Original "
    assert events[1].parent_id == "control"


def test_identity_is_unambiguous_bounded_and_independent_of_payload_edits() -> None:
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    pairs = [("a:b", "c"), ("a", "b:c"), ("西安" * 300, "event" * 300), ("/", "%2F"), ("%2F", "/")]
    names = []
    for session_id, event_id in pairs:
        event = parse_pi_session(_archive([_message(event_id)], session_id=session_id))[0]
        first = asyncio.run(registry.resolve(event))
        second = asyncio.run(registry.resolve(event.model_copy(update={"content": "changed"})))
        assert first.name == second.name
        assert len(first.name) <= 256
        names.append(first.name)
    assert len(set(names)) == len(pairs)


def test_complete_event_survives_definition_read_schema_and_transport() -> None:
    record = _message("event", content="Original\n" + "完整工具结果" * 5000)
    event = parse_pi_session(_archive([record]))[0]
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(event))
    assert isinstance(source, PiEventSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert asyncio.run(registry.read(PiEventSource.model_validate_json(source.model_dump_json()))) == event
    observation = project_source_for_transport(registry, source)
    manifest = manifest_for_definition(PI_EVENT_SOURCE_DEFINITION)
    assert (manifest.name, manifest.version) == ("pi-event", "1")
    assert PI_EVENT_SOURCE_DEFINITION.input_class is PiEventInput
    Draft202012Validator(manifest.source_schema).validate(observation.payload)
    evidence = TextEvidence.model_validate(observation.projection(TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_id == source.name
    assert evidence.content == event.content
    assert evidence.metadata == {
        "session_id": event.session_id,
        "event_id": event.event_id,
        "parent_id": None,
        "event_type": "message",
        "role": "user",
    }
    assert observation.payload["record"] == record
