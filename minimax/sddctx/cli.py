"""命令行入口：``python -m sddctx <command>``。

命令一览（完整说明见 README）：

    doctor      自检 seekdb / PowerContext / MiniMax 配置
    import      把规格、代码、测试导入 seekdb
    context     针对一个开发任务装配上下文（Markdown 或 --json）
    state       init / add / show 任务状态（PowerContext）
    verify      运行一组测试并记录验证结果（命令、退出码、日志、代码版本）
    db          stats：查看库里到底存了什么
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .assemble import DEFAULT_MAX_BYTES, build_context, detect_endpoint
from .config import PROJECT_ROOT, AppConfig, load_config
from .db import SeekDb, open_db
from .ingest import ingest
from .powercontext import PowerContextClient, PowerContextError
from .state import TaskStateStore

PYTEST_SUMMARY_RE = r"(\d+) passed"
PYTEST_FAILED_RE = r"(\d+) failed"


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _open_stack(cfg: AppConfig) -> tuple[SeekDb, PowerContextClient, TaskStateStore]:
    db = open_db(cfg.seekdb)
    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    return db, client, TaskStateStore(client, db)


def _git_commit() -> str:
    import shutil

    exe = shutil.which("git")
    if not exe:
        for candidate in (
            r"C:\Program Files\Git\cmd\git.exe",
            r"C:\Program Files (x86)\Git\cmd\git.exe",
            "/usr/bin/git",
        ):
            if Path(candidate).is_file():
                exe = candidate
                break
    if not exe:
        return "unknown (git 不在 PATH)"
    try:
        result = subprocess.run(
            [exe, "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# --------------------------------------------------------------------- doctor
def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    report: dict = {"paths": {}, "seekdb": {}, "powercontext": {}, "minimax": {}}
    ok = True

    for name, path in (
        ("specs", cfg.specs_dir),
        ("code", cfg.code_dir),
        ("tests", cfg.tests_dir),
    ):
        report["paths"][name] = {"path": str(path), "exists": Path(path).exists()}
        ok = ok and Path(path).exists()

    try:
        db = open_db(cfg.seekdb)
        report["seekdb"] = {
            "ok": True,
            "host": f"{cfg.seekdb.host}:{cfg.seekdb.port}",
            "database": cfg.seekdb.database,
            "version": db.scalar("SELECT VERSION() AS v"),
            "stats": db.stats(),
        }
    except Exception as exc:  # noqa: BLE001
        ok = False
        report["seekdb"] = {"ok": False, "error": str(exc)}

    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    try:
        alive = client.ping()
        report["powercontext"] = {
            "ok": alive,
            "base_url": cfg.powercontext.base_url,
            "scope_id": client.scope_id() if alive else "",
        }
        ok = ok and alive
    except PowerContextError as exc:
        ok = False
        report["powercontext"] = {"ok": False, "error": str(exc)}

    report["minimax"] = {
        "base_url": cfg.minimax.base_url,
        "model": cfg.minimax.model,
        "api_key_configured": bool(cfg.minimax.api_key),
    }
    report["ok"] = ok
    report["python"] = sys.version.split()[0]
    report["platform"] = platform.platform()
    _print_json(report)
    return 0 if ok else 1


# --------------------------------------------------------------------- import
def cmd_import(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = open_db(cfg.seekdb)
    report = ingest(
        cfg,
        db,
        reset=args.reset,
        force=args.force,
        specs_dir=Path(args.specs) if args.specs else None,
        code_dir=Path(args.code) if args.code else None,
        tests_dir=Path(args.tests) if args.tests else None,
    )
    _print_json(report.to_dict())
    return 0


# -------------------------------------------------------------------- context
def cmd_context(args: argparse.Namespace) -> int:
    cfg = load_config()
    db, client, store = _open_stack(cfg)
    state = None
    if args.task_id:
        state = store.to_dict(store.load(args.task_id))
    context = build_context(
        db=db,
        project=cfg.project,
        task_text=args.task,
        task_id=args.task_id or "",
        endpoint=args.endpoint or "",
        state=state,
        max_bytes=args.max_bytes,
        persist=not args.no_persist,
    )
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已写入 {path}")
    if args.json:
        _print_json(context)
    else:
        print(context["markdown"])
    return 0


# ---------------------------------------------------------------------- state
def cmd_state(args: argparse.Namespace) -> int:
    cfg = load_config()
    db, client, store = _open_stack(cfg)
    if args.action == "init":
        result = store.init_task(args.task_id, args.title, args.description)
        print(f"已写入任务登记：{result['entry_id']}")
        return 0
    if args.action == "add":
        meta = json.loads(args.meta) if args.meta else {}
        text = args.text
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8").strip()
        if not text:
            print("--text 或 --text-file 至少给一个", file=sys.stderr)
            return 2
        if getattr(args, "closes", ""):
            meta["closes"] = args.closes
        kind = args.kind
        if kind == "done":
            result = store.add_done(args.task_id, text, meta)
        elif kind == "todo":
            result = store.add_todo(args.task_id, text, meta)
        elif kind == "validation":
            result = store.add_validation(args.task_id, text, meta)
        else:
            result = store.add_decision(args.task_id, text, meta)
        _print_json(result)
        return 0
    if args.action == "show":
        state = store.load(args.task_id)
        payload = store.to_dict(state)
        if args.json:
            _print_json(payload)
        else:
            print(f"# 任务状态 {state.task_id} · {state.title}")
            print(f"- PowerContext scope: {state.scope_id}")
            for label, entries in (
                ("已完成", state.done),
                ("待办", state.todo),
                ("决策", [entry.text for entry in state.decisions]),
            ):
                print(f"\n## {label}")
                for text in entries or ["（无）"]:
                    print(f"- {text}")
            print("\n## 验证结果")
            for entry in state.validations:
                print(
                    f"- {entry.text} · exit={entry.meta.get('exit_code')}"
                    f" · {entry.meta.get('command', '')}"
                )
        return 0
    print(f"未知 action: {args.action}", file=sys.stderr)
    return 2


# --------------------------------------------------------------------- verify
def _pytest_counts(output: str) -> tuple[int, int]:
    """只从 pytest 的汇总行取数字，避免误匹配被测命令自己打印的统计。"""
    import re

    for line in reversed(output.splitlines()):
        text = line.strip()
        if not re.search(r"\bin \d+(?:\.\d+)?s\b", text):
            continue
        if not any(word in text for word in ("passed", "failed", "error", "skipped")):
            continue
        passed = re.search(r"(\d+) passed", text)
        failed = re.search(r"(\d+) failed", text)
        return (
            int(passed.group(1)) if passed else 0,
            int(failed.group(1)) if failed else 0,
        )
    return 0, 0


def cmd_verify(args: argparse.Namespace) -> int:
    cfg = load_config()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("verify 需要一个要执行的命令，例如：sddctx verify --label target -- pytest workdir/tests", file=sys.stderr)
        return 2

    started = datetime.now()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.log_dir / f"{started.strftime('%Y%m%d-%H%M%S')}-{args.label}.log"
    commit = _git_commit()

    process = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (process.stdout or "") + (process.stderr or "")
    passed_count, failed_count = _pytest_counts(output)

    header = [
        f"command: {' '.join(command)}",
        f"cwd: {PROJECT_ROOT}",
        f"started: {started.isoformat(timespec='seconds')}",
        f"exit_code: {process.returncode}",
        f"code_version: {commit}",
        f"python: {sys.version.split()[0]}",
        f"label: {args.label}",
        "-" * 72,
    ]
    log_path.write_text("\n".join(header) + "\n" + output, encoding="utf-8")

    if not args.no_state:
        db, client, store = _open_stack(cfg)
        summary = (
            f"{args.label}：退出码 {process.returncode}，"
            f"通过 {passed_count}，失败 {failed_count}"
        )
        store.add_validation(
            args.task_id,
            summary,
            {
                "exit_code": process.returncode,
                "command": " ".join(command),
                "passed": passed_count,
                "failed": failed_count,
                "log": str(log_path.relative_to(PROJECT_ROOT).as_posix()),
                "code_version": commit,
            },
        )
        try:
            client.capture_source(
                f"verify-{args.label}-{started.strftime('%Y%m%d%H%M%S')}",
                log_path.read_text(encoding="utf-8")[:200000],
                {
                    "label": args.label,
                    "exit_code": process.returncode,
                    "code_version": commit,
                    "command": " ".join(command),
                },
            )
        except PowerContextError as exc:
            print(f"[warn] 验证证据写入 PowerContext 失败：{exc}", file=sys.stderr)

    print(output)
    print(f"[sddctx] exit={process.returncode} passed={passed_count} failed={failed_count}")
    print(f"[sddctx] log={log_path}")
    return process.returncode


# ------------------------------------------------------------------------- db
def cmd_db(args: argparse.Namespace) -> int:
    cfg = load_config()
    db = open_db(cfg.seekdb)
    if args.action == "stats":
        payload = {
            "database": cfg.seekdb.database,
            "stats": db.stats(),
            "documents": db.query(
                "SELECT kind, COUNT(*) AS n FROM documents GROUP BY kind ORDER BY kind"
            ),
            "clauses": db.query(
                "SELECT status, COUNT(*) AS n FROM clauses GROUP BY status ORDER BY status"
            ),
        }
        _print_json(payload)
        return 0
    if args.action == "sql":
        rows = db.query(args.sql)
        _print_json({"rows": rows})
        return 0
    print(f"未知 action: {args.action}", file=sys.stderr)
    return 2


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sddctx", description="规有所依：面向持续演进的 SDD 上下文模块"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="自检依赖与配置")
    p_doctor.set_defaults(func=cmd_doctor)

    p_import = sub.add_parser("import", help="导入规格/代码/测试到 seekdb")
    p_import.add_argument("--reset", action="store_true", help="先清空库再导入")
    p_import.add_argument("--force", action="store_true", help="忽略摘要，强制重建索引")
    p_import.add_argument("--specs", default="", help="规格目录（默认 data/specs）")
    p_import.add_argument("--code", default="", help="代码目录（默认 workdir/biz）")
    p_import.add_argument("--tests", default="", help="测试目录（默认 workdir/tests）")
    p_import.set_defaults(func=cmd_import)

    p_context = sub.add_parser("context", help="装配开发上下文")
    p_context.add_argument("--task", required=True, help="开发任务描述")
    p_context.add_argument("--task-id", default="", help="关联的任务 ID（读取 PowerContext 状态）")
    p_context.add_argument("--endpoint", default="", help="本次修改涉及的接口，如 /api/products")
    p_context.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    p_context.add_argument("--json", action="store_true", help="输出 JSON")
    p_context.add_argument("--out", default="", help="把 JSON 写到文件")
    p_context.add_argument("--no-persist", action="store_true", help="不写检索审计")
    p_context.set_defaults(func=cmd_context)

    p_state = sub.add_parser("state", help="任务状态（PowerContext）")
    state_sub = p_state.add_subparsers(dest="action", required=True)
    p_init = state_sub.add_parser("init")
    p_init.add_argument("--task-id", required=True)
    p_init.add_argument("--title", required=True)
    p_init.add_argument("--description", default="")
    p_add = state_sub.add_parser("add")
    p_add.add_argument("--task-id", required=True)
    p_add.add_argument("--kind", required=True, choices=["done", "todo", "validation", "decision"])
    p_add.add_argument("--text", default="")
    p_add.add_argument("--text-file", default="")
    p_add.add_argument("--meta", default="", help="附加 JSON 元数据")
    p_add.add_argument("--closes", default="", help="把哪条待办标记为已完成（写入 meta.closes）")
    p_show = state_sub.add_parser("show")
    p_show.add_argument("--task-id", required=True)
    p_show.add_argument("--json", action="store_true")

    p_verify = sub.add_parser("verify", help="运行测试并记录验证结果")
    p_verify.add_argument("--task-id", required=True)
    p_verify.add_argument("--label", required=True)
    p_verify.add_argument("--no-state", action="store_true")
    p_verify.add_argument("command", nargs=argparse.REMAINDER)
    p_verify.set_defaults(func=cmd_verify)

    p_db = sub.add_parser("db", help="数据库自检")
    db_sub = p_db.add_subparsers(dest="action", required=True)
    db_sub.add_parser("stats")
    p_sql = db_sub.add_parser("sql")
    p_sql.add_argument("--sql", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "state":
        if args.action == "show":
            args.func = cmd_state
        else:
            args.func = cmd_state
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
