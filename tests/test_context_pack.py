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

import base64
import hashlib
import json

import pytest

from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
    prepare_context_pack,
)


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_pack_wire_matches_independent_digest_and_byte_count_oracle() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": [base64.b64encode(i).decode() for i in items]}
    digest = "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()

    pack = ContextPack.build(items)
    assert json.loads(pack.to_bytes()) == {**unsigned, "digest": digest, "byte_count": len(pack.to_bytes())}
    assert pack.digest == digest
    assert pack.byte_count == len(pack.to_bytes())
    assert pack.to_bytes() == _canonical(
        {"schema": "powercontext.context-pack.v1", "items": unsigned["items"], "digest": digest,
         "byte_count": pack.byte_count}
    )


def test_pack_from_bytes_round_trip_and_equality() -> None:
    pack = ContextPack.build((b"one", b"two"))
    restored = ContextPack.from_bytes(pack.to_bytes())
    assert restored == pack
    assert restored.to_bytes() == pack.to_bytes()
    empty = ContextPack.build(())
    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    assert empty.items == ()
    assert b"".join(empty.items) == b""


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "schema"}, id="missing-schema"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "items"}, id="missing-items"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "digest"}, id="missing-digest"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "byte_count"}, id="missing-byte-count"),
        pytest.param(lambda p: {**p, "extra": 1}, id="extra-field"),
        pytest.param(lambda p: {**p, "schema": "other.v1"}, id="wrong-schema"),
        pytest.param(lambda p: {**p, "digest": "sha256:" + "0" * 64}, id="wrong-digest"),
        pytest.param(lambda p: {**p, "byte_count": p["byte_count"] + 1}, id="wrong-byte-count"),
        pytest.param(lambda p: {**p, "items": p["items"] + ["!!!"]}, id="invalid-base64"),
        pytest.param(lambda p: {**p, "items": p["items"] + [123]}, id="non-string-item"),
    ],
)
def test_pack_from_bytes_rejects_tampering(mutate) -> None:
    pack = ContextPack.build((b"alpha", b"beta"))
    payload = json.loads(pack.to_bytes())
    tampered = json.dumps(mutate(payload), sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(tampered)


def test_pack_from_bytes_accepts_non_canonical_valid_payloads() -> None:
    pack = ContextPack.build((b"alpha", b"beta"))
    spaced = json.dumps(json.loads(pack.to_bytes())).encode()
    assert ContextPack.from_bytes(spaced) == pack
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(b"not json")
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps([1, 2]).encode())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({"schema": "powercontext.context-pack.v1"}).encode())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes("text")  # type: ignore[arg-type]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_from_content_rejects_invalid_chunk_limits(limit: object) -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("text", chunk_bytes=limit)  # type: ignore[arg-type]


def test_from_content_chunks_stay_bounded_and_reassemble_exactly() -> None:
    text = "alpha 海洋 beta\nsecond line 海洋\n"
    for limit in (7, 16, 512, 4096):
        pack = ContextPack.from_content(text, chunk_bytes=limit)
        assert b"".join(pack.items).decode("utf-8") == text
        assert all(0 < len(item) <= limit for item in pack.items)
        assert all(item.decode("utf-8") for item in pack.items)
        assert ContextPack.from_content(text, chunk_bytes=limit) == pack


def test_from_content_rejects_limits_below_one_character() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)
    with pytest.raises(ContextPackError):
        ContextPack.from_content("a海", chunk_bytes=2)
    assert ContextPack.from_content("海", chunk_bytes=3).items == ("海".encode(),)
    assert ContextPack.from_content("text", chunk_bytes=1).items == (b"t", b"e", b"x", b"t")


def test_from_content_maps_none_to_empty_pack() -> None:
    pack = ContextPack.from_content(None)
    assert pack.items == ()
    assert b"".join(pack.items) == b""
    with pytest.raises(ContextPackError):
        ContextPack.from_content(b"bytes")  # type: ignore[arg-type]


def test_build_rejects_non_bytes_items() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.build(("text",))  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        ContextPack.build((bytearray(b"x"),))  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        ContextPack.build(123)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "target_items",
    [
        (b"a", b"b", b"new", b"tail"),
        (b"new", b"b", b"old"),
        (b"a", b"new", b"old"),
        (b"a", b"b", b"old", b"extra"),
        (),
        (b"wholly", b"different"),
    ],
)
def test_delta_reconstructs_changes_at_any_position(target_items: tuple[bytes, ...]) -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build(target_items)
    delta = ContextPackDelta.between(base, target)
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )
    assert rebuilt.to_bytes() == target.to_bytes()
    assert rebuilt == target
    assert delta.byte_count == len(delta.to_bytes())
    assert delta.base_digest == base.digest
    assert delta.target_digest == target.digest
    expected_prefix = 0
    for left, right in zip(base.items, target.items, strict=False):
        if left != right:
            break
        expected_prefix += 1
    assert delta.prefix_items == expected_prefix
    assert 0 <= delta.prefix_items <= len(base.items)


def test_delta_wire_fields_and_full_resend_prefix_zero() -> None:
    base = ContextPack.build((b"a", b"b"))
    target = ContextPack.build((b"x", b"y"))
    delta = ContextPackDelta.between(base, target)
    payload = json.loads(delta.to_bytes())
    assert set(payload) == {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
    assert payload["schema"] == "powercontext.context-pack-delta.v1"
    assert payload["prefix_items"] == 0
    assert payload["appended"] == [base64.b64encode(i).decode() for i in target.items]
    assert ContextPackDelta.from_bytes(delta.to_bytes()) == delta
    full_resend = {**payload, "prefix_items": 0, "appended": [base64.b64encode(i).decode() for i in target.items]}
    decoded = ContextPackDelta.from_bytes(_canonical(full_resend))
    assert apply_context_pack_delta(base, decoded).to_bytes() == target.to_bytes()


@pytest.mark.parametrize("prefix", [True, 1.5, -1])
def test_delta_from_bytes_rejects_invalid_prefix(prefix: object) -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())
    payload["prefix_items"] = prefix
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_canonical(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "appended"}, id="missing-appended"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "base_digest"}, id="missing-base-digest"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "prefix_items"}, id="missing-prefix"),
        pytest.param(lambda p: {**p, "extra": True}, id="extra-field"),
        pytest.param(lambda p: {**p, "schema": "other"}, id="wrong-schema"),
        pytest.param(lambda p: {**p, "target_digest": "not-a-digest"}, id="bad-target-digest"),
        pytest.param(lambda p: {**p, "appended": p["appended"] + ["!!!"]}, id="invalid-base64"),
    ],
)
def test_delta_from_bytes_rejects_tampering(mutate) -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    payload = json.loads(delta.to_bytes())
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(_canonical(mutate(payload)))


def test_delta_from_bytes_accepts_non_canonical_valid_payloads() -> None:
    base = ContextPack.build((b"alpha",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"alpha", b"beta")))
    spaced = json.dumps(json.loads(delta.to_bytes())).encode()
    assert ContextPackDelta.from_bytes(spaced) == delta
    with pytest.raises(ContextPackError):
        ContextPackDelta.from_bytes(b"[]")


def test_apply_rejects_wrong_base_target_and_prefix() -> None:
    base = ContextPack.build((b"one",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"one", b"two")))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"one", b"two")))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, delta.target_digest, 2, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, delta.target_digest, -1, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, delta.target_digest, True, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, ContextPackDelta(base.digest, delta.target_digest, 0, ("text",)))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(base, delta.to_bytes())  # type: ignore[arg-type]


def test_apply_rejects_delta_bound_violations_for_empty_base() -> None:
    empty = ContextPack.build(())
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(empty, ContextPackDelta(empty.digest, "sha256:" + "0" * 64, 1, ()))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(empty, ContextPackDelta(empty.digest, "sha256:" + "0" * 64, 0, (b"x",)))


def test_prepare_context_pack_returns_full_pack_without_base() -> None:
    pack = prepare_context_pack("alpha 海洋")
    assert isinstance(pack, ContextPack)
    assert b"".join(pack.items).decode("utf-8") == "alpha 海洋"
    assert pack == ContextPack.from_content("alpha 海洋", chunk_bytes=128)
    assert prepare_context_pack(None) == ContextPack.build(())


def test_prepare_context_pack_reuses_base_prefix_chunks() -> None:
    previous = "header\n" + "row" * 40
    updated = previous + "tail addition"
    base = prepare_context_pack(previous)
    assert isinstance(base, ContextPack)
    delta = prepare_context_pack(updated, base)
    assert isinstance(delta, ContextPackDelta)
    assert delta.base_digest == base.digest
    rebuilt = apply_context_pack_delta(
        ContextPack.from_bytes(base.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
    )
    assert b"".join(rebuilt.items).decode("utf-8") == updated
    assert rebuilt.byte_count == len(rebuilt.to_bytes())
    assert delta.byte_count == len(delta.to_bytes())
    assert delta.prefix_items == len(base.items)
    assert b"".join(delta.appended) == b"tail addition"


def test_prepare_context_pack_handles_head_changes_and_empty_target() -> None:
    base = prepare_context_pack("same prefix\nchanged tail")
    assert isinstance(base, ContextPack)
    delta = prepare_context_pack("same prefix\nother tail", base)
    assert isinstance(delta, ContextPackDelta)
    rebuilt = apply_context_pack_delta(base, delta)
    assert b"".join(rebuilt.items).decode("utf-8") == "same prefix\nother tail"
    empty_delta = prepare_context_pack(None, base)
    assert isinstance(empty_delta, ContextPackDelta)
    assert empty_delta.prefix_items == 0
    assert empty_delta.appended == ()
    rebuilt_empty = apply_context_pack_delta(base, empty_delta)
    assert rebuilt_empty == ContextPack.build(())
    assert b"".join(rebuilt_empty.items) == b""


def test_prepare_context_pack_rejects_invalid_base_pack() -> None:
    base = ContextPack.build((b"a",))
    forged = ContextPack((b"a",), "sha256:" + "0" * 64, 99)
    with pytest.raises(ContextPackError):
        prepare_context_pack("a", forged)
    with pytest.raises(ContextPackError):
        prepare_context_pack("a", "not-a-pack")  # type: ignore[arg-type]
    assert isinstance(prepare_context_pack("a", base), ContextPackDelta)


def test_prepare_context_pack_saves_bytes_for_stable_rounds() -> None:
    content = "line\n" * 200
    base = prepare_context_pack(content)
    assert isinstance(base, ContextPack)
    stable = prepare_context_pack(content, base)
    assert isinstance(stable, ContextPackDelta)
    assert stable.prefix_items == len(base.items)
    assert stable.appended == ()
    assert stable.byte_count < base.byte_count / 5
