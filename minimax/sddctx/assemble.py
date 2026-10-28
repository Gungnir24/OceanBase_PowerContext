"""上下文装配：规格条目 + 代码符号 + 测试 + 任务状态 -> 一份可复核的上下文。

选择规则（都写在输出里，评审可逐条核对）：

1. **适用性优先**：``应用面`` 命中本次接口（或标记为「全局」）且状态为「生效」的规格条目，
   无论文本相似度如何都进入上下文——兼容性约束不能因为「任务描述里没提」就漏掉。
2. **相关性补充**：全文/向量 RRF 融合后命中的其它生效条目按分数补入。
3. **锚点展开**：规格条目里声明的 ``关联代码`` / ``关联测试`` 展开为具体文件、符号、用例，
   并在磁盘上校验存在性；不存在的引用进入 ``diagnostics.missing_refs``，绝不编造。
4. **失效可见**：状态为「已取代 / 已废弃」但仍命中本次接口的条目单独列出，
   让 Agent 明确「这条决策已经失效」，而不是悄悄消失或悄悄生效。
5. **预算裁剪**：超过 ``max_bytes`` 时优先丢弃「仅靠相似度入选」的语义条目，锚点条目不动。
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from .config import PROJECT_ROOT
from .db import SeekDb
from .retrieve import ChunkHit, clauses_by_applicability, lookup_clauses, search

ENDPOINT_RE = re.compile(r"/api/[A-Za-z0-9_\-/]+")
DEFAULT_MAX_BYTES = 12000
EXCERPT_CHARS = 1600
SEMANTIC_CODE_LIMIT = 6
SEMANTIC_TEST_LIMIT = 4


def detect_endpoint(task_text: str) -> str:
    match = ENDPOINT_RE.search(task_text or "")
    return match.group(0).rstrip("/") if match else ""


def _file_exists(root: Path, path: str) -> bool:
    return (root / path).is_file()


def _symbol_rows(db: SeekDb, project: str, path: str) -> list[dict]:
    return db.query(
        "SELECT name, kind, start_line, end_line, signature FROM symbols"
        " WHERE project=%s AND doc_path=%s ORDER BY start_line",
        (project, path),
    )


def _chunk_excerpt(db: SeekDb, project: str, path: str, symbol: str, limit: int = EXCERPT_CHARS) -> str:
    rows = db.query(
        "SELECT body FROM chunks WHERE project=%s AND doc_path=%s AND symbol=%s ORDER BY ordinal",
        (project, path, symbol),
    )
    text = "\n".join(row["body"] for row in rows)
    return text[:limit]


def _classify_test(path: str, clause_ids: list[str]) -> str:
    lowered = path.lower()
    if "target" in lowered:
        return "target"
    if "regression" in lowered:
        return "regression"
    if any(clause.startswith("FILTER") or clause.startswith("ADR") for clause in clause_ids):
        return "target"
    if any(clause.startswith("COMPAT") for clause in clause_ids):
        return "regression"
    return "other"


class _Entries:
    """去重的条目容器，保持插入顺序（锚点在前，语义补充在后）。"""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict] = {}

    def add(self, item: dict) -> None:
        key = (item["path"], item.get("symbol") or item.get("test_id") or "")
        existing = self._items.get(key)
        if existing:
            for clause in item.get("clauses", []):
                if clause not in existing["clauses"]:
                    existing["clauses"].append(clause)
            if not existing.get("reason") and item.get("reason"):
                existing["reason"] = item["reason"]
            return
        self._items[key] = item

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self) -> int:
        return len(self._items)


def _expand_code_ref(
    db: SeekDb, project: str, root: Path, ref: str, clause_id: str, missing: list[dict]
) -> list[dict]:
    path, symbol = ref.split("#", 1) if "#" in ref else (ref, "")
    if not _file_exists(root, path):
        missing.append({"ref": ref, "clause": clause_id, "reason": "文件不存在"})
        return []
    rows = _symbol_rows(db, project, path)
    if symbol:
        matched = [row for row in rows if row["name"] == symbol]
        if not matched:
            missing.append({"ref": ref, "clause": clause_id, "reason": "符号未在索引中找到"})
            return []
        return [
            {
                "path": path,
                "symbol": symbol,
                "kind": matched[0]["kind"],
                "start_line": matched[0]["start_line"],
                "end_line": matched[0]["end_line"],
                "reason": f"规格 {clause_id} 声明的关联代码",
                "source": "anchor",
                "clauses": [clause_id],
                "excerpt": _chunk_excerpt(db, project, path, symbol),
            }
        ]
    return [
        {
            "path": path,
            "symbol": row["name"],
            "kind": row["kind"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "reason": f"规格 {clause_id} 声明的关联代码（文件级）",
            "source": "anchor",
            "clauses": [clause_id],
            "excerpt": _chunk_excerpt(db, project, path, row["name"]),
        }
        for row in rows
        # 文件级引用不展开模块常量，除非规格显式写了 `路径#常量名`
        if row["kind"] != "variable"
    ]


def _expand_test_ref(
    db: SeekDb, project: str, root: Path, ref: str, clause_id: str, missing: list[dict]
) -> list[dict]:
    path, symbol = ref.split("#", 1) if "#" in ref else (ref, "")
    if not _file_exists(root, path):
        missing.append({"ref": ref, "clause": clause_id, "reason": "测试文件不存在"})
        return []
    rows = _symbol_rows(db, project, path)
    if symbol:
        matched = [row for row in rows if row["name"] == symbol]
        if not matched:
            missing.append({"ref": ref, "clause": clause_id, "reason": "用例未在索引中找到"})
            return []
        rows = matched
    else:
        rows = [row for row in rows if row["name"].startswith("test")]
    return [
        {
            "path": path,
            "test_id": row["name"],
            "kind": _classify_test(path, [clause_id]),
            "start_line": row["start_line"],
            "reason": f"规格 {clause_id} 声明的验证用例",
            "source": "anchor",
            "clauses": [clause_id],
        }
        for row in rows
    ]


def build_context(
    *,
    db: SeekDb,
    project: str,
    task_text: str,
    task_id: str = "",
    endpoint: str = "",
    state: dict | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    project_root: Path | None = None,
    persist: bool = True,
) -> dict:
    started = time.perf_counter()
    root = project_root or PROJECT_ROOT
    endpoint = endpoint or detect_endpoint(task_text)
    clauses = lookup_clauses(db, project)

    spec_hits, spec_audit = search(db, task_text, project=project, kinds=["spec"], limit=12)
    hit_by_clause: dict[str, ChunkHit] = {
        hit.clause_id: hit for hit in spec_hits if hit.clause_id
    }

    selected: list[dict] = []
    selected_ids: set[str] = set()

    for row in clauses_by_applicability(db, project, endpoint):
        if row["status"] != "active" or row["clause_id"] in selected_ids:
            continue
        selected_ids.add(row["clause_id"])
        selected.append(
            {
                "clause_id": row["clause_id"],
                "title": row["title"],
                "doc_path": row["doc_path"],
                "doc_version": row["version"],
                "status": row["status"],
                "applies_to": row["applies_to"],
                "body": row["body"],
                "code_refs": [ref for ref in (row["code_refs"] or "").split(",") if ref],
                "test_refs": [ref for ref in (row["test_refs"] or "").split(",") if ref],
                "selected_by": ["适用面"] + (["全文/向量"] if row["clause_id"] in hit_by_clause else []),
                "relevance": round(hit_by_clause.get(row["clause_id"]).score, 6)
                if row["clause_id"] in hit_by_clause
                else None,
            }
        )

    for hit in spec_hits:
        clause = clauses.get(hit.clause_id)
        if not clause or clause["status"] != "active" or hit.clause_id in selected_ids:
            continue
        selected_ids.add(hit.clause_id)
        selected.append(
            {
                "clause_id": clause["clause_id"],
                "title": clause["title"],
                "doc_path": clause["doc_path"],
                "doc_version": clause["version"],
                "status": clause["status"],
                "applies_to": clause["applies_to"],
                "body": clause["body"],
                "code_refs": [ref for ref in (clause["code_refs"] or "").split(",") if ref],
                "test_refs": [ref for ref in (clause["test_refs"] or "").split(",") if ref],
                "selected_by": ["全文/向量"],
                "relevance": round(hit.score, 6),
            }
        )

    excluded: list[dict] = []
    for row in clauses_by_applicability(db, project, endpoint):
        if row["status"] == "active":
            continue
        replaced_by = row["replaced_by"] or ""
        excluded.append(
            {
                "clause_id": row["clause_id"],
                "title": row["title"],
                "doc_path": row["doc_path"],
                "doc_version": row["version"],
                "status": row["status"],
                "replaced_by": replaced_by,
                "reason": "已被取代，本次任务不得实现"
                if row["status"] == "superseded"
                else "已废弃，本次任务不得实现",
            }
        )

    missing: list[dict] = []
    code_entries = _Entries()
    test_entries = _Entries()

    for clause in selected:
        for ref in clause["code_refs"]:
            for item in _expand_code_ref(db, project, root, ref, clause["clause_id"], missing):
                code_entries.add(item)
        for ref in clause["test_refs"]:
            for item in _expand_test_ref(db, project, root, ref, clause["clause_id"], missing):
                test_entries.add(item)

    semantic_code, code_audit = search(db, task_text, project=project, kinds=["code"], limit=10)
    added = 0
    for hit in semantic_code:
        if added >= SEMANTIC_CODE_LIMIT:
            break
        if not hit.symbol or not _file_exists(root, hit.doc_path):
            continue
        before = len(code_entries)
        code_entries.add(
            {
                "path": hit.doc_path,
                "symbol": hit.symbol,
                "kind": "semantic",
                "start_line": 0,
                "end_line": 0,
                "reason": "全文/向量相似（未经规格声明，供参考）",
                "source": "semantic",
                "clauses": [],
                "score": round(hit.score, 6),
                "excerpt": hit.body[:EXCERPT_CHARS],
            }
        )
        if len(code_entries) > before:
            added += 1

    semantic_tests, test_audit = search(db, task_text, project=project, kinds=["test"], limit=10)
    added = 0
    for hit in semantic_tests:
        if added >= SEMANTIC_TEST_LIMIT:
            break
        if not hit.symbol.startswith("test") or not _file_exists(root, hit.doc_path):
            continue
        before = len(test_entries)
        test_entries.add(
            {
                "path": hit.doc_path,
                "test_id": hit.symbol,
                "kind": _classify_test(hit.doc_path, []),
                "start_line": 0,
                "reason": "全文/向量相似（未经规格声明，供参考）",
                "source": "semantic",
                "clauses": [],
                "score": round(hit.score, 6),
            }
        )
        if len(test_entries) > before:
            added += 1

    context = {
        "schema": "sddctx.context/v1",
        "task": {
            "task_id": task_id,
            "text": task_text,
            "endpoint": endpoint,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "specs": selected,
        "excluded_specs": excluded,
        "code": list(code_entries),
        "tests": list(test_entries),
        "state": state,
        "diagnostics": {
            "missing_refs": missing,
            "truncated": False,
            "dropped": [],
            "spec_selected_by": {
                "适用面": sum(1 for item in selected if "适用面" in item["selected_by"]),
                "全文/向量": sum(1 for item in selected if "全文/向量" in item["selected_by"]),
            },
        },
    }

    # 预算裁剪：先丢语义补充条目，锚点条目保留
    markdown = render_markdown(context)
    dropped: list[str] = []
    while len(markdown.encode("utf-8")) > max_bytes:
        semantic_code_items = [item for item in context["code"] if item["source"] == "semantic"]
        semantic_test_items = [item for item in context["tests"] if item["source"] == "semantic"]
        if semantic_code_items:
            victim = semantic_code_items[-1]
            context["code"].remove(victim)
            dropped.append(victim["path"])
        elif semantic_test_items:
            victim = semantic_test_items[-1]
            context["tests"].remove(victim)
            dropped.append(victim["path"])
        else:
            for item in context["code"]:
                item["excerpt"] = item.get("excerpt", "")[:500]
            markdown = render_markdown(context)
            if len(markdown.encode("utf-8")) > max_bytes:
                break
            continue
        markdown = render_markdown(context)

    context["diagnostics"]["truncated"] = bool(dropped)
    context["diagnostics"]["dropped"] = dropped
    context["diagnostics"]["rendered_bytes"] = len(markdown.encode("utf-8"))
    context["diagnostics"]["budget_bytes"] = max_bytes

    latency_ms = int((time.perf_counter() - started) * 1000)
    audit = spec_audit + code_audit + test_audit
    context["retrieval"] = {
        "run_id": None,
        "latency_ms": latency_ms,
        "queries": [
            {"channel": "fts", "kinds": "spec", "query": task_text},
            {"channel": "vector", "kinds": "spec", "query": task_text},
            {"channel": "fts", "kinds": "code,test", "query": task_text},
            {"channel": "vector", "kinds": "code,test", "query": task_text},
        ],
        "hit_channels": {
            "fts": sum(1 for row in audit if row["channel"] == "fts"),
            "vector": sum(1 for row in audit if row["channel"] == "vector"),
        },
        "hit_count": len(audit),
    }

    if persist:
        payload = {key: value for key, value in context.items() if key != "markdown"}
        run_id = db.log_context_run(task_id or "adhoc", latency_ms, payload)
        db.log_retrievals(run_id, audit)
        context["retrieval"]["run_id"] = run_id

    context["markdown"] = render_markdown(context)
    return context


# -------------------------------------------------------------------- 渲染
def render_markdown(context: dict) -> str:
    task = context["task"]
    lines: list[str] = []
    lines.append(f"# SDD 开发上下文 · {task.get('task_id') or '（未命名任务）'}")
    lines.append("")
    lines.append(f"- 任务：{task['text']}")
    lines.append(f"- 接口：{task.get('endpoint') or '（未识别）'}")
    lines.append(f"- 生成时间：{task.get('generated_at')}")
    lines.append("")

    lines.append(f"## 1. 适用规格（{len(context['specs'])} 条）")
    for spec in context["specs"]:
        lines.append("")
        lines.append(f"### {spec['clause_id']} {spec['title']}")
        lines.append(
            f"- 来源：`{spec['doc_path']}` · 版本 {spec['doc_version'] or 'n/a'} · 状态 {spec['status']}"
            f" · 适用 {spec['applies_to'] or '全局'}"
        )
        lines.append(f"- 选择依据：{' + '.join(spec['selected_by'])}")
        if spec.get("relevance") is not None:
            lines.append(f"- 检索相关度：{spec['relevance']}")
        lines.append("- 要求：")
        for body_line in spec["body"].splitlines():
            if body_line.strip():
                lines.append(f"  - {body_line.strip()}")
        if spec["code_refs"]:
            lines.append(f"- 声明关联代码：{', '.join(spec['code_refs'])}")
        if spec["test_refs"]:
            lines.append(f"- 声明关联测试：{', '.join(spec['test_refs'])}")

    lines.append("")
    lines.append(f"## 2. 需要修改的代码（{len(context['code'])} 处）")
    for item in context["code"]:
        location = item["path"]
        if item.get("symbol"):
            location += f"#{item['symbol']}"
        lines.append("")
        lines.append(f"### {location}")
        if item.get("start_line"):
            lines.append(f"- 行号：L{item['start_line']}-L{item['end_line']}")
        lines.append(f"- 入选原因：{item['reason']}")
        if item["clauses"]:
            lines.append(f"- 依据条目：{', '.join(item['clauses'])}")
        if item.get("excerpt"):
            lines.append("")
            lines.append("```python")
            lines.append(item["excerpt"])
            lines.append("```")

    lines.append("")
    lines.append(f"## 3. 验证用测试（{len(context['tests'])} 个）")
    for item in context["tests"]:
        lines.append(
            f"- [{item['kind']}] `{item['path']}::{item['test_id']}` · {item['reason']}"
        )

    lines.append("")
    lines.append("## 4. 任务状态（PowerContext）")
    state = context.get("state")
    if not state:
        lines.append("- 未绑定任务状态（可用 `sddctx state init` 建立）。")
    else:
        lines.append(f"- 任务：{state.get('task_id')} · {state.get('title')}")
        lines.append(f"- Scope：`{state.get('scope_id')}` · 状态条目 {state.get('entry_count')} 条")
        lines.append("- 已完成：")
        for text in state.get("done") or ["（无）"]:
            lines.append(f"  - {text}")
        lines.append("- 待办：")
        for text in state.get("todo") or ["（无）"]:
            lines.append(f"  - {text}")
        lines.append("- 已有验证结果：")
        if state.get("validations"):
            for item in state["validations"]:
                exit_code = item.get("exit_code")
                lines.append(f"  - {item['text']}（退出码 {exit_code}）")
        else:
            lines.append("  - （无）")

    if context["excluded_specs"]:
        lines.append("")
        lines.append(f"## 5. 已失效 / 被取代的规格（{len(context['excluded_specs'])} 条，已排除）")
        for spec in context["excluded_specs"]:
            replaced = f" → 由 {spec['replaced_by']} 取代" if spec["replaced_by"] else ""
            lines.append(
                f"- {spec['clause_id']} {spec['title']}（{spec['status']}{replaced}）"
                f" · 来源 `{spec['doc_path']}`"
            )

    lines.append("")
    lines.append("## 6. 检索审计")
    retrieval = context.get("retrieval") or {}
    lines.append(
        f"- seekdb：run_id={retrieval.get('run_id')} · 耗时 {retrieval.get('latency_ms')}ms"
        f" · 命中 {retrieval.get('hit_count')} 行"
        f"（全文 {retrieval.get('hit_channels', {}).get('fts', 0)}，"
        f"向量 {retrieval.get('hit_channels', {}).get('vector', 0)}）"
    )
    missing = context["diagnostics"]["missing_refs"]
    if missing:
        lines.append(f"- 悬空引用（已忽略）：{len(missing)} 处")
        for item in missing:
            lines.append(f"  - {item['ref']}（{item['reason']}）")
    if context["diagnostics"]["dropped"]:
        lines.append(f"- 预算裁剪：{', '.join(context['diagnostics']['dropped'])}")
    return "\n".join(lines).strip() + "\n"
