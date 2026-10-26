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

"""Connection-boundary checks; native EXPLAIN needs the Linux acceptance suite."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import (
    CapabilityNotSupportedError,
    EmbeddingProfile,
    MemorySearchRequest,
)
from powercontext.builtin.artifacts.memory.fusion import fuse_rankings
from powercontext.builtin.persistence.oceanbase.memory_index import OceanBaseMemoryVectorIndex
from powercontext.builtin.persistence.seekdb.memory_index import SeekDBMemoryHybridIndex
from powercontext.builtin.tags import TagFilter

PROFILE = EmbeddingProfile(profile_id="single-sql", model="offline", dimension=3, normalization="unit")
CURRENT = ArtifactRef(family="memory", artifact_id="memory-one", revision=7)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters or {}))
        return _Rows(self.rows)


@pytest.fixture
def complete(monkeypatch):
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(OceanBaseMemoryVectorIndex, "vector_complete", check)
    return check


def request(**changes):
    values = {
        "query": "alpha",
        "analyzed_query": "alpha",
        "memories": (CURRENT,),
        "candidate_limit": 8,
        "mode": "hybrid",
        "query_vector": (1.0, 0.0, 0.0),
        "embedding_profile": PROFILE,
    }
    values.update(changes)
    return MemorySearchRequest.model_validate(values)


def row(entry="shared", **changes):
    values = {
        "channel": "fts",
        "memory_artifact_id": CURRENT.artifact_id,
        "head_revision": 1,
        "entry_id": entry,
        "entry_version_id": f"version-{entry}",
        "text": f"Original body of {entry}.",
        "fts_score": 1.0,
        "distance": None,
    }
    values.update(changes)
    return values


def test_one_statement_keeps_two_rankings_and_current_references_for_old_projections(complete):
    async def scenario():
        rows = [
            row(),
            row("lexical"),
            row(channel="vector", distance=0.0),
            row("semantic", channel="vector", distance=0.4),
        ]
        connection = _Connection(rows)
        index = SeekDBMemoryHybridIndex(PROFILE)
        result = await index.search(cast(AsyncConnection, connection), "scope-one", request())
        assert len(connection.calls) == 1
        assert [hit.entry_id for hit in result.fts] == ["shared", "lexical"]
        assert [hit.entry_id for hit in result.vector] == ["shared", "semantic"]
        for hit in (*result.fts, *result.vector):
            assert hit.memory_ref == CURRENT
            assert hit.entry_version_id == f"version-{hit.entry_id}"
            assert hit.text == f"Original body of {hit.entry_id}."
        fused = fuse_rankings(fts=result.fts, vector=result.vector, limit=10)
        assert len({(hit.memory_ref.artifact_id, hit.entry_id, hit.entry_version_id) for hit in fused}) == len(fused)
        assert fused[0].entry_id == "shared" and fused[0].matched_by == ("fts", "vector")
        statement, parameters = connection.calls[0]
        sql = str(statement)
        assert "UNION ALL" in sql
        assert "MATCH(m.searchable_text) AGAINST(:analyzed_query)" in sql
        assert "ORDER BY l2_distance(m.embedding, :query_vector) APPROXIMATE" in sql
        assert sql.count("LIMIT :candidate_limit") == 2
        assert sql.count("m.scope_id = :scope_id") == 2
        assert sql.count("m.memory_artifact_id IN") == 2
        assert "m.head_revision =" not in sql
        assert parameters["memory_artifact_ids"] == (CURRENT.artifact_id,)
        assert parameters["scope_id"] == "scope-one" and parameters["candidate_limit"] == 8
        assert parameters["query_vector"] == (1.0, 0.0, 0.0)
        repeated = await index.search(cast(AsyncConnection, connection), "scope-one", request())
        assert repeated == result

    asyncio.run(scenario())


@pytest.mark.parametrize("match,minimum", [("any", 1), ("all", 2)])
def test_tags_are_parameterized_in_each_branch_before_the_limit(complete, match, minimum):
    async def scenario():
        tag_filter = TagFilter(tags=("selected", "论文"), match=match)
        connection = _Connection([])
        await SeekDBMemoryHybridIndex(PROFILE).search(
            cast(AsyncConnection, connection), "scope-one", request(tag_filter=tag_filter)
        )
        assert len(connection.calls) == 1
        statement, parameters = connection.calls[0]
        fts_branch, vector_branch = str(statement).split("UNION ALL")
        for branch in (fts_branch, vector_branch):
            assert branch.index("pc_artifact_tags") < branch.index("LIMIT :candidate_limit")
            assert "tags.scope_id = m.scope_id" in branch
            assert "tags.artifact_id = m.memory_artifact_id" in branch
            assert "tags.target_id = m.entry_id" in branch
        assert parameters["tag_minimum"] == minimum
        assert parameters["tag_keys"] == tag_filter.keys
        assert len(parameters["tag_hashes"]) == 2
        compiled = statement.params(**parameters).compile(
            dialect=dialect(), compile_kwargs={"render_postcompile": True}
        )
        assert "论文" not in str(compiled)
        assert all(key in compiled.params.values() for key in tag_filter.keys)

    asyncio.run(scenario())


def test_query_scope_and_multiple_artifacts_use_bound_values_not_sql_interpolation(complete):
    async def scenario():
        query = "alpha'); SELECT 'injected' --"
        scope = "scope'); SELECT 2 --"
        second = ArtifactRef(family="memory", artifact_id="memory'); SELECT 1 --", revision=9)
        connection = _Connection([])
        await SeekDBMemoryHybridIndex(PROFILE).search(
            cast(AsyncConnection, connection), scope, request(analyzed_query=query, memories=(CURRENT, second))
        )
        statement, parameters = connection.calls[0]
        compiled = statement.params(**parameters).compile(
            dialect=dialect(), compile_kwargs={"render_postcompile": True}
        )
        sql = str(compiled)
        assert query not in sql and scope not in sql and second.artifact_id not in sql
        assert compiled.params["analyzed_query"] == query
        assert compiled.params["scope_id"] == scope
        assert CURRENT.artifact_id in compiled.params.values() and second.artifact_id in compiled.params.values()
        processor = compiled.binds["query_vector"].type.bind_processor(dialect())
        assert processor is not None and json.loads(processor(parameters["query_vector"])) == [1.0, 0.0, 0.0]

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["fts", "vector"])
def test_single_channel_queries_still_use_the_original_index_statement(complete, mode):
    async def scenario():
        connection = _Connection([row(distance=0.0 if mode == "vector" else None)])
        result = await SeekDBMemoryHybridIndex(PROFILE).search(
            cast(AsyncConnection, connection), "scope-one", request(mode=mode)
        )
        assert len(connection.calls) == 1
        statement = str(connection.calls[0][0].compile(dialect=dialect()))
        assert "UNION ALL" not in statement
        if mode == "fts":
            assert result.fts and not result.vector
            assert "MATCH" in statement and "pc_memory_vector_entries" not in statement
        else:
            assert result.vector and not result.fts
            assert "l2_distance" in statement and "AGAINST" not in statement

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"query_vector": None},
        {"embedding_profile": PROFILE.model_copy(update={"profile_id": "different"})},
        {"query_vector": (1.0, 0.0)},
        {"query_vector": (float("nan"), 0.0, 0.0)},
    ],
)
def test_invalid_embeddings_do_not_execute_candidate_sql(complete, changes):
    async def scenario():
        connection = _Connection([])
        with pytest.raises((CapabilityNotSupportedError, ValueError)):
            await SeekDBMemoryHybridIndex(PROFILE).search(
                cast(AsyncConnection, connection), "scope-one", request(**changes)
            )
        assert not connection.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("stale", [False, True])
def test_existing_vector_completeness_and_stale_head_failures_are_preserved(complete, stale):
    async def scenario():
        if stale:
            complete.side_effect = CapabilityNotSupportedError("head")
        else:
            complete.return_value = False
        connection = _Connection([])
        with pytest.raises(CapabilityNotSupportedError):
            await SeekDBMemoryHybridIndex(PROFILE).search(cast(AsyncConnection, connection), "scope-one", request())
        assert not connection.calls

    asyncio.run(scenario())
