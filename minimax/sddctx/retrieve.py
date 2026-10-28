"""混合检索：seekdb 全文（ngram） + 向量（l2_distance） + RRF 融合。

一次检索的真实 SQL 会写进 ``retrieval_logs``，评审可在库里核对：
哪条查询命中了哪个文件/条目、命中的是全文通道还是向量通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .db import SeekDb
from .embeddings import embed, to_literal
from .textutil import boolean_query

RRF_K = 60
FTS_LIMIT = 12
VECTOR_LIMIT = 12


@dataclass
class ChunkHit:
    doc_path: str
    kind: str
    clause_id: str
    symbol: str
    heading: str
    body: str
    score: float = 0.0
    fts_score: float = 0.0
    distance: float = 0.0
    channels: list[str] = field(default_factory=list)
    ranks: dict[str, int] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.doc_path}#{self.symbol or self.clause_id}"


def _placeholders(values: list[str]) -> str:
    return ",".join(["%s"] * len(values))


def _fts(db: SeekDb, project: str, query: str, kinds: list[str], limit: int) -> list[dict]:
    terms = boolean_query(query)
    if not terms:
        return []
    sql = (
        "SELECT doc_path, kind, clause_id, symbol, heading, body,"
        " MATCH(body) AGAINST(%s) AS fts_score"
        " FROM chunks"
        f" WHERE project=%s AND kind IN ({_placeholders(kinds)}) AND MATCH(body) AGAINST(%s)"
        " ORDER BY fts_score DESC LIMIT %s"
    )
    args = [terms, project, *kinds, terms, limit]
    return db.query(sql, args)


def _vector(db: SeekDb, project: str, query: str, kinds: list[str], limit: int) -> list[dict]:
    literal = to_literal(embed(query))
    sql = (
        "SELECT doc_path, kind, clause_id, symbol, heading, body,"
        " l2_distance(embedding, %s) AS distance"
        " FROM chunks"
        f" WHERE project=%s AND kind IN ({_placeholders(kinds)})"
        " ORDER BY distance ASC LIMIT %s"
    )
    args = [literal, project, *kinds, limit]
    return db.query(sql, args)


def search(
    db: SeekDb,
    query: str,
    *,
    project: str,
    kinds: list[str],
    limit: int = 10,
    fts_limit: int = FTS_LIMIT,
    vector_limit: int = VECTOR_LIMIT,
) -> tuple[list[ChunkHit], list[dict]]:
    """返回 (融合排序结果, 审计行)。"""
    fts_rows = _fts(db, project, query, kinds, fts_limit)
    vec_rows = _vector(db, project, query, kinds, vector_limit)

    merged: dict[str, ChunkHit] = {}
    audit: list[dict] = []

    def take(row: dict, channel: str, rank: int, score: float) -> None:
        hit = ChunkHit(
            doc_path=row["doc_path"],
            kind=row["kind"],
            clause_id=row.get("clause_id") or "",
            symbol=row.get("symbol") or "",
            heading=row.get("heading") or "",
            body=row.get("body") or "",
        )
        hit = merged.setdefault(hit.key, hit)
        hit.channels.append(channel)
        hit.ranks[channel] = rank
        hit.score += 1.0 / (RRF_K + rank)
        if channel == "fts":
            hit.fts_score = float(score)
        else:
            hit.distance = float(score)
        audit.append(
            {
                "channel": channel,
                "query": query,
                "doc_path": row["doc_path"],
                "clause_id": row.get("clause_id") or "",
                "score": float(score),
                "rank": rank,
            }
        )

    for rank, row in enumerate(fts_rows, start=1):
        take(row, "fts", rank, row.get("fts_score") or 0.0)
    for rank, row in enumerate(vec_rows, start=1):
        take(row, "vector", rank, row.get("distance") or 0.0)

    ordered = sorted(merged.values(), key=lambda hit: (-hit.score, hit.doc_path))[:limit]
    return ordered, audit


def clause_hits(db: SeekDb, query: str, *, project: str, limit: int = 12) -> tuple[list[ChunkHit], list[dict]]:
    return search(db, query, project=project, kinds=["spec"], limit=limit)


def lookup_clauses(db: SeekDb, project: str) -> dict[str, dict]:
    rows = db.query("SELECT * FROM clauses WHERE project=%s", (project,))
    return {row["clause_id"]: row for row in rows}


def clauses_by_applicability(db: SeekDb, project: str, endpoint: str) -> list[dict]:
    """结构化适用面：``全局`` 或适用于本次修改的接口。"""
    rows = db.query("SELECT * FROM clauses WHERE project=%s ORDER BY clause_id", (project,))
    matched = []
    for row in rows:
        applies = (row.get("applies_to") or "").strip()
        if applies == "全局" or (endpoint and endpoint in applies):
            matched.append(row)
    return matched
