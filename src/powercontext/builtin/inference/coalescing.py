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

"""Share in-flight embedding work and retain successful vectors in a bounded LRU."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel, embed_query

_Key = tuple[bool, str]


@dataclass(frozen=True, slots=True)
class _ResolvedVector:
    vector: EmbeddingVector
    batch: object
    usage: InferenceUsage


def _observe_completion(future: asyncio.Future[_ResolvedVector]) -> None:
    # Every caller may cancel before a provider fails; still retrieve the exception.
    if not future.cancelled():
        future.exception()


def _combined_usage(usages: Iterable[InferenceUsage]) -> InferenceUsage:
    batches = tuple(usages)
    inputs = [usage.input_tokens for usage in batches if usage.input_tokens is not None]
    outputs = [usage.output_tokens for usage in batches if usage.output_tokens is not None]
    return InferenceUsage(
        requests=sum(usage.requests for usage in batches),
        input_tokens=sum(inputs) if batches and len(inputs) == len(batches) else None,
        output_tokens=sum(outputs) if batches and len(outputs) == len(batches) else None,
    )


class CoalescingEmbeddingModel:
    """An opt-in embedding wrapper shared by callers on the same event loop.

    Text identity is exact. Query and document embeddings share work when the
    provider is symmetric, and remain separate for a provider with embed_query.
    Caller cancellation never cancels the shared provider tasks.
    """

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, (int, float)):
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003, TRY004
        if not math.isfinite(coalesce_ms) or coalesce_ms < 0:
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        self.profile = model.profile
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[_Key, EmbeddingVector] = OrderedDict()
        self._inflight: dict[_Key, asyncio.Future[_ResolvedVector]] = {}
        self._pending: dict[_Key, asyncio.Future[_ResolvedVector]] = {}
        self._flush_handle: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self._embed(tuple(texts), query=False)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self._embed(tuple(texts), query=isinstance(self._model, QueryEmbeddingModel))

    async def _embed(self, texts: tuple[str, ...], *, query: bool) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=())
        loop = asyncio.get_running_loop()
        vectors: dict[str, EmbeddingVector] = {}
        waiting: dict[str, asyncio.Future[_ResolvedVector]] = {}
        for text in texts:
            key = (query, text)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                vectors[text] = cached
                continue
            future = self._inflight.get(key)
            if future is None:
                future = loop.create_future()
                future.add_done_callback(_observe_completion)
                self._inflight[key] = self._pending[key] = future
            waiting[text] = future
        if self._pending and self._flush_handle is None:
            self._flush_handle = loop.call_later(self._coalesce_seconds, self._dispatch)

        # Shield each shared Future, rather than letting gather propagate caller cancellation.
        resolved = await asyncio.gather(*(asyncio.shield(future) for future in waiting.values()))
        usage_by_batch: dict[object, InferenceUsage] = {}
        for text, result in zip(waiting, resolved, strict=True):
            vectors[text] = result.vector
            usage_by_batch[result.batch] = result.usage
        ordered = tuple(vectors[text] for text in texts)
        for text, vector in zip(texts, ordered, strict=True):
            self._remember((query, text), vector)
        return EmbeddingResult(vectors=ordered, usage=_combined_usage(usage_by_batch.values()))

    def _remember(self, key: _Key, vector: EmbeddingVector) -> None:
        if not self._cache_size:
            return
        self._cache[key] = vector
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def _dispatch(self) -> None:
        self._flush_handle = None
        pending, self._pending = self._pending, {}
        for query in (False, True):
            items = tuple((key, future) for key, future in pending.items() if key[0] is query)
            for start in range(0, len(items), self._max_batch_size):
                batch = items[start : start + self._max_batch_size]
                task = asyncio.create_task(self._execute_batch(batch))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    async def _execute_batch(self, batch: tuple[tuple[_Key, asyncio.Future[_ResolvedVector]], ...]) -> None:
        texts = tuple(key[1] for key, _ in batch)
        try:
            result = await embed_query(self._model, texts) if batch[0][0][0] else await self._model.embed(texts)
            if len(result.vectors) != len(batch):
                raise ValueError("Embedding vector count does not match the requested batch")  # noqa: TRY003, TRY301
            token = object()
            usage = result.usage.model_copy(deep=True)
            for (key, future), vector in zip(batch, result.vectors, strict=True):
                self._remember(key, vector)
                future.set_result(_ResolvedVector(vector=vector, batch=token, usage=usage))
        except asyncio.CancelledError:
            for _, future in batch:
                future.cancel()
            raise
        except Exception as error:
            for _, future in batch:
                future.set_exception(error)
        finally:
            for key, future in batch:
                if self._inflight.get(key) is future:
                    del self._inflight[key]


__all__ = ["CoalescingEmbeddingModel"]
