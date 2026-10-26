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

"""Durable evidence eligibility over a scope's immutable dependency graph."""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.artifact_governance import (
    ArtifactGovernanceRepository,
    ArtifactLifecycleState,
)
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.sources import SourceRepository
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.builtin.records import BaseValueNotFoundError
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceMaterialization, SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Invalid registration, event, or attempted use of withdrawn evidence."""


def _require_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("empty-identity-or-reason")
    return value


class SourceLifecycleEvent(BaseModel):
    """One immutable, strictly typed Source availability transition."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return _require_text(value)


class _Node(BaseModel):
    kind: Literal["source", "memory", "topic"]
    reference: SourceRef | MemoryCitation | ArtifactRef
    revision: int | None = None
    parents: tuple[str, ...] = ()
    policy: Literal["all", "any"] = "all"
    available: bool = True
    event: SourceLifecycleEvent | None = None


class _Graph(BaseModel):
    nodes: dict[str, _Node] = Field(default_factory=dict)

    def propagate(self) -> None:
        try:
            order = tuple(TopologicalSorter({key: node.parents for key, node in self.nodes.items()}).static_order())
        except CycleError as error:
            raise SourceLifecycleError("cyclic-dependencies") from error
        for key in order:
            node = self.nodes[key]
            if node.kind != "source":
                supported = (self.nodes[parent].available for parent in node.parents)
                node.available = all(supported) if node.policy == "all" else any(supported)


class ScopedSourceLifecycle:
    """Reconcile durable desired states with native Memory and Topic eligibility.

    Each operation reads only its scope's graph (16-20 nodes in the contract).
    Desired states and the accepted event are committed before native writes.
    Replays compare actual native state, so a crash can resume without rewriting
    immutable bodies or repeating already completed Memory transitions.
    """

    def __init__(
        self,
        runtime: BuiltinRuntime,
        database: AsyncDatabase | None,
        sources: SourceRepository | None,
        scope_id: str,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> None:
        if type(revision) is not int or revision <= 0 or not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("invalid-source-binding")
        await self._register(node_id, _Node(kind="source", reference=source_ref, revision=revision))

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("invalid-memory-binding")
        await self._register_derived(node_id, "memory", citation, parents, policy)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("invalid-topic-binding")
        await self._register_derived(node_id, "topic", artifact_ref, parents, policy)

    async def _register_derived(
        self,
        node_id: str,
        kind: Literal["memory", "topic"],
        reference: MemoryCitation | ArtifactRef,
        parents: tuple[str, ...],
        policy: Literal["all", "any"],
    ) -> None:
        if not parents or isinstance(parents, str) or policy not in {"all", "any"}:
            raise SourceLifecycleError("invalid-dependencies")
        for parent in parents:
            _require_text(parent)
        if len(set(parents)) != len(parents) or node_id in parents:
            raise SourceLifecycleError("duplicate-or-self-parent")
        await self._register(
            node_id,
            _Node(kind=kind, reference=reference, parents=parents, policy=policy),
        )

    async def _register(self, node_id: str, node: _Node) -> None:
        _require_text(node_id)
        node = node.model_copy(deep=True)
        async with (
            self._runtime._scope_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
        ):
            graph = await self._load()
            existing = graph.nodes.get(node_id)
            if existing is not None:
                excluded = {"available", "event"}
                if existing.model_dump(exclude=excluded) != node.model_dump(exclude=excluded):
                    raise SourceLifecycleError("conflicting-registration")
            if any(parent not in graph.nodes for parent in node.parents):
                raise SourceLifecycleError("missing-parent")
            await self._read_reference(node)
            if existing is None:
                graph.nodes[node_id] = node
                graph.propagate()
                await self._save(graph)
            await self._reconcile(graph, reason="source-lifecycle-registration")

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        event = SourceLifecycleEvent.model_validate(event)
        async with (
            self._runtime._scope_operation(self.scope_id),
            self._runtime._locked(self.scope_id),
        ):
            graph = await self._load()
            node = graph.nodes.get(event.node_id)
            if node is None or node.kind != "source" or node.revision != event.revision:
                raise SourceLifecycleError("unknown-source-or-revision")
            previous = node.event
            if previous is not None and (
                event.sequence < previous.sequence or (event.sequence == previous.sequence and event != previous)
            ):
                raise SourceLifecycleError("conflicting-event-sequence")
            if event != previous:
                node.available = event.available
                node.event = event
                graph.propagate()
                await self._save(graph)
            # Even identical events must finish any interrupted native writes.
            await self._reconcile(graph, reason=event.reason)
            return sorted(key for key, value in graph.nodes.items() if value.available)

    async def resolve(
        self, node_id: str, *, purpose: Literal["generation", "audit"]
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown-purpose")
        async with self._runtime._scope_operation(self.scope_id):
            node = (await self._load()).nodes.get(node_id)
            if node is None:
                raise SourceLifecycleError("unknown-node")
            if purpose == "generation" and not node.available:
                raise SourceLifecycleError("unavailable-evidence")
            return node.reference.model_copy(deep=True)

    def _storage(self) -> AsyncDatabase:
        if self._database is None:
            raise SourceLifecycleError("lifecycle-storage-unavailable")
        return self._database

    async def _load(self) -> _Graph:
        async with self._storage().transaction() as connection:
            payload = await connection.scalar(
                select(SOURCE_LIFECYCLE_TABLE.c.payload).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
            )
        return _Graph() if payload is None else _Graph.model_validate_json(payload)

    async def _save(self, graph: _Graph) -> None:
        table = SOURCE_LIFECYCLE_TABLE
        async with self._storage().transaction() as connection:
            existing = await connection.scalar(select(table.c.scope_id).where(table.c.scope_id == self.scope_id))
            if existing is None:
                await connection.execute(insert(table).values(scope_id=self.scope_id, payload=graph.model_dump_json()))
            else:
                await connection.execute(
                    update(table).where(table.c.scope_id == self.scope_id).values(payload=graph.model_dump_json())
                )

    async def _read_reference(self, node: _Node) -> None:
        reference = node.reference
        try:
            if isinstance(reference, SourceRef):
                if self._sources is None:
                    raise SourceLifecycleError("source-storage-unavailable")
                async with self._storage().transaction() as connection:
                    stored = await self._sources.get(connection, self.scope_id, reference)
                    if stored.value.materialization != SourceMaterialization.CAPTURED:
                        raise SourceLifecycleError("source-not-captured")
                    await self._sources.read_value(stored.value)
            elif isinstance(reference, MemoryCitation):
                await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=reference))
            else:
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    reference.family, reference.artifact_id, reference.revision
                )
        except (LookupError, ValueError, BaseValueNotFoundError) as error:
            raise SourceLifecycleError("unreadable-reference") from error

    async def _reconcile(self, graph: _Graph, *, reason: str) -> None:
        memories = [node for node in graph.nodes.values() if node.kind == "memory"]
        if memories:
            async with self._runtime._context(self.scope_id) as context:
                service = context.artifacts.memory
                current = await service.head(context.artifacts.memory_artifact_id)
                entries = {entry.entry_id: entry for entry in await service.entries(current)}
                states = {entry.entry_id: entry.state for entry in current.content.manifest.entries}
                for node in memories:
                    reference = node.reference
                    if not isinstance(reference, MemoryCitation):
                        raise SourceLifecycleError("invalid-memory-reference")
                    entry = entries.get(reference.entry_id)
                    if entry is None or entry.entry_version_id != reference.entry_version_id:
                        raise SourceLifecycleError("bound-memory-version-changed")
                    desired = "active" if node.available else "inactive"
                    if states[entry.entry_id] != desired:
                        transition = service.reactivate if node.available else service.forget
                        current = await transition(current, entries=(entry,), reason=reason)
                        states[entry.entry_id] = desired
        governance = ArtifactGovernanceRepository()
        async with self._storage().transaction() as connection:
            for node in graph.nodes.values():
                if node.kind != "topic" or not isinstance(node.reference, ArtifactRef):
                    continue
                ref = node.reference
                current = await governance.get(connection, self.scope_id, ref.family, ref.artifact_id)
                desired = ArtifactLifecycleState.ACTIVE if node.available else ArtifactLifecycleState.DEPRECATED
                if current.lifecycle_state != desired:
                    await governance.transition(
                        connection,
                        self.scope_id,
                        ref.family,
                        ref.artifact_id,
                        current.governance_generation,
                        desired,
                        None,
                    )


class SourceLifecycleApplication:
    """Select the persistent lifecycle graph belonging to one scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        database: AsyncDatabase | None,
        sources: SourceRepository | None,
    ) -> None:
        self._runtime = runtime
        self._database = database
        self._sources = sources

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, self._database, self._sources, scope_id)


__all__ = ["SourceLifecycleError", "SourceLifecycleEvent"]
