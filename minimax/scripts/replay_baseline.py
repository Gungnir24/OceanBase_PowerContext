"""把 workdir 回滚到「修改前」基线，用于复现目标测试失败的场景。

用法::

    python scripts/replay_baseline.py            # 回滚 + 跑测试
    python scripts/replay_baseline.py --restore  # 只回滚
    python scripts/replay_baseline.py --no-test
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _util  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回滚到修改前基线")
    parser.add_argument("--restore", action="store_true", help="只回滚，不跑测试")
    parser.add_argument("--no-test", action="store_true", help="不回滚，只跑测试")
    args = parser.parse_args(argv)

    if not args.no_test:
        restored = _util.restore_baseline()
        print(f"已回滚基线：{len(restored)} 个文件")
        if args.restore:
            return 0

    code, output = _util.python("-m", "pytest", "workdir/tests", "-q")
    passed = re.findall(r"(\d+) passed", output)
    failed = re.findall(r"(\d+) failed", output)
    print(output[-4000:] if len(output) > 4000 else output)
    print(
        f"[replay_baseline] exit={code} passed={passed[-1] if passed else 0}"
        f" failed={failed[-1] if failed else 0}"
    )
    # 基线场景下目标测试必然失败，因此返回 0 只表示「回滚动作成功」
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
