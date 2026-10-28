"""一键验收：按 README 的六项必做项逐条跑一遍，并留下带退出码的日志。

用法::

    python scripts/verify_all.py

它会依次验证：
1. 环境：seekdb 可连、PowerContext 可连；
2. 数据初始化：清空并导入规格/代码/测试索引；
3. 修改前：目标测试失败（功能未实现），回归测试通过；
4. 修改后：重放 MiniMax 记录的补丁，31 个测试全部通过，文件摘要一致；
5. 上下文：必须包含样例规定的规格条目、代码符号与测试，来源真实存在；
6. 规格更新：新增 FILTER-005 后重新导入，上下文随之变化；
7. 状态复用：新进程从 PowerContext 读回任务进展，并出现在上下文里。

退出码 0 表示全部通过。日志写入 ``docs/logs/acceptance-<时间戳>.log``。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import _util  # noqa: E402
from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.ingest import ingest  # noqa: E402
from sddctx.powercontext import PowerContextClient  # noqa: E402

REQUIRED_SPECS = {
    "FILTER-001",
    "FILTER-002",
    "FILTER-003",
    "FILTER-004",
    "COMPAT-001",
    "COMPAT-002",
    "COMPAT-003",
    "COMPAT-004",
    "ADR-0011",
    "SRS-F-014",
}
REQUIRED_CODE = {
    ("workdir/biz/catalog/service.py", "search_products"),
    ("workdir/biz/catalog/api.py", "handle_products_query"),
    ("workdir/biz/catalog/filters.py", "apply_price_range"),
}
REQUIRED_TESTS = {"workdir/tests/test_search_target.py", "workdir/tests/test_search_regression.py"}

RESULTS: list[dict] = []
LOG: list[str] = []


def record(section: str, ok: bool, detail: str, command: str = "", exit_code: int | None = None) -> bool:
    RESULTS.append(
        {
            "section": section,
            "ok": ok,
            "detail": detail,
            "command": command,
            "exit_code": exit_code,
        }
    )
    gate = "PASS" if ok else "FAIL"
    line = f"[{gate}] {section} · {detail}"
    print(line)
    LOG.append(line)
    if command:
        LOG.append(f"       $ {command} (exit={exit_code})")
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===")
    LOG.append("")
    LOG.append(f"=== {title} ===")


def run_cli(*args: str) -> tuple[int, str]:
    return _util.python("-m", "sddctx", *args)


def counts(output: str) -> tuple[int, int]:
    """从 sddctx verify 的汇总行取通过/失败数，避免误匹配日志中的其它数字。"""
    match = re.search(r"\[sddctx\] exit=\d+ passed=(\d+) failed=(\d+)", output)
    if match:
        return int(match.group(1)), int(match.group(2))
    passed = re.findall(r"(\d+) passed", output)
    failed = re.findall(r"(\d+) failed", output)
    return (int(passed[-1]) if passed else 0, int(failed[-1]) if failed else 0)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    cfg = load_config()

    section("1. 环境检查")
    try:
        db = open_db(cfg.seekdb)
        version = db.scalar("SELECT VERSION() AS v")
        record("环境 · seekdb", True, f"{cfg.seekdb.host}:{cfg.seekdb.port} / {version}")
    except Exception as exc:  # noqa: BLE001
        record("环境 · seekdb", False, str(exc))
        _flush(stamp)
        return 1

    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    if not client.ping():
        record("环境 · PowerContext", False, f"{cfg.powercontext.base_url} 不可用")
        _flush(stamp)
        return 1
    scope_id = client.scope_id()
    record("环境 · PowerContext", True, f"{cfg.powercontext.base_url} / scope={scope_id}")

    section("2. 数据初始化（seekdb 导入工程资料）")
    report = ingest(cfg, db, reset=True)
    stats = db.stats()
    ok = stats["documents"] >= 5 and stats["clauses"] >= 10 and stats["chunks"] > 0
    record(
        "初始化 · 导入",
        ok,
        f"documents={stats['documents']} clauses={stats['clauses']} chunks={stats['chunks']}"
        f" symbols={stats['symbols']}",
    )

    section("3. 修改前：目标测试失败、回归测试通过")
    _util.restore_baseline()
    code, output = run_cli(
        "verify",
        "--task-id",
        "T-2026-1028",
        "--label",
        "baseline-target",
        "--",
        sys.executable,
        "-m",
        "pytest",
        "workdir/tests/test_search_target.py",
        "-q",
    )
    passed_n, failed_n = counts(output)
    record(
        "修改前 · 目标测试",
        code != 0,
        f"exit={code} passed={passed_n} failed={failed_n}"
        "（预期失败，已写入 PowerContext）",
        "python -m sddctx verify --label baseline-target -- pytest workdir/tests/test_search_target.py -q",
        code,
    )
    code, output = run_cli(
        "verify",
        "--task-id",
        "T-2026-1028",
        "--label",
        "baseline-regression",
        "--",
        sys.executable,
        "-m",
        "pytest",
        "workdir/tests/test_search_regression.py",
        "-q",
    )
    record(
        "修改前 · 回归测试",
        code == 0,
        f"exit={code}",
        "python -m sddctx verify --label baseline-regression -- pytest workdir/tests/test_search_regression.py -q",
        code,
    )

    section("4. 修改后：重放 MiniMax 补丁并通过全部测试")
    code, output = _util.python("scripts/replay_minimax.py", "--check")
    LOG.extend("      " + line for line in output.strip().splitlines()[-6:])
    record(
        "修改后 · 重放与测试",
        code == 0,
        "31 个测试通过且文件摘要与记录一致" if code == 0 else output.strip().splitlines()[-1],
        "python scripts/replay_minimax.py --check",
        code,
    )
    ingest(cfg, db)
    code, output = run_cli(
        "verify",
        "--task-id",
        "T-2026-1028",
        "--label",
        "after-change",
        "--",
        sys.executable,
        "-m",
        "pytest",
        "workdir/tests",
        "-q",
    )
    passed_n, failed_n = counts(output)
    record(
        "修改后 · 验证结果入库",
        code == 0,
        f"exit={code} passed={passed_n} failed={failed_n}（已写入 PowerContext）",
        "python -m sddctx verify --label after-change -- pytest workdir/tests -q",
        code,
    )

    section("5. 上下文输出核对")
    code, output = run_cli(
        "context",
        "--task",
        "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）"
        "与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；"
        "同时保持既有响应结构、排序语义与分页行为不变",
        "--task-id",
        "T-2026-1028",
        "--json",
        "--max-bytes",
        "40000",
    )
    if code != 0:
        record("上下文 · 生成", False, output[-300:], exit_code=code)
        _flush(stamp)
        return 1
    context = json.loads(output[output.find("{") :])
    spec_ids = {spec["clause_id"] for spec in context["specs"]}
    code_ids = {(item["path"], item["symbol"]) for item in context["code"]}
    test_paths = {item["path"] for item in context["tests"]}
    excluded = {spec["clause_id"] for spec in context["excluded_specs"]}

    missing_specs = REQUIRED_SPECS - spec_ids
    record("上下文 · 必要规格", not missing_specs, f"缺失 {sorted(missing_specs)}" if missing_specs else f"{len(spec_ids)} 条")
    missing_code = REQUIRED_CODE - code_ids
    record("上下文 · 必要代码", not missing_code, f"缺失 {sorted(missing_code)}" if missing_code else f"{len(code_ids)} 处")
    missing_tests = REQUIRED_TESTS - test_paths
    record("上下文 · 必要测试", not missing_tests, f"缺失 {sorted(missing_tests)}" if missing_tests else f"{len(test_paths)} 个文件")
    record("上下文 · 失效决策可见", "ADR-0007" in excluded, f"已排除 {sorted(excluded)}")

    sources = [item["path"] for item in context["code"]] + [item["path"] for item in context["tests"]]
    missing_files = [path for path in set(sources) if not (ROOT / path).is_file()]
    record("上下文 · 来源真实存在", not missing_files, f"缺失 {missing_files}" if missing_files else f"{len(set(sources))} 个来源")
    record(
        "上下文 · 无悬空引用",
        not context["diagnostics"]["missing_refs"],
        str(context["diagnostics"]["missing_refs"]) or "none",
    )

    state = context.get("state") or {}
    record(
        "上下文 · 任务状态",
        bool(state.get("done")) and bool(state.get("todo")),
        f"done={len(state.get('done') or [])} todo={len(state.get('todo') or [])}"
        f" validations={len(state.get('validations') or [])}",
    )

    section("6. 规格更新后重新导入")
    code, output = _util.python("scripts/spec_update_demo.py")
    LOG.extend("      " + line for line in output.strip().splitlines()[-6:])
    record(
        "规格更新 · 输出随规格变化",
        code == 0,
        "FILTER-005 出现在更新后的上下文，恢复后消失" if code == 0 else output[-300:],
        "python scripts/spec_update_demo.py",
        code,
    )

    section("7. 状态复用（新进程读取 PowerContext）")
    code, output = run_cli("state", "show", "--task-id", "T-2026-1028", "--json")
    if code != 0:
        record("状态 · 新进程读取", False, output[-300:], exit_code=code)
    else:
        payload = json.loads(output[output.find("{") :])
        record(
            "状态 · 新进程读取",
            bool(payload.get("done")) and bool(payload.get("todo")),
            f"scope={payload.get('scope_id')} done={len(payload.get('done') or [])}"
            f" todo={len(payload.get('todo') or [])}"
            f" validations={len(payload.get('validations') or [])}",
            "python -m sddctx state show --task-id T-2026-1028 --json",
            code,
        )

    section("8. 最终回归")
    code, output = _util.python("-m", "pytest", "workdir/tests", "-q")
    passed_n, failed_n = counts(output)
    record(
        "最终 · 全量测试",
        code == 0,
        f"exit={code} passed={passed_n} failed={failed_n}",
        "python -m pytest workdir/tests -q",
        code,
    )

    return 0 if _flush(stamp) else 1


def _flush(stamp: str) -> bool:
    log_dir = ROOT / "docs" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"acceptance-{stamp}.log"
    passed = sum(1 for item in RESULTS if item["ok"])
    total = len(RESULTS)
    lines = [
        f"# SDD 上下文模块验收日志 {datetime.now().isoformat(timespec='seconds')}",
        f"# 代码版本: {_util.git_commit()}",
        f"# 结果: {passed}/{total} 通过",
        "",
        *LOG,
        "",
        "## 汇总",
        *[
            f"- [{'PASS' if item['ok'] else 'FAIL'}] {item['section']}: {item['detail']}"
            for item in RESULTS
        ],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{'=' * 60}")
    print(f"验收结果：{passed}/{total} 通过")
    print(f"日志：{path.relative_to(ROOT).as_posix()}")
    print(f"{'=' * 60}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(main())
