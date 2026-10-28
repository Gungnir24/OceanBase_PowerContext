"""演示：为一个开发任务装配上下文，并打印关键结论。

用法::

    python scripts/demo_context.py                 # Markdown 上下文
    python scripts/demo_context.py --json out.json # 同时落盘 JSON
    python scripts/demo_context.py --task "..." --task-id T-2026-1028
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sddctx.assemble import build_context  # noqa: E402
from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.powercontext import PowerContextClient  # noqa: E402
from sddctx.state import TaskStateStore  # noqa: E402

TASK = (
    "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）"
    "与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；"
    "过滤需与 keyword / sort / page / page_size 组合，total 为过滤后总数；"
    "同时保持既有响应结构、排序语义与分页行为不变。"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="装配并打印开发上下文")
    parser.add_argument("--task", default=TASK)
    parser.add_argument("--task-id", default="T-2026-1028")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--max-bytes", type=int, default=20000)
    parser.add_argument("--json", default="", help="把完整 JSON 写到这个路径")
    parser.add_argument("--markdown-out", default="", help="把 Markdown 上下文写到这个路径")
    parser.add_argument("--summary", action="store_true", help="只打印摘要")
    args = parser.parse_args(argv)

    cfg = load_config()
    db = open_db(cfg.seekdb)
    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    store = TaskStateStore(client, db)
    state = store.to_dict(store.load(args.task_id)) if args.task_id else None

    context = build_context(
        db=db,
        project=cfg.project,
        task_text=args.task,
        task_id=args.task_id,
        endpoint=args.endpoint,
        state=state,
        max_bytes=args.max_bytes,
    )

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON -> {path}")

    if args.markdown_out:
        path = Path(args.markdown_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(context["markdown"].rstrip() + "\n", encoding="utf-8", newline="\n")
        print(f"Markdown -> {path}")

    if args.summary:
        print("适用规格:", ", ".join(spec["clause_id"] for spec in context["specs"]))
        print("已排除:", ", ".join(spec["clause_id"] for spec in context["excluded_specs"]))
        print("代码位置:")
        for item in context["code"]:
            print(f"  - {item['path']}#{item['symbol']}  [{item['source']}]")
        target = [t for t in context["tests"] if t["kind"] == "target"]
        regression = [t for t in context["tests"] if t["kind"] == "regression"]
        print(f"测试: target={len(target)} regression={len(regression)}")
        print(f"检索: run_id={context['retrieval']['run_id']} latency={context['retrieval']['latency_ms']}ms")
        return 0

    print(context["markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
