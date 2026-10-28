"""任务状态：写入与复用（PowerContext 为主，seekdb 镜像对账）。

状态行格式（既可读也可解析）::

    [SDD][task=T-001][done] 实现 price_min/price_max 闭区间过滤
    [SDD][task=T-001][validation] 目标测试通过 || {"exit_code": 0, ...}

``||`` 之后是可选的 JSON 元数据（命令、退出码、日志路径等），便于后续运行
把它当作「已有验证结果」直接复用，而不是让 Agent 重跑一遍。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .db import SeekDb
from .powercontext import PowerContextClient, PowerContextError

LINE_RE = re.compile(r"^\[SDD\]\[task=([^\]]+)\]\[([a-z]+)\]\s*(.*)$", re.S)
KIND_LABELS = {
    "task": "任务",
    "done": "已完成",
    "todo": "待办",
    "validation": "验证结果",
    "decision": "决策",
}

TASK_KIND = "sdd-task"
ENTRY_KIND_PREFIX = "sdd-"


@dataclass
class StateEntry:
    kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)
    entry_id: str = ""
    created: str = ""


@dataclass
class TaskState:
    task_id: str
    title: str = ""
    description: str = ""
    entries: list[StateEntry] = field(default_factory=list)
    scope_id: str = ""
    prepared_excerpt: str = ""
    source: str = "powercontext"

    def of_kind(self, kind: str) -> list[StateEntry]:
        return [entry for entry in self.entries if entry.kind == kind]

    @property
    def done(self) -> list[str]:
        return [entry.text for entry in self.of_kind("done")]

    @property
    def todo(self) -> list[str]:
        return [entry.text for entry in self.of_kind("todo")]

    @property
    def validations(self) -> list[StateEntry]:
        return self.of_kind("validation")

    @property
    def decisions(self) -> list[StateEntry]:
        return self.of_kind("decision")


class TaskStateStore:
    def __init__(self, client: PowerContextClient, db: SeekDb) -> None:
        self.client = client
        self.db = db

    # ---------------------------------------------------------------- 写入
    def init_task(self, task_id: str, title: str, description: str) -> dict:
        meta = {"title": title, "description": description}
        self.db.execute(
            "INSERT INTO tasks (id, title, description) VALUES (%s, %s, %s)"
            " ON DUPLICATE KEY UPDATE title=VALUES(title), description=VALUES(description)",
            (task_id, title, description),
        )
        return self._append(task_id, "task", f"任务已登记：{title}", meta)

    def add_done(self, task_id: str, text: str, meta: dict | None = None) -> dict:
        return self._append(task_id, "done", text, meta or {})
    def add_todo(self, task_id: str, text: str, meta: dict | None = None) -> dict:
        return self._append(task_id, "todo", text, meta or {})

    def add_decision(self, task_id: str, text: str, meta: dict | None = None) -> dict:
        return self._append(task_id, "decision", text, meta or {})

    def add_validation(self, task_id: str, text: str, meta: dict | None = None) -> dict:
        return self._append(task_id, "validation", text, meta or {})

    def prune(self, task_id: str, reason: str = "") -> int:
        """撤回该任务的全部状态条目（PowerContext retire + 本地镜像清理）。

        用于纠正写错的记录（例如统计口径改过之后重新登记），retire 后条目在服务端
        变为 inactive，仍然保留审计痕迹，但不再参与上下文装配。
        """
        removed = 0
        for _ in range(500):
            target = None
            for entry in self.client.list_entries():
                if f"[task={task_id}]" not in str(entry.get("text", "")):
                    continue
                if entry.get("citation"):
                    target = entry
                    break
            if target is None:
                break
            # 每撤回一条，scope 的 Memory Revision 都会递增，所以必须重新拉一次
            self.client.retire_entry(
                target["citation"], reason or f"清理 {task_id} 的历史状态"
            )
            removed += 1
        self.db.execute("DELETE FROM task_states WHERE task_id=%s", (task_id,))
        return removed

    def _append(self, task_id: str, kind: str, text: str, meta: dict) -> dict:
        line = f"[SDD][task={task_id}][{kind}] {text}"
        if meta:
            line += " || " + json.dumps(meta, ensure_ascii=False)
        result = self.client.remember(
            f"{ENTRY_KIND_PREFIX}{kind}", line, reason=f"SDD 上下文 · {KIND_LABELS.get(kind, kind)}"
        )
        self.db.execute(
            "INSERT INTO task_states (task_id, kind, text) VALUES (%s, %s, %s)",
            (task_id, kind, line),
        )
        return {
            "ok": True,
            "entry": line,
            "entry_id": (result.get("citation") or {}).get("entry_id", ""),
            "version": result.get("version", 1),
        }

    # ---------------------------------------------------------------- 读取
    def load(self, task_id: str) -> TaskState:
        state = TaskState(task_id=task_id, scope_id=self.client.scope_id())
        try:
            entries = self.client.list_entries()
        except PowerContextError:
            entries = []
        for raw in entries:
            text = str(raw.get("text", ""))
            match = LINE_RE.match(text.strip())
            if not match or match.group(1) != task_id:
                continue
            kind = match.group(2)
            body = match.group(3).strip()
            meta: dict[str, Any] = {}
            if " || " in body:
                body, _, tail = body.partition(" || ")
                try:
                    meta = json.loads(tail)
                except json.JSONDecodeError:
                    meta = {"raw": tail}
            state.entries.append(
                StateEntry(
                    kind=kind,
                    text=body.strip(),
                    meta=meta,
                    entry_id=(raw.get("citation") or {}).get("entry_id", ""),
                )
            )
            if kind == "task":
                state.title = state.title or meta.get("title", "")
                state.description = state.description or meta.get("description", "")

        if not state.title:
            rows = self.db.query("SELECT title, description FROM tasks WHERE id=%s", (task_id,))
            if rows:
                state.title = rows[0]["title"]
                state.description = rows[0]["description"]

        # 「已完成」条目可以通过 meta.closes 关闭对应的待办，待办列表只保留未关闭项
        closed = {
            str(entry.meta.get("closes", "")).strip()
            for entry in state.entries
            if entry.kind == "done" and entry.meta.get("closes")
        }
        if closed:
            for entry in state.entries:
                if entry.kind == "todo" and entry.text.strip() in closed:
                    entry.kind = "closed-todo"

        try:
            prepared = self.client.prepare(f"任务 {task_id} 的进展与验证结果", max_bytes=2000)
            state.prepared_excerpt = _prepared_text(prepared)
        except PowerContextError:
            state.prepared_excerpt = ""
        return state

    def to_dict(self, state: TaskState) -> dict:
        return {
            "task_id": state.task_id,
            "title": state.title,
            "description": state.description,
            "source": "PowerContext Scope/Memory（scope_id 见下）",
            "scope_id": state.scope_id,
            "entry_count": len(state.entries),
            "done": state.done,
            "todo": state.todo,
            "closed_todos": [entry.text for entry in state.of_kind("closed-todo")],
            "decisions": [entry.text for entry in state.decisions],
            "validations": [
                {
                    "text": entry.text,
                    "exit_code": entry.meta.get("exit_code"),
                    "command": entry.meta.get("command", ""),
                    "passed": entry.meta.get("passed"),
                    "failed": entry.meta.get("failed"),
                    "log": entry.meta.get("log", ""),
                }
                for entry in state.validations
            ],
            "prepared_context_excerpt": state.prepared_excerpt,
        }


def _prepared_text(prepared: dict) -> str:
    for key in ("content", "markdown", "text", "assembled"):
        value = prepared.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1500]
    sections = prepared.get("sections")
    if isinstance(sections, list):
        joined = "\n".join(str(item.get("content", "")) for item in sections if isinstance(item, dict))
        return joined.strip()[:1500]
    return ""
