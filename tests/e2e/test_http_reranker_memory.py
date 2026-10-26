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

import httpx

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts

SCORES_BY_PROJECT = {"alpha": 1.0, "beta": 2.0, "gamma": 3.0}


def test_native_memory_search_uses_http_scores_and_keeps_identity() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        scores = [
            {"rank": item["rank"], "score": SCORES_BY_PROJECT[item["text"].split()[1]]} for item in body["candidates"]
        ]
        return httpx.Response(200, json={"scores": scores})

    async def scenario() -> object:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=1)
            async with open_builtin_contexts(
                BuiltinConfig(database=SQLiteConfig()), memory_reranker=reranker
            ) as contexts:
                service = (await contexts.get("http-reranker-e2e")).artifacts.memory
                memory = await service.remember(
                    memory=None,
                    entries=(
                        MemoryEntryInput(kind="fact", text="Project alpha fact."),
                        MemoryEntryInput(kind="fact", text="Project beta fact."),
                        MemoryEntryInput(kind="fact", text="Project gamma fact."),
                    ),
                    mode="append",
                )
                return await service.search("project", memories=(memory,), limit=2, mode="fts")

    result = asyncio.run(scenario())
    assert result.rerank is not None
    trace = result.rerank
    pool = trace.candidate_hits
    assert len(pool) == 3
    assert trace.policy_id == "http-cross-encoder-v1"
    assert trace.usage.requests == 3
    assert trace.discarded_rank_count == 0
    assert trace.used_fallback is False
    assert result.generation_calls == 1
    ordered_texts = tuple(pool[rank - 1].text for rank in trace.selected_ranks)
    assert ordered_texts == ("Project gamma fact.", "Project beta fact.")
    assert tuple(hit.text for hit in result.hits) == ordered_texts
    for hit, rank in zip(result.hits, trace.selected_ranks, strict=True):
        candidate = pool[rank - 1]
        assert hit.entry_id == candidate.entry_id
        assert hit.entry_version_id == candidate.entry_version_id
        assert hit.memory_ref == candidate.memory_ref
    assert len(requests) == 3
    assert sorted(item["rank"] for request in requests for item in request["candidates"]) == [1, 2, 3]
    for request in requests:
        assert request["query"] == "project"
        assert len(request["candidates"]) == 1
