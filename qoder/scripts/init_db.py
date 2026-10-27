#!/usr/bin/env python
"""检查 seekdb 连接并创建表/索引。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.seekdb_store import SeekDBError, SeekDBStore  # noqa: E402


def main() -> int:
    cfg = Config.load()
    store = SeekDBStore(cfg)
    try:
        store.ensure_schema()
    except SeekDBError as exc:
        print(f"[FAIL] {exc}")
        print("提示：先执行 `docker compose up -d seekdb`，并确认 .env 中的 SEEKDB_* 配置。")
        return 1
    health = store.health()
    print("[OK] seekdb 已连接")
    print(f"  version       : {health['version']}")
    print(f"  database      : {health['database']}")
    print(f"  vector index  : {'HNSW' if health['vector_index'] else '未创建（退化为精确扫描）'}")
    print(f"  fulltext index: {'可用' if health['fts_available'] else '未创建（退化为 LIKE）'}")
    for warning in health.get("warnings", []):
        print(f"  ! {warning}")
    print("\n下一步：python scripts/seed.py 导入示例文献，或直接 python app/server.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
