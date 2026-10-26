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
import gc
from typing import Any

import pytest
from typing_extensions import override

from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage, embed_query
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class MeteredModel:
    profile = EmbeddingProfile(profile_id="coalescing-edge-cases", model="offline", dimension=1, normalization="none")

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        self.calls.append(texts)
        return self.result(texts)

    @staticmethod
    def result(texts: tuple[str, ...]) -> EmbeddingResult:
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=3, input_tokens=sum(len(text.encode()) for text in texts), output_tokens=0),
        )


@pytest.mark.parametrize("window", [0, 5])
def test_multiple_batches_preserve_per_caller_usage_and_exact_text_identity(window: float) -> None:
    async def scenario() -> None:
        base = MeteredModel()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=window, cache_size=2)
        assert model.profile is base.profile
        inputs = (("A", "a", "A", " a "), ("a", "雪", ""), ("雪", "A"))
        results = await asyncio.gather(*(model.embed(texts) for texts in inputs))
        assert sorted(text for call in base.calls for text in call) == sorted({
            text for texts in inputs for text in texts
        })
        assert len(base.calls) == 3
        assert all(len(call) <= 2 for call in base.calls)
        for texts, result in zip(inputs, results, strict=True):
            assert result.vectors == tuple((float(len(text)),) for text in texts)
            waited = [call for call in base.calls if set(call) & set(texts)]
            assert result.usage.requests == 3 * len(waited)
            assert result.usage.input_tokens == sum(base.result(call).usage.input_tokens or 0 for call in waited)
            assert result.usage.output_tokens == 0

    asyncio.run(scenario())


def test_late_waiters_share_running_work_with_cache_disabled_and_one_caller_cancelled() -> None:
    class Blocked(MeteredModel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.second_started = asyncio.Event()
            self.release = asyncio.Event()

        @override
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            self.started.set()
            if len(self.calls) == 2:
                self.second_started.set()
            await self.release.wait()
            return self.result(texts)

    async def scenario() -> None:
        base = Blocked()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0, cache_size=0)
        cancelled = asyncio.create_task(model.embed(("alpha", "beta", "alpha")))
        await base.started.wait()
        second = asyncio.create_task(model.embed(("beta", "gamma")))
        await base.second_started.wait()
        third = asyncio.create_task(model.embed(("beta", "beta")))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.release.set()
        result, shared = await asyncio.gather(second, third)
        assert result.vectors == ((4.0,), (5.0,))
        assert shared.vectors == ((4.0,), (4.0,))
        assert result.usage.requests == 6 and result.usage.input_tokens == 14
        assert shared.usage.requests == 3 and shared.usage.input_tokens == 9
        assert sorted(text for call in base.calls for text in call) == ["alpha", "beta", "gamma"]
        assert (await model.embed(("beta",))).usage.requests == 3
        assert len(base.calls) == 3

    asyncio.run(scenario())


def test_cache_hits_and_duplicate_positions_refresh_lru_without_recharging_usage() -> None:
    async def scenario() -> None:
        base = MeteredModel()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        await model.embed(("a", "b", "a"))
        await model.embed(("c",))
        assert (await model.embed(("a", "a"))).usage.requests == 0
        await model.embed(("b",))
        assert base.calls == [("a", "b"), ("c",), ("b",)]
        mixed = await model.embed(("b", "new", "new"))
        assert mixed.vectors == ((1.0,), (3.0,), (3.0,))
        assert mixed.usage.requests == 3 and mixed.usage.input_tokens == 3
        assert (await model.embed(("new",))).usage.requests == 0

    asyncio.run(scenario())


def test_failed_batch_does_not_cancel_independent_work_or_cache_the_failure() -> None:
    class Partial(MeteredModel):
        def __init__(self) -> None:
            super().__init__()
            self.good_started = asyncio.Event()
            self.fail = asyncio.Event()
            self.release_good = asyncio.Event()

        @override
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            if texts == ("bad",) and self.calls.count(texts) == 1:
                await self.fail.wait()
                raise RuntimeError("Temporary batch failure")  # noqa: TRY003
            if texts == ("good",):
                self.good_started.set()
                await self.release_good.wait()
            return self.result(texts)

    async def scenario() -> None:
        base = Partial()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        mixed = asyncio.create_task(model.embed(("bad", "good")))
        await base.good_started.wait()
        independent = asyncio.create_task(model.embed(("good",)))
        await asyncio.sleep(0)
        base.fail.set()
        with pytest.raises(RuntimeError, match="Temporary batch failure"):
            await asyncio.wait_for(mixed, timeout=2)
        assert not independent.done()
        base.release_good.set()
        assert (await independent).vectors == ((4.0,),)
        assert (await model.embed(("good",))).usage.requests == 0
        assert (await model.embed(("bad",))).vectors == ((3.0,),)
        assert base.calls.count(("good",)) == 1
        assert base.calls.count(("bad",)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("difference", [-1, 1])
def test_wrong_vector_count_fails_all_dependents_atomically_then_retries(difference: int) -> None:
    class WrongCount(MeteredModel):
        @override
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            if len(self.calls) == 1:
                return EmbeddingResult(vectors=tuple((1.0,) for _ in range(len(texts) + difference)))
            return self.result(texts)

    async def scenario() -> None:
        base = WrongCount()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=0)
        errors = await asyncio.gather(model.embed(("alpha",)), model.embed(("beta",)), return_exceptions=True)
        assert all(isinstance(error, ValueError) for error in errors)
        assert (await model.embed(("alpha", "beta"))).vectors == ((5.0,), (4.0,))
        assert len(base.calls) == 2
        assert (await model.embed(("alpha", "beta"))).usage.requests == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("fails", [False, True])
def test_cancelling_all_waiters_does_not_leave_unretrieved_provider_errors(fails: bool) -> None:
    class Abandoned(MeteredModel):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = asyncio.Event()

        @override
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.calls.append(texts)
            self.started.set()
            await self.release.wait()
            self.finished.set()
            if fails:
                raise RuntimeError("Abandoned provider failure")  # noqa: TRY003
            return self.result(texts)

    async def scenario() -> None:
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
        base = Abandoned()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        task = asyncio.create_task(model.embed(("orphan",)))
        await base.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        base.release.set()
        await base.finished.wait()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
        if not fails:
            assert (await model.embed(("orphan",))).usage.requests == 0
            assert len(base.calls) == 1

    asyncio.run(scenario())


def test_query_embeddings_keep_provider_direction_and_symmetric_models_share_cache() -> None:
    class Directional(MeteredModel):
        def __init__(self) -> None:
            super().__init__()
            self.query_calls: list[tuple[str, ...]] = []

        async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            self.query_calls.append(texts)
            return EmbeddingResult(
                vectors=tuple((-float(len(text)),) for text in texts), usage=InferenceUsage(requests=2)
            )

    async def scenario() -> None:
        base = Directional()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        document, query = await asyncio.gather(model.embed(("same",)), embed_query(model, ("same",)))
        assert document.vectors == ((4.0,),) and query.vectors == ((-4.0,),)
        assert len(base.calls) == len(base.query_calls) == 1
        assert (await model.embed(("same",))).usage.requests == 0
        assert (await embed_query(model, ("same",))).usage.requests == 0
        symmetric = MeteredModel()
        wrapper = CoalescingEmbeddingModel(symmetric, coalesce_ms=0)
        await wrapper.embed(("same",))
        assert (await embed_query(wrapper, ("same",))).usage.requests == 0
        assert len(symmetric.calls) == 1

    asyncio.run(scenario())


def test_unknown_token_counts_remain_unknown_when_batches_are_combined() -> None:
    class Incomplete(MeteredModel):
        @override
        async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
            return EmbeddingResult(
                vectors=((1.0,),),
                usage=InferenceUsage(requests=2, input_tokens=7 if texts[0] == "known" else None, output_tokens=0),
            )

    async def scenario() -> None:
        result = await CoalescingEmbeddingModel(Incomplete(), max_batch_size=1, coalesce_ms=0).embed((
            "known",
            "unknown",
        ))
        assert result.usage.requests == 4 and result.usage.input_tokens is None and result.usage.output_tokens == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_batch_size": 0},
        {"max_batch_size": -1},
        {"max_batch_size": 1.5},
        {"max_batch_size": True},
        {"cache_size": -1},
        {"cache_size": 1.5},
        {"cache_size": True},
        {"coalesce_ms": -1},
        {"coalesce_ms": float("nan")},
        {"coalesce_ms": float("inf")},
        {"coalesce_ms": "5"},
        {"coalesce_ms": True},
    ],
)
def test_invalid_configuration_fails_before_scheduling_work(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        CoalescingEmbeddingModel(MeteredModel(), **kwargs)
