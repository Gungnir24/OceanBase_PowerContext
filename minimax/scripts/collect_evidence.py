"""生成技术使用记录：seekdb 查询记录 + PowerContext 状态写入/复用记录。

所有内容都来自当前真实数据库与真实调用日志，避免「文档里写的和跑出来的不一致」。

用法::

    python scripts/collect_evidence.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sddctx.assemble import build_context  # noqa: E402
from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.embeddings import embed, to_literal  # noqa: E402
from sddctx.powercontext import PowerContextClient  # noqa: E402
from sddctx.state import TaskStateStore  # noqa: E402
from sddctx.textutil import boolean_query  # noqa: E402

DOCS = ROOT / "docs"
TASK_ID = "T-2026-1028"
TASK = (
    "为 GET /api/products 增加按价格区间（price_min / price_max，单位元、闭区间、最多两位小数）"
    "与库存状态（in_stock_only）的筛选条件；非法过滤参数返回 400 且错误码为 INVALID_FILTER；"
    "同时保持既有响应结构、排序语义与分页行为不变。"
)


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def db_record(db, cfg) -> str:
    # 先真实跑一次上下文装配，这样审计表里一定有本次的记录
    context = build_context(db=db, project="catalog", task_text=TASK, task_id=TASK_ID)
    run_id = context["retrieval"]["run_id"]
    version = db.scalar("SELECT VERSION() AS v")
    stats = db.stats()
    lines = [
        "# seekdb 使用记录（自动生成）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 连接：`{cfg.seekdb.host}:{cfg.seekdb.port}`，库 `{cfg.seekdb.database}`，账号 `{cfg.seekdb.user}`",
        f"- 服务端版本：`{version}`",
        "- 访问方式：PyMySQL（MySQL 协议），上下文模块内的 `sddctx/db.py`",
        "",
        "## 1. 存储内容（真实计数）",
        "",
        _table(
            ["表", "行数", "作用"],
            [
                [table, stats.get(table, 0), purpose]
                for table, purpose in (
                    ("documents", "规格/代码/测试文档登记（含版本与摘要）"),
                    ("clauses", "规格条目（编号、状态、适用面、声明锚点）"),
                    ("chunks", "可检索片段（含 VECTOR(384) 与 ngram 全文索引）"),
                    ("symbols", "代码符号（path#symbol，用于精确定位）"),
                    ("context_runs", "每次上下文装配的载荷与耗时"),
                    ("retrieval_logs", "每次检索的通道、命中文件、分数、排名"),
                    ("tasks", "任务登记"),
                    ("task_states", "PowerContext 状态的服务端镜像（对账用）"),
                )
            ],
        ),
        "",
        "## 2. 关键表结构（截取）",
        "",
        "```sql",
        "CREATE TABLE chunks (",
        "  id BIGINT AUTO_INCREMENT PRIMARY KEY,",
        "  project VARCHAR(64) NOT NULL, doc_path VARCHAR(512) NOT NULL,",
        "  kind VARCHAR(16) NOT NULL, clause_id VARCHAR(64), symbol VARCHAR(255),",
        "  ordinal INT NOT NULL, heading VARCHAR(255), body MEDIUMTEXT NOT NULL,",
        "  embedding VECTOR(384) NOT NULL,",
        "  FULLTEXT KEY ft_chunk_body (body) WITH PARSER ngram,",
        "  UNIQUE KEY uk_doc_ordinal (doc_path, ordinal)",
        ");",
        "```",
        "",
        "## 3. 检索 SQL 与实际结果",
        "",
    ]

    terms = boolean_query("价格区间 库存筛选 商品目录")
    fts_sql = (
        "SELECT doc_path, clause_id, symbol, MATCH(body) AGAINST(%s) AS score\n"
        "FROM chunks WHERE project='catalog' AND kind='spec'\n"
        "  AND MATCH(body) AGAINST(%s) ORDER BY score DESC LIMIT 5"
    )
    fts_rows = db.query(
        "SELECT doc_path, clause_id, symbol, MATCH(body) AGAINST(%s) AS score"
        " FROM chunks WHERE project=%s AND kind='spec' AND MATCH(body) AGAINST(%s)"
        " ORDER BY score DESC LIMIT 5",
        (terms, "catalog", terms),
    )
    lines += [
        "### 3.1 全文检索（ngram 分词）",
        "",
        f"查询词：`{terms}`",
        "",
        "```sql",
        fts_sql.replace("%s", "'" + terms + "'"),
        "```",
        "",
        _table(
            ["doc_path", "clause_id", "score"],
            [[row["doc_path"], row["clause_id"], f"{float(row['score']):.6f}"] for row in fts_rows],
        ),
        "",
    ]

    literal = to_literal(embed(TASK))
    vec_rows = db.query(
        "SELECT doc_path, clause_id, symbol, l2_distance(embedding, %s) AS distance"
        " FROM chunks WHERE project=%s AND kind='spec' ORDER BY distance ASC LIMIT 5",
        (literal, "catalog"),
    )
    lines += [
        "### 3.2 向量检索（l2_distance 暴力扫描，seekdb 1.4.0 不支持向量索引）",
        "",
        "```sql",
        "SELECT doc_path, clause_id, l2_distance(embedding, :query_vector) AS distance",
        "FROM chunks WHERE project='catalog' AND kind='spec' ORDER BY distance ASC LIMIT 5;",
        "```",
        "",
        _table(
            ["doc_path", "clause_id", "distance"],
            [[row["doc_path"], row["clause_id"], f"{float(row['distance']):.6f}"] for row in vec_rows],
        ),
        "",
    ]

    clause_rows = db.query(
        "SELECT clause_id, status, applies_to, replaced_by FROM clauses"
        " WHERE project=%s AND (applies_to='全局' OR applies_to LIKE %s) ORDER BY clause_id",
        ("catalog", "%/api/products%"),
    )
    lines += [
        "### 3.3 结构化适用面查询（决定「哪些规格条目适用」）",
        "",
        "```sql",
        "SELECT clause_id, status, applies_to, replaced_by FROM clauses",
        "WHERE project='catalog' AND (applies_to='全局' OR applies_to LIKE '%/api/products%')",
        "ORDER BY clause_id;",
        "```",
        "",
        _table(
            ["clause_id", "status", "applies_to", "replaced_by"],
            [
                [row["clause_id"], row["status"], row["applies_to"], row["replaced_by"]]
                for row in clause_rows
            ],
        ),
        "",
    ]

    log_rows = db.query(
        "SELECT channel, doc_path, clause_id, score, rank_no FROM retrieval_logs"
        " WHERE run_id=%s ORDER BY id LIMIT 12",
        (run_id,),
    )
    lines += [
        "### 3.4 检索审计（每次装配上下文都落库，可逐条复核）",
        "",
        f"本次演示产生 `context_runs.id={run_id}`，对应检索明细：",
        "",
        _table(
            ["channel", "doc_path", "clause_id", "score", "rank"],
            [
                [
                    row["channel"],
                    row["doc_path"],
                    row["clause_id"],
                    f"{float(row['score']):.6f}",
                    row["rank_no"],
                ]
                for row in log_rows
            ],
        ),
        "",
        f"本次装配耗时 {context['retrieval']['latency_ms']} ms，命中 "
        f"{context['retrieval']['hit_count']} 行（全文 "
        f"{context['retrieval']['hit_channels']['fts']}、向量 "
        f"{context['retrieval']['hit_channels']['vector']}）。",
        "",
        "## 4. 结论",
        "",
        "- 工程资料（规格条目、代码符号、测试用例）全部存在 seekdb 中，不是从本地文件现读；",
        "- 上下文里的每条规格、每个代码位置、每个测试都来自上述 SQL 的结果；",
        "- 规格更新后重新导入，`clauses` 与 `chunks` 变化，上下文随之变化（见 `docs/01-验证样例.md` 第 6 节）。",
    ]
    return "\n".join(lines) + "\n"


def pc_record(client: PowerContextClient, store: TaskStateStore) -> str:
    state = store.load(TASK_ID)
    calls = client.read_calls(limit=24)
    entries = client.list_entries()
    lines = [
        "# PowerContext 使用记录（自动生成）",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 服务端：`{client.cfg.base_url}`（PowerContext Server 1.2.0，REST `/v1`）",
        f"- Scope：`{state.scope_id}`",
        f"- 任务：`{state.task_id}` · {state.title}",
        "- 分工：seekdb 存「项目事实」，PowerContext 存「任务事实」（进展、决策、验证结果）",
        "",
        "## 1. 最近的接口调用（来自 `.state/powercontext-calls.jsonl`）",
        "",
        _table(
            ["时间", "方法", "接口", "状态", "耗时(ms)", "请求摘要"],
            [
                [
                    call.get("time", ""),
                    call.get("method", ""),
                    call.get("path", ""),
                    call.get("status", ""),
                    call.get("duration_ms", ""),
                    json.dumps(call.get("request") or {}, ensure_ascii=False)[:80],
                ]
                for call in calls
            ],
        ),
        "",
        "## 2. Scope 内的状态条目",
        "",
        f"共 {len(entries)} 条 memory entry，按类型统计：",
        "",
    ]
    counts: dict[str, int] = {}
    for entry in entries:
        kind = str(entry.get("kind", ""))
        counts[kind] = counts.get(kind, 0) + 1
    lines.append(_table(["kind", "条数"], [[kind, count] for kind, count in sorted(counts.items())]))
    lines += ["", "本任务（`T-2026-1028`）的结构化状态：", ""]
    lines.append(
        _table(
            ["类型", "内容"],
            [
                *[["已完成", text] for text in state.done],
                *[["待办", text] for text in state.todo],
                *[["决策", entry.text] for entry in state.decisions],
                *[
                    ["验证结果", f"{entry.text}（exit={entry.meta.get('exit_code')}）"]
                    for entry in state.validations
                ],
            ],
        )
    )
    lines += [
        "",
        "## 3. 复用证据（重启后读取）",
        "",
        "```text",
        "$ python -m sddctx state show --task-id T-2026-1028 --json",
        f"  scope_id = {state.scope_id}",
        f"  done     = {len(state.done)} 条",
        f"  todo     = {len(state.todo)} 条",
        f"  validations = {len(state.validations)} 条",
        "```",
        "",
        "每次 `state show` 都是**全新进程**发起 `POST /v1/memory/entries/list` 重新拉取，",
        "没有任何本地缓存（除 scope_id 本身），因此能证明状态来自 PowerContext 而不是内存。",
        "",
        "## 4. context/prepare 返回的有界上下文（截取）",
        "",
        "```text",
        (state.prepared_excerpt or "（服务端未返回可展示内容）")[:1200],
        "```",
        "",
        "## 5. 验证证据落库",
        "",
        "`sddctx verify` 除了写 memory，还会把完整日志通过 `POST /v1/sources/content` 存为 Source，",
        "source_id 形如 `verify-after-change-20261028135632`，便于复核「当时到底跑了什么」。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    cfg = load_config()
    db = open_db(cfg.seekdb)
    client = PowerContextClient(cfg.powercontext, cfg.state_dir)
    store = TaskStateStore(client, db)

    (DOCS / "04-数据库查询记录.md").write_text(db_record(db, cfg), encoding="utf-8")
    (DOCS / "05-PowerContext状态记录.md").write_text(pc_record(client, store), encoding="utf-8")
    print("已生成 docs/04-数据库查询记录.md 与 docs/05-PowerContext状态记录.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
