"""状态复用测试：一个进程写 PowerContext，另一个进程读回来并用于上下文。

这里刻意用 subprocess 而不是同进程调用，模拟「模块重启 / 新会话」的真实场景。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "T-TEST-REUSE"


def _cli(*args: str) -> tuple[int, str]:
    process = subprocess.run(
        [sys.executable, "-m", "sddctx", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return process.returncode, (process.stdout or "") + (process.stderr or "")


def _json(output: str) -> dict:
    return json.loads(output[output.find("{") :])


def test_state_survives_process_restart(services):
    code, _ = _cli(
        "state",
        "init",
        "--task-id",
        TASK_ID,
        "--title",
        "状态复用回归任务",
        "--description",
        "验证 PowerContext 写入后可由新进程读取",
    )
    assert code == 0

    marker = "跨进程可见的待办：补齐价格区间边界用例"
    code, _ = _cli("state", "add", "--task-id", TASK_ID, "--kind", "todo", "--text", marker)
    assert code == 0
    code, _ = _cli("state", "add", "--task-id", TASK_ID, "--kind", "done", "--text", "上下文模块已通过契约测试")
    assert code == 0

    # 新进程：只读，不写任何状态
    code, output = _cli("state", "show", "--task-id", TASK_ID, "--json")
    assert code == 0
    payload = _json(output)
    assert marker in payload["todo"]
    assert any("契约测试" in item for item in payload["done"])
    assert payload["scope_id"]

    # 新进程：装配上下文时应带上这份状态
    code, output = _cli(
        "context",
        "--task",
        "给 /api/products 增加价格筛选",
        "--task-id",
        TASK_ID,
        "--json",
        "--no-persist",
    )
    assert code == 0
    context = _json(output)
    assert context["state"]["todo"] and marker in context["state"]["todo"]
    assert context["state"]["scope_id"] == payload["scope_id"]
