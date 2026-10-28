"""把 MiniMax 记录的最终补丁重放到干净基线上（可复现性检查）。

用法::

    # 首次记录当前 workdir 的摘要（当前 workdir 就是 MiniMax 的输出）
    python scripts/replay_minimax.py --record

    # 之后任何时候重放并校验
    python scripts/replay_minimax.py --check

``--check`` 会：回滚到 data/baseline -> 写入 final-patch-response.json 的文件 ->
跑 workdir/tests -> 与记录的摘要比对。任何一步不一致都会失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _util  # noqa: E402

ROOT = _util.ROOT
FINAL = ROOT / "docs" / "minimax" / "final-patch-response.json"
DIGESTS = ROOT / "docs" / "minimax" / "applied-digests.json"
PATCH = ROOT / "data" / "patches" / "0001-price-and-stock-filter.patch"


def _digest(path: Path) -> str:
    """按 LF 归一化后取摘要，避免 Windows 检出 CRLF 导致校验结果随平台变化。"""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _load_files() -> list[dict]:
    payload = json.loads(FINAL.read_text(encoding="utf-8"))
    files = payload.get("files") or []
    if not files:
        raise SystemExit("final-patch-response.json 里没有 files")
    return files


def _load_round(round_no: int) -> tuple[list[dict], Path]:
    matches = sorted((ROOT / "docs" / "minimax").glob(f"*-round{round_no}-response.json"))
    if not matches:
        raise SystemExit(f"找不到第 {round_no} 轮的响应记录")
    path = matches[-1]
    payload = json.loads(path.read_text(encoding="utf-8")).get("parsed") or {}
    files = payload.get("files") or []
    if not files:
        raise SystemExit(f"{path.name} 里没有 files")
    return files, path


def _check_patch(files: list[dict]) -> tuple[bool, str]:
    """把 data/patches 里的 diff 打到基线副本上，结果应与 MiniMax 输出逐字节一致。"""
    exe = _util.git_exe()
    if not exe:
        return True, "跳过（git 不可用）"
    if not PATCH.is_file():
        return True, "跳过（没有 patch 文件）"

    temp = Path(tempfile.mkdtemp(prefix="sddctx-patch-"))
    try:
        shutil.copytree(_util.BASELINE, temp / "workdir")
        code, output = _util.run([exe, "apply", "--check", "-p1", str(PATCH)], cwd=temp)
        if code != 0:
            return False, f"git apply --check 失败：{output.strip()}"
        code, output = _util.run([exe, "apply", "-p1", str(PATCH)], cwd=temp)
        if code != 0:
            return False, f"git apply 失败：{output.strip()}"
        differing = [
            item["path"]
            for item in files
            if (temp / item["path"]).read_text(encoding="utf-8").strip()
            != (ROOT / item["path"]).read_text(encoding="utf-8").strip()
        ]
        if differing:
            return False, f"patch 结果与 MiniMax 输出不一致：{differing}"
        return True, f"{PATCH.name} 可干净应用到基线，且结果与 MiniMax 输出一致"
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="重放 MiniMax 记录的补丁")
    parser.add_argument("--record", action="store_true", help="记录当前 workdir 的摘要")
    parser.add_argument("--check", action="store_true", help="重放并校验摘要与测试")
    parser.add_argument("--keep", action="store_true", help="保留重放后的文件（不回滚）")
    parser.add_argument("--round", type=int, default=0, help="复现第 N 轮的结果（1=失败，2=通过）")
    args = parser.parse_args(argv)

    if args.round:
        files, source = _load_round(args.round)
        _util.restore_baseline()
        for item in files:
            _util.write_text(ROOT / item["path"], item["content"].rstrip() + "\n")
        exit_code, output, passed, failed = _run_tests()
        print(f"来源：{_util.rel(source)}（{len(files)} 个文件）")
        print(output.strip().splitlines()[-1] if output.strip() else "")
        expect_ok = args.round >= 2
        reproduced = exit_code == 0 if expect_ok else failed > 0
        print(
            f"复现{'成功' if reproduced else '失败'}：第 {args.round} 轮"
            f"（预期{'通过' if expect_ok else '失败'}，实际 exit={exit_code}"
            f" passed={passed} failed={failed}）"
        )
        return 0 if reproduced else 1

    files = _load_files()

    if args.record:
        record = {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "source": FINAL.name,
            "files": {
                item["path"]: {
                    "sha256": _digest(ROOT / item["path"]),
                    "bytes": (ROOT / item["path"]).stat().st_size,
                }
                for item in files
            },
        }
        _util.write_text(DIGESTS, json.dumps(record, ensure_ascii=False, indent=2))
        print(f"已记录 {len(files)} 个文件的摘要 -> {_util.rel(DIGESTS)}")
        return 0

    restored = _util.restore_baseline()
    print(f"[1/5] 已回滚基线（{len(restored)} 个文件）")
    for item in files:
        _util.write_text(ROOT / item["path"], item["content"].rstrip() + "\n")
    print(f"[2/5] 已写入 MiniMax 输出（{len(files)} 个文件）")

    patch_ok, patch_detail = _check_patch(files)
    print(f"[3/5] diff 校验：{'通过' if patch_ok else '失败'} · {patch_detail}")

    mismatches: list[str] = []
    if DIGESTS.is_file() and args.check:
        recorded = json.loads(DIGESTS.read_text(encoding="utf-8"))["files"]
        for path, info in recorded.items():
            actual = _digest(ROOT / path)
            if actual != info["sha256"]:
                mismatches.append(f"{path}: 期望 {info['sha256'][:12]} 实际 {actual[:12]}")
        print(f"[4/5] 摘要校验：{'通过' if not mismatches else '不一致'}")
        for line in mismatches:
            print(f"      {line}")
    else:
        print("[4/5] 跳过摘要校验（未记录）")

    exit_code, output, passed, failed = _run_tests()
    print(f"[5/5] pytest：exit={exit_code} passed={passed} failed={failed}")
    if exit_code != 0:
        print(output[-3000:])

    if not args.keep and not args.check:
        _util.restore_baseline()

    if mismatches or exit_code != 0 or not patch_ok:
        return 1
    return 0


def _run_tests() -> tuple[int, str, int, int]:
    import re

    code, output = _util.python("-m", "pytest", "workdir/tests", "-q")
    passed = re.findall(r"(\d+) passed", output)
    failed = re.findall(r"(\d+) failed", output)
    return code, output, int(passed[-1]) if passed else 0, int(failed[-1]) if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
