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
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit

ENDPOINT = "https://reranker.invalid/score"


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"candidate {rank}",
        score=1.0 / rank,
        matched_by=("fts",),
    )


def _echo_transport(scores_by_rank: dict[int, float]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        scores = [{"rank": item["rank"], "score": scores_by_rank[item["rank"]]} for item in body["candidates"]]
        return httpx.Response(200, json={"scores": scores})

    return httpx.MockTransport(handler)


def test_batches_preserve_original_global_ranks_and_count_requests() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200, json={"scores": [{"rank": item["rank"], "score": 0.5} for item in body["candidates"]]}
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=2)
            decision = await reranker.rerank("query", tuple(_hit(i) for i in range(1, 6)), 5)
            assert decision.selected_ranks == (1, 2, 3, 4, 5)
            assert decision.usage.requests == 3
            assert decision.discarded_rank_count == 0
            assert decision.used_fallback is False

    asyncio.run(scenario())
    assert [len(body["candidates"]) for body in seen] == [2, 2, 1]
    assert sorted(item["rank"] for body in seen for item in body["candidates"]) == [1, 2, 3, 4, 5]
    for body in seen:
        assert set(body) == {"query", "candidates"}
        assert body["query"] == "query"
        for item in body["candidates"]:
            assert set(item) == {"rank", "text"}
            assert type(item["rank"]) is int
            assert item["text"] == f"candidate {item['rank']}"


def test_cross_batch_scores_decide_one_stable_global_order() -> None:
    scores_by_rank = {1: 0.10, 2: 0.90, 3: 0.95, 4: 0.90}

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=_echo_transport(scores_by_rank)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=2)
            candidates = tuple(_hit(i) for i in range(1, 5))
            top_two = await reranker.rerank("数据库连接超时怎么排查", candidates, 2)
            assert top_two.selected_ranks == (3, 2)
            assert top_two.usage.requests == 2
            full = await reranker.rerank("数据库连接超时怎么排查", candidates, 4)
            assert full.selected_ranks == (3, 2, 4, 1)
            assert full.usage.requests == 2

    asyncio.run(scenario())


def test_out_of_order_and_int_scores_are_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        scores = [{"rank": item["rank"], "score": item["rank"]} for item in reversed(body["candidates"])]
        return httpx.Response(200, json={"scores": scores})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=2)
            decision = await reranker.rerank("q", tuple(_hit(i) for i in range(1, 4)), 3)
            assert decision.selected_ranks == (3, 2, 1)
            assert decision.usage.requests == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"extra": True},
        {"scores": [], "extra": True},
        {"scores": "no-list"},
        {"scores": {"1": 0.5}},
        {"scores": [None]},
        {"scores": [{}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
        {"scores": [{"rank": 1.0, "score": 0.5}]},
        {"scores": [{"rank": "1", "score": 0.5}]},
        {"scores": [{"rank": True, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": 1, "score": True}]},
        {"scores": [{"rank": 1, "score": None}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1, "score": float("-inf")}]},
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
    ],
)
def test_invalid_response_schemas_and_coverage_fail(payload: dict) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=json.dumps(payload).encode()))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


def test_cross_batch_ranks_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ranks = [item["rank"] for item in body["candidates"]]
        return httpx.Response(200, json={"scores": [{"rank": rank, "score": 0.5} for rank in [*ranks, 3]]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client, batch_size=2)
            with pytest.raises(ValueError):
                await reranker.rerank("q", tuple(_hit(i) for i in range(1, 3)), 2)

    asyncio.run(scenario())


def test_invalid_json_bodies_fail_as_value_errors() -> None:
    async def scenario() -> None:
        for content in (b"", b"not json", b'{"scores": ['):
            transport = httpx.MockTransport(lambda _request, c=content: httpx.Response(200, content=c))
            async with httpx.AsyncClient(transport=transport) as client:
                reranker = HTTPMemoryReranker(ENDPOINT, client=client)
                with pytest.raises(ValueError):
                    await reranker.rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [404, 500, 503])
def test_non_2xx_responses_preserve_httpx_errors(status: int) -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda _request: httpx.Response(status))
        async with httpx.AsyncClient(transport=transport) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client)
            with pytest.raises(httpx.HTTPStatusError):
                await reranker.rerank("q", (_hit(1),), 1)

    asyncio.run(scenario())


def test_network_errors_are_preserved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client)
            with pytest.raises(httpx.ConnectError):
                await reranker.rerank("q", (_hit(1), _hit(2)), 1)

    asyncio.run(scenario())


def test_invalid_bounds_and_batch_sizes_fail_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")  # noqa: TRY003

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker(ENDPOINT, client=client)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 0)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 2)
            with pytest.raises(ValueError):
                HTTPMemoryReranker(ENDPOINT, client=client, batch_size=0)
            with pytest.raises(ValueError):
                HTTPMemoryReranker(ENDPOINT, client=client, batch_size=-3)

    asyncio.run(scenario())


def test_policy_id_defaults_and_overrides() -> None:
    async def scenario() -> None:
        transport = _echo_transport({1: 0.5})
        async with httpx.AsyncClient(transport=transport) as client:
            default = HTTPMemoryReranker(ENDPOINT, client=client)
            assert default.policy_id == "http-cross-encoder-v1"
            assert (await default.rerank("q", (_hit(1),), 1)).selected_ranks == (1,)
            custom = HTTPMemoryReranker(ENDPOINT, client=client, policy_id="custom-cross-encoder-v2")
            assert custom.policy_id == "custom-cross-encoder-v2"
            assert (await custom.rerank("q", (_hit(1),), 1)).selected_ranks == (1,)

    asyncio.run(scenario())
