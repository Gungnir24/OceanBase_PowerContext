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

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, PrepareContextRequest, open_builtin_runtime
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


class _CandidatePipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="conversation-turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


TURNS = (
    "We should discuss the remote control features for the new product line.",
    "The remote sensor array needs a battery life of at least forty hours.",
    "Marketing wants the remote launch event scheduled before the summer.",
    "Our supplier confirmed the remote module pricing yesterday afternoon.",
)


def test_runtime_prepare_incremental_replays_after_serialization() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_CandidatePipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(
                    title="Incremental pack",
                    summary="Runtime integration",
                    idempotency_key="context-02-incremental-pack-e2e-runtime",
                )
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)

            await sources.capture(CaptureSource(source_id="turn-1", content=TURNS[0], metadata={}))
            while (await memory.flush()).processed:
                pass
            first = await context.prepare_incremental(
                PrepareContextRequest(query="remote", max_bytes=4096)
            )
            assert isinstance(first, ContextPack)
            first_content = b"".join(first.items).decode("utf-8")
            assert first_content

            await sources.capture(CaptureSource(source_id="turn-2", content=TURNS[1], metadata={}))
            while (await memory.flush()).processed:
                pass
            second = await context.prepare_incremental(
                PrepareContextRequest(query="remote", max_bytes=4096), base_pack=first
            )
            assert isinstance(second, ContextPackDelta)
            expected = await context.prepare(PrepareContextRequest(query="remote", max_bytes=4096))

            restored_base = ContextPack.from_bytes(first.to_bytes())
            restored_delta = ContextPackDelta.from_bytes(second.to_bytes())
            rebuilt = apply_context_pack_delta(restored_base, restored_delta)
            assert b"".join(rebuilt.items).decode("utf-8") == expected.content
            assert expected.content != first_content
            assert second.byte_count == len(second.to_bytes())

            stable = await context.prepare_incremental(
                PrepareContextRequest(query="remote", max_bytes=4096), base_pack=rebuilt
            )
            assert isinstance(stable, ContextPackDelta)
            assert stable.prefix_items == len(rebuilt.items)
            assert stable.appended == ()
            assert apply_context_pack_delta(rebuilt, stable).to_bytes() == rebuilt.to_bytes()

            empty_first = await context.prepare_incremental(
                PrepareContextRequest(query="unmatched-zzz-term", max_bytes=4096)
            )
            assert isinstance(empty_first, ContextPack)
            assert empty_first.items == ()

            empty_delta = await context.prepare_incremental(
                PrepareContextRequest(query="unmatched-zzz-term", max_bytes=4096), base_pack=rebuilt
            )
            assert isinstance(empty_delta, ContextPackDelta)
            assert empty_delta.prefix_items == 0
            assert empty_delta.appended == ()
            restored_empty = apply_context_pack_delta(rebuilt, empty_delta)
            assert b"".join(restored_empty.items) == b""

            await sources.capture(CaptureSource(source_id="turn-3", content=TURNS[2], metadata={}))
            while (await memory.flush()).processed:
                pass
            third = await context.prepare_incremental(
                PrepareContextRequest(query="marketing", max_bytes=4096), base_pack=rebuilt
            )
            assert isinstance(third, ContextPackDelta)
            expected_third = await context.prepare(PrepareContextRequest(query="marketing", max_bytes=4096))
            rebuilt_third = apply_context_pack_delta(rebuilt, ContextPackDelta.from_bytes(third.to_bytes()))
            assert b"".join(rebuilt_third.items).decode("utf-8") == expected_third.content
            assert expected_third.content != expected.content

    asyncio.run(scenario())
