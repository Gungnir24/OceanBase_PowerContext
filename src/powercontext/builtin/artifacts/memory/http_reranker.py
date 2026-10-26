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

"""Batched HTTP score-service reranking for coarse Memory candidates."""

from __future__ import annotations

import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision, _validate_bounds
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Select Memory hits by scoring the full candidate pool through an HTTP service.

    Candidates are sent in bounded batches while keeping their original one-based
    global ranks, so the service scores always map back to the exact input records.
    Transport failures stay httpx errors; malformed payloads raise ``ValueError``.
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_DEFAULT_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("rerank batch size must be at least one")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate exactly once and select the globally best ranks."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            stop = min(start + self._batch_size, len(candidates))
            batch_ranks = tuple(range(start + 1, stop + 1))
            response = await self._client.post(
                self._endpoint,
                json={
                    "query": query,
                    "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in batch_ranks],
                },
            )
            requests += 1
            response.raise_for_status()
            batch_scores = _parse_score_response(response)
            if set(batch_scores) != set(batch_ranks):
                raise ValueError("rerank service scores must cover exactly the requested batch ranks")  # noqa: TRY003
            scores.update(batch_scores)
        ranked = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ranked[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _parse_score_response(response: httpx.Response) -> dict[int, float]:
    """Validate one score response and map its records by rank."""

    try:
        payload = response.json()
    except ValueError as error:
        raise ValueError("rerank service response is not valid JSON") from error  # noqa: TRY003
    if type(payload) is not dict or set(payload) != {"scores"}:
        raise ValueError("rerank service response must contain exactly 'scores'")  # noqa: TRY003
    records = payload["scores"]
    if type(records) is not list:
        raise ValueError("rerank service 'scores' must be a list")  # noqa: TRY003
    scores: dict[int, float] = {}
    for record in records:
        if type(record) is not dict or set(record) != {"rank", "score"}:
            raise ValueError("rerank service score records must contain exactly 'rank' and 'score'")  # noqa: TRY003
        rank = record["rank"]
        score = record["score"]
        if type(rank) is not int:
            raise ValueError("rerank service score 'rank' must be an integer")  # noqa: TRY003
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("rerank service score 'score' must be a finite number")  # noqa: TRY003
        if rank in scores:
            raise ValueError("rerank service score 'rank' must appear exactly once")  # noqa: TRY003
        scores[rank] = float(score)
    return scores


__all__ = [
    "HTTP_RERANK_DEFAULT_POLICY_ID",
    "HTTPMemoryReranker",
]
