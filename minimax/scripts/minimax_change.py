"""用 MiniMax 完成本次业务代码修改（可重放、有留痕）。

流程：
1. 调用 sddctx 装配上下文（真实 seekdb 检索 + PowerContext 任务状态）；
2. 把上下文、真实源文件全文、验收测试一起交给 MiniMax，要求返回整文件补丁 JSON；
3. 落到 workdir 上跑测试：目标测试 + 回归测试都通过才算成功；
4. 失败则回滚基线，把失败输出喂回模型重试（最多 N 轮）；
5. 每轮的请求/响应/Running 结果都写入 docs/minimax/，最终补丁写入 data/patches/。

用法::

    python scripts/minimax_change.py --task-id T-2026-1028 --apply

不带 ``--apply`` 时只生成候选补丁、不写入 workdir。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _util  # noqa: E402
from sddctx.assemble import build_context  # noqa: E402
from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.ingest import ingest  # noqa: E402
from sddctx.minimax_client import MiniMaxClient, MinimaxError, parse_json_block  # noqa: E402
from sddctx.powercontext import PowerContextClient  # noqa: E402
from sddctx.state import TaskStateStore  # noqa: E402

ROOT = _util.ROOT
DOCS_MINIMAX = ROOT / "docs" / "minimax"
PATCH_DIR = ROOT / "data" / "patches"

TASK_TEXT = (
    "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）"
    "与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；"
    "过滤需与 keyword / sort / page / page_size 组合，total 为过滤后总数；"
    "同时保持既有响应结构、排序语义与分页行为不变。"
)

SYSTEM_PROMPT = """你是一名资深 Python 后端工程师，正在一个规格驱动开发（SDD）流程里修改代码。

你会收到：
1. 本次开发任务；
2. 由上下文模块从规格库检索出的「适用规格条目」（含条目编号、要求、声明的关联代码与测试）；
3. 需要修改的源文件全文；
4. 验收测试文件全文（这些测试不得修改）。

你的输出必须是**一个 JSON 对象**，不要输出 Markdown 代码围栏之外的任何解释：
{
  "analysis": "简述你如何满足每条规格，以及哪些既有行为必须保持",
  "files": [
    {"path": "workdir/biz/catalog/service.py", "content": "修改后的完整文件内容"}
  ],
  "notes": "实现说明（可选）"
}

硬性约束：
- 只能修改 workdir/biz/ 下**已存在**的文件；禁止新建文件、禁止修改 workdir/tests/（含 conftest.py）；
- files 里的 content 必须是完整文件内容（不是 diff、不是片段）；
- 只使用 Python 标准库，不新增第三方依赖；
- 保持公开函数签名向后兼容；错误必须用 catalog.errors.ApiError 抛出；
- 规格里标为「已取代 / 已废弃」的决策一律不得实现；
- 测试文件与 conftest.py 是验收口径：**即使测试失败也不得修改它们**，失败要回到实现里修；
- 语言使用简体中文注释，风格与现有代码一致。
"""


def _read_sources(context: dict, limit_files: int = 8) -> dict[str, str]:
    paths: list[str] = []
    for item in context["code"]:
        if item["path"].startswith("workdir/biz/") and item["path"] not in paths:
            paths.append(item["path"])
    selected = paths[:limit_files]
    sources = {}
    for path in selected:
        absolute = ROOT / path
        if absolute.is_file():
            sources[path] = absolute.read_text(encoding="utf-8")
    return sources


def _build_user_prompt(task: str, context: dict, sources: dict[str, str], tests: dict[str, str]) -> str:
    parts = [
        "# 开发任务",
        task,
        "",
        "# 上下文模块输出的适用规格与验证要求",
        context["markdown"],
        "",
        "# 需要修改的源文件全文",
    ]
    for path, content in sources.items():
        parts.append(f"## {path}")
        parts.append("```python")
        parts.append(content.rstrip())
        parts.append("```")
    parts.append("")
    parts.append("# 验收测试全文（禁止修改，用于理解期望行为）")
    for path, content in tests.items():
        parts.append(f"## {path}")
        parts.append("```python")
        parts.append(content.rstrip())
        parts.append("```")
    parts.append("")
    parts.append(
        "现在输出 JSON：files 必须是上面列出的源文件的完整新内容。"
        "如果某个文件不需要修改，可以不列出；但列出的文件路径必须与上面完全一致。"
    )
    return "\n".join(parts)


def _run_tests() -> tuple[int, str, int, int]:
    code, output = _util.python("-m", "pytest", "workdir/tests", "-q")
    passed = re.findall(r"(\d+) passed", output)
    failed = re.findall(r"(\d+) failed", output)
    return (
        code,
        output,
        int(passed[-1]) if passed else 0,
        int(failed[-1]) if failed else 0,
    )


def _validate_files(payload: dict) -> tuple[list[dict], str]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return [], "响应里 files 为空"
    cleaned = []
    for item in files:
        if not isinstance(item, dict):
            return [], "files 的元素不是对象"
        path = str(item.get("path", "")).strip()
        content = item.get("content")
        if not path.startswith("workdir/biz/"):
            return [], f"不允许修改 {path}：只能改 workdir/biz/ 下的文件"
        if path.startswith("workdir/tests/"):
            return [], "不允许修改测试文件"
        if not isinstance(content, str) or not content.strip():
            return [], f"{path} 的 content 为空"
        if not (ROOT / path).is_file():
            return [], f"{path} 不是已存在的文件（禁止新建文件）"
        cleaned.append({"path": path, "content": content})
    return cleaned, ""


def _write_patch(before: Path, after_files: list[str]) -> Path:
    PATCH_DIR.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    for path in sorted(after_files):
        baseline_rel = Path(path).relative_to("workdir")
        old = (before / baseline_rel).read_text(encoding="utf-8").splitlines(keepends=True)
        new = (ROOT / path).read_text(encoding="utf-8").splitlines(keepends=True)
        diff = difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        text = "".join(diff)
        if text:
            chunks.append(text)
    patch_path = PATCH_DIR / "0001-price-and-stock-filter.patch"
    _util.write_text(patch_path, "".join(chunks))
    return patch_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 MiniMax 完成业务代码修改")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--task", default=TASK_TEXT)
    parser.add_argument("--apply", action="store_true", help="把候选补丁写入 workdir")
    parser.add_argument("--max-rounds", type=int, default=4)
    parser.add_argument("--max-bytes", type=int, default=40000)
    args = parser.parse_args(argv)

    cfg = load_config()
    db = open_db(cfg.seekdb)
    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    store = TaskStateStore(client, db)
    minimax = MiniMaxClient(cfg.minimax)

    DOCS_MINIMAX.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"[1/5] 装配上下文（task_id={args.task_id}）…")
    state = store.to_dict(store.load(args.task_id))
    context = build_context(
        db=db,
        project=cfg.project,
        task_text=args.task,
        task_id=args.task_id,
        state=state,
        max_bytes=args.max_bytes,
    )
    (DOCS_MINIMAX / f"{stamp}-context.json").write_text(
        json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"      规格 {len(context['specs'])} 条 / 代码 {len(context['code'])} 处 / "
        f"测试 {len(context['tests'])} 个 / run_id={context['retrieval']['run_id']}"
    )

    sources = _read_sources(context)
    tests = {
        "workdir/tests/test_search_target.py": (ROOT / "workdir/tests/test_search_target.py").read_text(
            encoding="utf-8"
        ),
        "workdir/tests/test_search_regression.py": (
            ROOT / "workdir/tests/test_search_regression.py"
        ).read_text(encoding="utf-8"),
    }
    user_prompt = _build_user_prompt(args.task, context, sources, tests)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    (DOCS_MINIMAX / f"{stamp}-round1-request.json").write_text(
        json.dumps(
            {
                "model": cfg.minimax.model,
                "base_url": cfg.minimax.base_url,
                "task_id": args.task_id,
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    rounds: list[dict] = []
    applied_files: list[str] = []
    success = False
    final_payload: dict = {}

    for round_no in range(1, args.max_rounds + 1):
        print(f"[2/5] 第 {round_no} 轮调用 MiniMax（{cfg.minimax.model}）…")
        try:
            payload, result = minimax.chat_json(messages)
        except MinimaxError as exc:
            print(f"      调用失败：{exc}")
            return 2

        usage = result.get("usage") or {}
        for key in total_usage:
            total_usage[key] += int(usage.get(key) or 0)
        record = {
            "round": round_no,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "model": result.get("model"),
            "usage": usage,
            "elapsed_ms": result.get("elapsed_ms"),
            "content_chars": len(result.get("content") or ""),
        }
        (DOCS_MINIMAX / f"{stamp}-round{round_no}-response.json").write_text(
            json.dumps(
                {
                    "request_messages": messages,
                    "response_raw": result.get("raw"),
                    "parsed": payload,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        files, error = _validate_files(payload)
        if error:
            record.update({"ok": False, "error": error})
            rounds.append(record)
            print(f"      输出不合规：{error}")
            messages.append({"role": "assistant", "content": result["content"][:4000]})
            messages.append(
                {
                    "role": "user",
                    "content": f"你的输出不合规：{error}。请重新输出完整 JSON，只包含允许修改的源文件。",
                }
            )
            continue

        # 每轮都从基线出发，保证「候选补丁 = 完整可回滚的一次改动」
        _util.restore_baseline()
        for item in files:
            _util.write_text(ROOT / item["path"], item["content"].rstrip() + "\n")
        applied_files = [item["path"] for item in files]

        exit_code, output, passed, failed = _run_tests()
        record.update(
            {
                "ok": exit_code == 0,
                "exit_code": exit_code,
                "passed": passed,
                "failed": failed,
                "files": applied_files,
            }
        )
        rounds.append(record)
        print(f"      测试：exit={exit_code} passed={passed} failed={failed}")
        if exit_code == 0:
            success = True
            final_payload = payload
            break

        _util.restore_baseline()
        tail = output[-6000:]
        messages.append({"role": "assistant", "content": result["content"][:6000]})
        messages.append(
            {
                "role": "user",
                "content": (
                    "测试失败了，请修正实现后重新输出完整 JSON。"
                    "注意：不得修改任何测试文件或 conftest.py，只能改 workdir/biz/ 下的源文件。"
                    "以下是 pytest 输出：\n"
                    f"```\n{tail}\n```"
                ),
            }
        )

    log_path = DOCS_MINIMAX / f"{stamp}-dev-log.json"
    log_path.write_text(
        json.dumps(
            {
                "task_id": args.task_id,
                "task": args.task,
                "model": cfg.minimax.model,
                "base_url": cfg.minimax.base_url,
                "success": success,
                "rounds": rounds,
                "total_usage": total_usage,
                "applied_files": applied_files,
                "context_run_id": context["retrieval"]["run_id"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"      记录：{_util.rel(log_path)}")

    if not success:
        _util.restore_baseline()
        print("[3/5] MiniMax 未在限定轮次内通过测试，已回滚基线。")
        return 1

    final_path = DOCS_MINIMAX / "final-patch-response.json"
    final_path.write_text(
        json.dumps(final_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    patch_path = _write_patch(_util.BASELINE, applied_files)
    print(f"[3/5] 补丁写入 {_util.rel(patch_path)}")

    if not args.apply:
        _util.restore_baseline()
        print("[3/5] 未加 --apply，已把 workdir 回滚到基线；候选补丁与记录保留。")
        return 0

    print("[4/5] 重新索引修改后的代码…")
    report = ingest(cfg, db, force=False)
    print(f"      重建 {report.documents} 个文档、{report.chunks} 个片段")

    store.add_done(
        args.task_id,
        "MiniMax 已完成 price_min/price_max 与 in_stock_only 的实现，目标测试与回归测试全部通过",
        {
            "model": cfg.minimax.model,
            "rounds": len(rounds),
            "files": applied_files,
            "usage": total_usage,
            "patch": _util.rel(patch_path),
            "final_response": _util.rel(final_path),
        },
    )
    print("[5/5] 已把完成事项写回 PowerContext。")
    print(json.dumps({"success": True, "usage": total_usage, "files": applied_files}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
