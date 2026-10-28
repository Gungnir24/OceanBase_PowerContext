"""脚本公共工具：命令执行、基线快照/回滚、代码版本记录。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKDIR = ROOT / "workdir"
BIZ = WORKDIR / "biz"
TESTS = WORKDIR / "tests"
BASELINE = ROOT / "data" / "baseline"
STATE = ROOT / ".state"


def run(command: list[str], *, cwd: Path | None = None, check: bool = False) -> tuple[int, str]:
    try:
        process = subprocess.run(
            command,
            cwd=str(cwd or ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        output = f"命令不存在：{command[0]}（未在 PATH 中）"
        if check:
            raise SystemExit(output) from None
        return 127, output
    except OSError as exc:
        if check:
            raise SystemExit(str(exc)) from None
        return 127, str(exc)
    output = (process.stdout or "") + (process.stderr or "")
    if check and process.returncode != 0:
        raise SystemExit(f"命令失败（{process.returncode}）：{' '.join(command)}\n{output}")
    return process.returncode, output


def python(*args: str, check: bool = False) -> tuple[int, str]:
    return run([sys.executable, *args], check=check)


def snapshot_baseline(target: Path | None = None) -> Path:
    """把当前 workdir 的源码快照到 data/baseline（保留相对结构）。"""
    target = target or BASELINE
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    for source in (BIZ, TESTS):
        relative = source.relative_to(WORKDIR)
        shutil.copytree(source, target / relative, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "修改前基线快照：只包含业务代码与测试，用于复现「修改前目标测试失败」",
        "files": sorted(
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file()
        ),
    }
    write_text(
        target / "MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2)
    )
    return target


def write_text(path: Path, text: str) -> Path:
    """统一用 LF 写文本，避免 Windows 上出现 CRLF 导致 diff/摘要不可移植。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def restore_baseline(source: Path | None = None) -> list[str]:
    """用基线快照覆盖 workdir（只覆盖快照里存在的文件）。"""
    source = source or BASELINE
    restored = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        relative = path.relative_to(source)
        destination = WORKDIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
        restored.append(relative.as_posix())
    return restored


def git_exe() -> str | None:
    """在 Windows 上 git 常常不在 PATH，这里做一次兜底查找。"""
    found = shutil.which("git")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        "/usr/bin/git",
        "/usr/local/bin/git",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


def git_commit() -> str:
    exe = git_exe()
    if not exe:
        return "unknown (git 不在 PATH)"
    code, out = run([exe, "rev-parse", "--short", "HEAD"])
    return out.strip() if code == 0 else "unknown"


def write_log(name: str, lines: list[str]) -> Path:
    log_dir = ROOT / "docs" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    write_text(path, "\n".join(lines) + "\n")
    return path


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)
