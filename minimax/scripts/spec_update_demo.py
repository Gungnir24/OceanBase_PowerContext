"""演示：规格变更后重新导入，上下文输出随之变化（不是固定答案）。

流程：
1. 用 ``data/specs/``（版本 2.3.0）导入并装配上下文 -> 记录条目集合 A；
2. 复制出 ``data/specs_updated/``，把接口契约升到 2.4.0 并新增 FILTER-005；
3. 用更新后的规格重新导入 -> 装配上下文 -> 记录条目集合 B；
4. 断言：B 含 FILTER-005、版本为 2.4.0，A 不含；随后恢复原规格，C 与 A 一致。

用法::

    python scripts/spec_update_demo.py
    python scripts/spec_update_demo.py --keep   # 保留 data/specs_updated 供人工查看
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sddctx.assemble import build_context  # noqa: E402
from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.ingest import ingest  # noqa: E402

BASE_SPECS = ROOT / "data" / "specs"
UPDATED_SPECS = ROOT / "data" / "specs_updated"
CONTRACT = "API-catalog-v2.md"

NEW_CLAUSE = """
### FILTER-005 价格区间跨度上限
- 状态: 生效
- 适用: GET /api/products
- 关联代码: workdir/biz/catalog/service.py#search_products
- 关联测试: workdir/tests/test_search_target.py#test_price_max_only
- 正文:
  - 单次请求的价格区间跨度（price_max - price_min）不得超过 100000 元。
  - 超过跨度上限返回 400，错误码 INVALID_FILTER。
  - 该条为 2.4.0 新增约束，用于防止误传超大区间导致全表扫描。
"""

TASK = (
    "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）"
    "与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；"
    "同时保持既有响应结构、排序语义与分页行为不变。"
)


def _apply_update() -> Path:
    if UPDATED_SPECS.exists():
        shutil.rmtree(UPDATED_SPECS)
    shutil.copytree(BASE_SPECS, UPDATED_SPECS)
    contract = UPDATED_SPECS / CONTRACT
    text = contract.read_text(encoding="utf-8")
    if "version: 2.3.0" not in text:
        raise SystemExit("契约版本不是 2.3.0，演示脚本已过期")
    text = text.replace("version: 2.3.0", "version: 2.4.0", 1)
    text = text.replace("updated: 2026-09-30", "updated: 2026-10-20", 1)
    if "FILTER-005" not in text:
        text = text.rstrip() + "\n" + NEW_CLAUSE
    contract.write_text(text, encoding="utf-8")
    return contract


def _clause_ids(context: dict) -> list[str]:
    return [spec["clause_id"] for spec in context["specs"]]


def _versions(context: dict) -> dict[str, str]:
    return {
        spec["doc_path"]: spec["doc_version"]
        for spec in context["specs"]
        if "API-catalog" in spec["doc_path"]
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="规格更新 -> 重新导入 -> 上下文变化")
    parser.add_argument("--keep", action="store_true", help="保留 data/specs_updated")
    args = parser.parse_args(argv)

    cfg = load_config()
    db = open_db(cfg.seekdb)
    lines: list[str] = [f"# 规格更新演示 {datetime.now().isoformat(timespec='seconds')}", ""]

    def build() -> dict:
        return build_context(
            db=db,
            project=cfg.project,
            task_text=TASK,
            task_id="",
            persist=False,
        )

    ingest(cfg, db, reset=True, specs_dir=BASE_SPECS)
    before = build()
    before_ids = _clause_ids(before)
    lines.append(f"## 更新前（data/specs，版本 2.3.0）")
    lines.append(f"- 条目：{', '.join(before_ids)}")
    lines.append(f"- 契约版本：{_versions(before)}")
    print(f"[1/4] 更新前条目 {len(before_ids)} 条；含 FILTER-005 = {'FILTER-005' in before_ids}")

    contract = _apply_update()
    lines.append("")
    lines.append(f"## 规格更新内容（{contract.relative_to(ROOT).as_posix()}）")
    lines.append("- version: 2.3.0 -> 2.4.0")
    lines.append("- 新增条目 FILTER-005 价格区间跨度上限")

    ingest(cfg, db, reset=True, specs_dir=UPDATED_SPECS)
    after = build()
    after_ids = _clause_ids(after)
    lines.append("")
    lines.append(f"## 更新后（data/specs_updated，版本 2.4.0）")
    lines.append(f"- 条目：{', '.join(after_ids)}")
    lines.append(f"- 契约版本：{_versions(after)}")
    print(f"[2/4] 更新后条目 {len(after_ids)} 条；含 FILTER-005 = {'FILTER-005' in after_ids}")

    checks: list[tuple[str, bool, str]] = []
    checks.append(("更新前不含 FILTER-005", "FILTER-005" not in before_ids, ""))
    checks.append(("更新后含 FILTER-005", "FILTER-005" in after_ids, ""))
    checks.append(
        (
            "更新后契约版本为 2.4.0",
            all(version == "2.4.0" for version in _versions(after).values()) and bool(_versions(after)),
            str(_versions(after)),
        )
    )
    checks.append(
        (
            "条目数量增加 1",
            len(after_ids) == len(before_ids) + 1,
            f"{len(before_ids)} -> {len(after_ids)}",
        )
    )

    ingest(cfg, db, reset=True, specs_dir=BASE_SPECS)
    restored = build()
    restored_ids = _clause_ids(restored)
    checks.append(("恢复原规格后回到更新前结果", restored_ids == before_ids, ""))
    print(f"[3/4] 已恢复原规格；条目数 {len(restored_ids)}")

    all_ok = True
    for name, ok, detail in checks:
        status = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        lines.append(f"- [{status}] {name} {detail}")
        print(f"      [{status}] {name} {detail}")

    if not args.keep and UPDATED_SPECS.exists():
        shutil.rmtree(UPDATED_SPECS)
        lines.append("- 已清理 data/specs_updated（--keep 可保留）")

    log_dir = ROOT / "docs" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"spec-update-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[4/4] 日志 -> {log_path.relative_to(ROOT).as_posix()}")
    print(json.dumps({"ok": all_ok, "before": before_ids, "after": after_ids}, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
