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

"""Context Packs: chunked, digest-verified context bodies with delta transfer.

A :class:`ContextPack` splits one prepared-context body into bounded, valid
UTF-8 chunks and carries its digest and exact serialized size. A
:class:`ContextPackDelta` reuses a base pack's leading chunks and appends the
remaining target chunks, so a receiver holding the base pack rebuilds the
target body from serialized data alone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"

DEFAULT_CHUNK_BYTES = 512
_PACK_CHUNK_BYTES = 128

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """A Context Pack wire payload or chunking rule was violated."""


def _canonical_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Iterable[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _coerce_items(items: Iterable[bytes]) -> tuple[bytes, ...]:
    try:
        item_tuple = tuple(items)
    except TypeError as error:
        raise ContextPackError("pack items must be an iterable of bytes") from error  # noqa: TRY003
    if any(not isinstance(item, bytes) for item in item_tuple):
        raise ContextPackError("pack items must be bytes")  # noqa: TRY003
    return item_tuple


def _split_utf8_chunks(data: bytes, chunk_bytes: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    start = 0
    total = len(data)
    while start < total:
        end = min(start + chunk_bytes, total)
        while end < total and (data[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:
            raise ContextPackError("chunk limit cannot fit one UTF-8 character")  # noqa: TRY003
        chunks.append(data[start:end])
        start = end
    return tuple(chunks)


def _pack_byte_count(digest: str, encoded_items: list[str]) -> int:
    value = 0
    for _ in range(10):
        encoded = _canonical_json(
            {"schema": PACK_SCHEMA, "items": encoded_items, "digest": digest, "byte_count": value}
        )
        if len(encoded) == value:
            return value
        value = len(encoded)
    raise ContextPackError("pack byte count did not converge")  # noqa: TRY003


def _decode_wire_json(data: object) -> object:
    if not isinstance(data, bytes | bytearray | memoryview):
        raise ContextPackError("wire payload must be bytes")  # noqa: TRY003
    try:
        return json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("wire payload is not valid JSON") from error  # noqa: TRY003


def _decode_wire_items(value: object) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("wire items must be a list of base64 strings")  # noqa: TRY003
    items: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("wire items must be a list of base64 strings")  # noqa: TRY003
        try:
            items.append(base64.b64decode(entry, validate=True))
        except ValueError as error:
            raise ContextPackError("wire items contain invalid base64") from error  # noqa: TRY003
    return tuple(items)


class ContextPack:
    """One complete chunked context body: items, digest, and serialized size."""

    __slots__ = ("byte_count", "digest", "items")

    def __init__(self, items: Iterable[bytes], digest: str, byte_count: int, /) -> None:
        item_tuple = _coerce_items(items)
        if not isinstance(digest, str):
            raise ContextPackError("pack digest must be a string")  # noqa: TRY003
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ContextPackError("pack byte count must be a non-negative integer")  # noqa: TRY003
        self.items = item_tuple
        self.digest = digest
        self.byte_count = byte_count

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a complete pack from a tuple of chunk bytes."""
        item_tuple = _coerce_items(items)
        encoded_items = _encode_items(item_tuple)
        digest = _pack_digest(item_tuple)
        return cls(item_tuple, digest, _pack_byte_count(digest, encoded_items))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk one context body deterministically into valid UTF-8 chunks."""
        if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
            raise ContextPackError("chunk limit must be a positive integer")  # noqa: TRY003
        if content is None:
            return cls.build(())
        if not isinstance(content, str):
            raise ContextPackError("content must be a string or None")  # noqa: TRY003
        return cls.build(_split_utf8_chunks(content.encode("utf-8"), chunk_bytes))

    def to_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema": PACK_SCHEMA,
                "items": _encode_items(self.items),
                "digest": self.digest,
                "byte_count": self.byte_count,
            }
        )

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode and fully verify one complete pack wire payload."""
        payload = _decode_wire_json(data)
        if not isinstance(payload, dict) or set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack payload fields do not match the schema")  # noqa: TRY003
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("pack schema mismatch")  # noqa: TRY003
        items = _decode_wire_items(payload["items"])
        if payload["digest"] != _pack_digest(items):
            raise ContextPackError("pack digest mismatch")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ContextPackError("pack byte count must be a non-negative integer")  # noqa: TRY003
        pack = cls(items, payload["digest"], byte_count)
        if len(pack.to_bytes()) != byte_count:
            raise ContextPackError("pack byte count mismatch")  # noqa: TRY003
        return pack

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextPack):
            return NotImplemented
        return (self.items, self.digest, self.byte_count) == (other.items, other.digest, other.byte_count)

    def __hash__(self) -> int:
        return hash((self.items, self.digest, self.byte_count))

    def __repr__(self) -> str:
        return f"ContextPack(items={self.items!r}, digest={self.digest!r}, byte_count={self.byte_count!r})"


class ContextPackDelta:
    """Reuse a base pack's leading chunks and append the remaining target chunks."""

    __slots__ = ("appended", "base_digest", "prefix_items", "target_digest")

    def __init__(
        self,
        base_digest: str,
        target_digest: str,
        prefix_items: int,
        appended: Iterable[bytes],
        /,
    ) -> None:
        self.base_digest = base_digest
        self.target_digest = target_digest
        self.prefix_items = prefix_items
        self.appended = tuple(appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Select the longest common chunk prefix and carry the target suffix."""
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("between requires ContextPack arguments")  # noqa: TRY003
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        return _canonical_json(
            {
                "schema": DELTA_SCHEMA,
                "base_digest": self.base_digest,
                "target_digest": self.target_digest,
                "prefix_items": self.prefix_items,
                "appended": _encode_items(self.appended),
            }
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode and fully verify one delta wire payload."""
        payload = _decode_wire_json(data)
        if not isinstance(payload, dict) or set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta payload fields do not match the schema")  # noqa: TRY003
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("delta schema mismatch")  # noqa: TRY003
        for field in ("base_digest", "target_digest"):
            value = payload[field]
            if not isinstance(value, str) or _DIGEST_PATTERN.fullmatch(value) is None:
                raise ContextPackError(f"delta {field} is not a sha256 digest")  # noqa: TRY003
        prefix = payload["prefix_items"]
        if isinstance(prefix, bool) or not isinstance(prefix, int) or prefix < 0:
            raise ContextPackError("delta prefix_items must be a non-negative integer")  # noqa: TRY003
        appended = _decode_wire_items(payload["appended"])
        return cls(payload["base_digest"], payload["target_digest"], prefix, appended)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextPackDelta):
            return NotImplemented
        return (self.base_digest, self.target_digest, self.prefix_items, self.appended) == (
            other.base_digest,
            other.target_digest,
            other.prefix_items,
            other.appended,
        )

    def __repr__(self) -> str:
        return (
            f"ContextPackDelta(base_digest={self.base_digest!r}, target_digest={self.target_digest!r}, "
            f"prefix_items={self.prefix_items!r}, appended={self.appended!r})"
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from a base pack and one verified delta."""
    if not isinstance(base, ContextPack):
        raise ContextPackError("base must be a ContextPack")  # noqa: TRY003
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta must be a ContextPackDelta")  # noqa: TRY003
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the base pack")  # noqa: TRY003
    prefix = delta.prefix_items
    if isinstance(prefix, bool) or not isinstance(prefix, int) or not 0 <= prefix <= len(base.items):
        raise ContextPackError("delta prefix_items is out of range for the base pack")  # noqa: TRY003
    if any(not isinstance(item, bytes) for item in delta.appended):
        raise ContextPackError("delta appended items must be bytes")  # noqa: TRY003
    target = ContextPack.build(base.items[:prefix] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack digest does not match the delta target digest")  # noqa: TRY003
    return target


def prepare_context_pack(
    content: str | None,
    base_pack: ContextPack | None = None,
    *,
    chunk_bytes: int = _PACK_CHUNK_BYTES,
) -> ContextPack | ContextPackDelta:
    """Pack one prepared context body, reusing a base pack's leading chunks.

    Without a base pack this returns the complete :class:`ContextPack`; with a
    valid base pack it returns a :class:`ContextPackDelta` whose rebuilt target
    concatenates to exactly ``content``.
    """
    if base_pack is None:
        return ContextPack.from_content(content, chunk_bytes=chunk_bytes)
    if not isinstance(base_pack, ContextPack):
        raise ContextPackError("base_pack must be a ContextPack")  # noqa: TRY003
    if base_pack.digest != _pack_digest(base_pack.items):
        raise ContextPackError("base_pack digest does not match its items")  # noqa: TRY003
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ContextPackError("chunk limit must be a positive integer")  # noqa: TRY003
    if content is None:
        data = b""
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        raise ContextPackError("content must be a string or None")  # noqa: TRY003
    prefix = 0
    consumed = 0
    for chunk in base_pack.items:
        if data[consumed : consumed + len(chunk)] != chunk:
            break
        prefix += 1
        consumed += len(chunk)
    # One appended chunk carries the whole remainder: every extra chunk would add
    # Base64 padding and JSON quoting overhead to the wire delta without enabling
    # any additional prefix reuse, since divergence is always mid-chunk.
    remainder = data[consumed:]
    appended = () if not remainder else (remainder,)
    target = ContextPack.build(base_pack.items[:prefix] + appended)
    return ContextPackDelta(base_pack.digest, target.digest, prefix, appended)
