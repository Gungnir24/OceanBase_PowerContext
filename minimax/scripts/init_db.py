"""初始化 seekdb：建库建表 + 导入规格/代码/测试索引。

等价于 ``python -m sddctx import --reset``，单独成脚本是为了让运行说明更直观。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.ingest import ingest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化 seekdb 工程资料索引")
    parser.add_argument("--reset", action="store_true", default=True)
    parser.add_argument("--keep", action="store_true", help="保留已有数据（增量导入）")
    parser.add_argument("--specs", default="", help="规格目录（默认 data/specs）")
    args = parser.parse_args(argv)

    cfg = load_config()
    db = open_db(cfg.seekdb)
    report = ingest(
        cfg,
        db,
        reset=not args.keep,
        specs_dir=Path(args.specs) if args.specs else None,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print(json.dumps({"seekdb": db.stats()}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
