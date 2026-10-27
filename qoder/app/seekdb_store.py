"""seekdb 数据访问层。

职责：
* 建库建表（文献、切片、行为日志、检索日志）；
* 文献与切片的写入/删除；
* 一次请求内同时执行「向量近邻」与「全文匹配」，再用 RRF 融合；
* 把每次检索的候选与耗时写入 retrieval_logs，供页面“技术核验”核对。

seekdb 兼容 MySQL 协议，因此这里使用 pymysql。向量列使用 `VECTOR(N)`，
索引语法与 OceanBase/seekdb 文档一致：`VECTOR INDEX ... WITH (DISTANCE=l2,
TYPE=hnsw, LIB=vsag)`，全文索引使用 `WITH PARSER ik` 以支持中文。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .embeddings import tokens_for_query, vector_literal

_IDENT = re.compile(r"^[A-Za-z0-9_]+$")


class SeekDBError(RuntimeError):
    pass


@dataclass
class SearchHit:
    chunk_id: str
    paper_id: str
    title: str
    section: str
    chunk_index: int
    content: str
    year: Optional[int] = None
    venue: str = ""
    tags: str = ""
    distance: Optional[float] = None
    vector_rank: Optional[int] = None
    fts_rank: Optional[int] = None
    rrf_score: float = 0.0
    matched_by: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "paper_id": self.paper_id,
            "title": self.title,
            "section": self.section,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "year": self.year,
            "venue": self.venue,
            "tags": self.tags,
            "distance": None if self.distance is None else round(self.distance, 4),
            "vector_rank": self.vector_rank,
            "fts_rank": self.fts_rank,
            "rrf_score": round(self.rrf_score, 6),
            "matched_by": self.matched_by,
        }


class SeekDBStore:
    def __init__(self, cfg) -> None:
        if not _IDENT.match(cfg.seekdb_database):
            raise SeekDBError("SEEKDB_DATABASE 只能包含字母、数字与下划线")
        self.cfg = cfg
        self.database = cfg.seekdb_database
        self.vector_dim = cfg.vector_dim
        self.fts_available = False
        self.vector_index = False
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ 连接
    def _connect(self, with_database: bool = True):
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - 环境缺依赖时给出明确指引
            raise SeekDBError(
                "缺少 pymysql，请先执行 `python -m pip install -r requirements.txt`"
            ) from exc

        kwargs = dict(
            host=self.cfg.seekdb_host,
            port=self.cfg.seekdb_port,
            user=self.cfg.seekdb_user,
            password=self.cfg.seekdb_password,
            charset=self.cfg.seekdb_charset,
            autocommit=True,
            connect_timeout=6,
            read_timeout=30,
            write_timeout=30,
        )
        if with_database:
            kwargs["database"] = self.database
        try:
            return pymysql.connect(**kwargs)
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if with_database and code == 1049:
                self._create_database()
                return self._connect(with_database=True)
            raise SeekDBError(
                f"连接 seekdb 失败（{self.cfg.seekdb_host}:{self.cfg.seekdb_port}）：{exc}"
            ) from exc

    def _create_database(self) -> None:
        connection = self._connect(with_database=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.database}` "
                    "DEFAULT CHARACTER SET utf8mb4"
                )
        finally:
            connection.close()

    def _execute(self, sql: str, params: Sequence[Any] | None = None):
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                if cursor.description is None:
                    return ()
                return cursor.fetchall()
        finally:
            connection.close()

    # ------------------------------------------------------------------ 建表
    def ensure_schema(self) -> None:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS papers (
                        id VARCHAR(64) NOT NULL PRIMARY KEY,
                        scope_id VARCHAR(128) NOT NULL,
                        title VARCHAR(512) NOT NULL,
                        authors VARCHAR(512) NOT NULL DEFAULT '',
                        year INT NULL,
                        venue VARCHAR(255) NOT NULL DEFAULT '',
                        tags VARCHAR(512) NOT NULL DEFAULT '',
                        source_name VARCHAR(255) NOT NULL DEFAULT '',
                        content LONGTEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_papers_scope (scope_id)
                    ) DEFAULT CHARSET = utf8mb4
                    """
                )
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS chunks (
                        id VARCHAR(64) NOT NULL PRIMARY KEY,
                        paper_id VARCHAR(64) NOT NULL,
                        scope_id VARCHAR(128) NOT NULL,
                        chunk_index INT NOT NULL,
                        section VARCHAR(255) NOT NULL DEFAULT '',
                        content TEXT NOT NULL,
                        embedding VECTOR({self.vector_dim}) NOT NULL,
                        year INT NULL,
                        venue VARCHAR(255) NOT NULL DEFAULT '',
                        tags VARCHAR(512) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_chunks_paper (paper_id),
                        KEY idx_chunks_scope (scope_id)
                    ) DEFAULT CHARSET = utf8mb4
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS activity (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        scope_id VARCHAR(128) NOT NULL,
                        kind VARCHAR(32) NOT NULL,
                        ref_id VARCHAR(128) NOT NULL DEFAULT '',
                        title VARCHAR(512) NOT NULL DEFAULT '',
                        detail TEXT,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_activity_scope (scope_id, created_at)
                    ) DEFAULT CHARSET = utf8mb4
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS retrieval_logs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        scope_id VARCHAR(128) NOT NULL,
                        query_text VARCHAR(1024) NOT NULL,
                        filters_json TEXT,
                        vector_ids TEXT,
                        fts_ids TEXT,
                        fused_ids TEXT,
                        mode VARCHAR(64) NOT NULL DEFAULT '',
                        latency_ms INT NOT NULL DEFAULT 0,
                        note VARCHAR(512) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        KEY idx_logs_scope (scope_id, created_at)
                    ) DEFAULT CHARSET = utf8mb4
                    """
                )
        finally:
            connection.close()

        self._ensure_vector_index()
        self._ensure_fulltext_index()

    def _ensure_vector_index(self) -> None:
        last = ""
        for statement in (
            "CREATE VECTOR INDEX idx_chunks_vec ON chunks (embedding) "
            "WITH (DISTANCE=l2, TYPE=hnsw, LIB=vsag)",
            "CREATE VECTOR INDEX idx_chunks_vec ON chunks (embedding) "
            "WITH (DISTANCE=l2, TYPE=hnsw)",
        ):
            try:
                self._execute(statement)
                self.vector_index = True
                return
            except Exception as exc:  # noqa: BLE001 - 已有索引同样会报错
                message = str(exc)
                if "exists" in message.lower() or "Duplicate" in message:
                    self.vector_index = True
                    return
                last = message
        self.vector_index = False
        self.warnings.append(f"向量索引创建失败，退化为精确扫描：{last[:200]}")

    def _ensure_fulltext_index(self) -> None:
        attempts = [
            f"CREATE FULLTEXT INDEX idx_chunks_fts ON chunks (content) WITH PARSER {self.cfg.fts_parser}",
            "CREATE FULLTEXT INDEX idx_chunks_fts ON chunks (content) WITH PARSER ngram",
            "CREATE FULLTEXT INDEX idx_chunks_fts ON chunks (content)",
        ]
        last = ""
        for statement in attempts:
            try:
                self._execute(statement)
                self.fts_available = True
                return
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if "exists" in message.lower() or "Duplicate" in message:
                    self.fts_available = True
                    return
                last = message
        self.fts_available = False
        self.warnings.append(
            f"全文索引创建失败，退化为 LIKE 匹配（检索质量下降）：{last[:200]}"
        )

    # ------------------------------------------------------------------ 写入
    def upsert_paper(
        self,
        scope_id: str,
        paper_id: str,
        document,
        chunks: Iterable[Tuple[str, int, str, str, Sequence[float]]],
    ) -> int:
        """写入/覆盖一篇文献及其切片，返回切片数。"""
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM chunks WHERE paper_id = %s", (paper_id,))
                cursor.execute(
                    """
                    INSERT INTO papers
                        (id, scope_id, title, authors, year, venue, tags, source_name, content)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        title=VALUES(title), authors=VALUES(authors), year=VALUES(year),
                        venue=VALUES(venue), tags=VALUES(tags), source_name=VALUES(source_name),
                        content=VALUES(content), scope_id=VALUES(scope_id)
                    """,
                    (
                        paper_id,
                        scope_id,
                        document.title,
                        document.authors,
                        document.year,
                        document.venue,
                        document.tags,
                        document.source_name,
                        document.body,
                    ),
                )
                count = 0
                for chunk_id, index, section, content, vector in chunks:
                    literal = vector_literal(vector)
                    cursor.execute(
                        f"""
                        INSERT INTO chunks
                            (id, paper_id, scope_id, chunk_index, section, content,
                             embedding, year, venue, tags)
                        VALUES (%s, %s, %s, %s, %s, %s, {literal}, %s, %s, %s)
                        """,
                        (
                            chunk_id,
                            paper_id,
                            scope_id,
                            index,
                            (section or "")[:255],
                            content,
                            document.year,
                            document.venue,
                            document.tags,
                        ),
                    )
                    count += 1
            connection.commit()
            return count
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def delete_paper(self, scope_id: str, paper_id: str) -> bool:
        connection = self._connect()
        try:
            connection.begin()
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM chunks WHERE paper_id = %s AND scope_id = %s",
                    (paper_id, scope_id),
                )
                cursor.execute(
                    "DELETE FROM papers WHERE id = %s AND scope_id = %s",
                    (paper_id, scope_id),
                )
                deleted = cursor.rowcount > 0
            connection.commit()
            return deleted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------ 查询
    def list_papers(self, scope_id: str) -> List[Dict[str, Any]]:
        rows = self._execute(
            """
            SELECT p.id, p.title, p.authors, p.year, p.venue, p.tags, p.source_name,
                   p.created_at,
                   (SELECT COUNT(*) FROM chunks c WHERE c.paper_id = p.id) AS chunk_count
            FROM papers p
            WHERE p.scope_id = %s
            ORDER BY p.created_at DESC, p.title ASC
            """,
            (scope_id,),
        )
        return [self._paper_row(row) for row in rows]

    def get_paper(self, scope_id: str, paper_id: str) -> Optional[Dict[str, Any]]:
        rows = self._execute(
            """
            SELECT id, title, authors, year, venue, tags, source_name, content, created_at
            FROM papers WHERE id = %s AND scope_id = %s
            """,
            (paper_id, scope_id),
        )
        if not rows:
            return None
        paper = self._paper_row(rows[0])
        chunks = self._execute(
            """
            SELECT id, chunk_index, section, content
            FROM chunks WHERE paper_id = %s ORDER BY chunk_index ASC
            """,
            (paper_id,),
        )
        paper["content"] = rows[0]["content"]
        paper["chunks"] = [
            {
                "chunk_id": row["id"],
                "chunk_index": row["chunk_index"],
                "section": row["section"],
                "content": row["content"],
            }
            for row in chunks
        ]
        return paper

    @staticmethod
    def _paper_row(row: Dict[str, Any]) -> Dict[str, Any]:
        created = row.get("created_at")
        return {
            "paper_id": row["id"],
            "title": row["title"],
            "authors": row.get("authors", ""),
            "year": row.get("year"),
            "venue": row.get("venue", ""),
            "tags": row.get("tags", ""),
            "source_name": row.get("source_name", ""),
            "chunk_count": int(row.get("chunk_count", 0) or 0),
            "created_at": created.strftime("%Y-%m-%d %H:%M") if created else "",
        }

    # ------------------------------------------------------------------ 检索
    def search(
        self,
        scope_id: str,
        query: str,
        embedding: Sequence[float],
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 6,
    ) -> Dict[str, Any]:
        filters = filters or {}
        started = time.perf_counter()
        where, params = self._filter_clause(scope_id, filters)

        vector_hits: List[SearchHit] = []
        fts_hits: List[SearchHit] = []
        warning = ""

        # 1) 向量通道：seekdb 的 l2_distance + HNSW 近似检索。
        literal = vector_literal(embedding)
        approximate = " APPROXIMATE" if self.vector_index else ""
        vector_sql = (
            "SELECT id, paper_id, title, section, chunk_index, content, year, venue, tags, "
            f"l2_distance(embedding, {literal}) AS distance "
            f"FROM chunks WHERE {where} "
            f"ORDER BY distance{approximate} LIMIT %s"
        )
        try:
            rows = self._execute(vector_sql, (*params, self.cfg.top_k_vector))
            for rank, row in enumerate(rows, start=1):
                vector_hits.append(self._hit_from_row(row, vector_rank=rank,
                                                     distance=float(row["distance"])))
        except Exception as exc:  # noqa: BLE001
            warning += f"向量通道失败：{str(exc)[:160]}；"
            vector_sql = "（向量检索失败）"

        # 2) 全文通道：seekdb MATCH ... AGAINST。
        terms = tokens_for_query(query)
        fts_query = " ".join(_sanitize_term(term) for term in terms)
        fts_sql = ""
        if fts_query:
            fts_sql, fts_params = self._fts_clause(where, params, fts_query)
            try:
                rows = self._execute(fts_sql, (*fts_params, self.cfg.top_k_fts))
                for rank, row in enumerate(rows, start=1):
                    fts_hits.append(self._hit_from_row(row, fts_rank=rank))
            except Exception as exc:  # noqa: BLE001
                warning += f"全文通道失败：{str(exc)[:160]}；"

        fused = self._rrf(vector_hits, fts_hits, top_k)
        latency_ms = int((time.perf_counter() - started) * 1000)
        self._log_retrieval(
            scope_id=scope_id,
            query=query,
            filters=filters,
            vector_hits=vector_hits,
            fts_hits=fts_hits,
            fused=fused,
            mode=("vector+fts" if fts_hits else "vector"),
            latency_ms=latency_ms,
            note=warning[:500],
        )
        return {
            "query": query,
            "filters": filters,
            "hits": [hit.to_dict() for hit in fused],
            "vector_candidates": len(vector_hits),
            "fts_candidates": len(fts_hits),
            "latency_ms": latency_ms,
            "vector_statement": vector_sql,
            "fts_statement": fts_sql,
            "fts_query": fts_query,
            "warning": warning.strip("；"),
            "fts_available": self.fts_available,
        }

    def _filter_clause(self, scope_id: str, filters: Dict[str, Any]) -> Tuple[str, tuple]:
        clauses = ["scope_id = %s"]
        params: List[Any] = [scope_id]
        year_from = filters.get("year_from")
        year_to = filters.get("year_to")
        if isinstance(year_from, int):
            clauses.append("year >= %s")
            params.append(year_from)
        if isinstance(year_to, int):
            clauses.append("year <= %s")
            params.append(year_to)
        venue = str(filters.get("venue") or "").strip()
        if venue:
            clauses.append("venue LIKE %s")
            params.append(f"%{venue}%")
        tag = str(filters.get("tag") or "").strip()
        if tag:
            clauses.append("tags LIKE %s")
            params.append(f"%{tag}%")
        return " AND ".join(clauses), tuple(params)

    def _fts_clause(self, where: str, params: tuple, fts_query: str) -> Tuple[str, tuple]:
        if self.fts_available:
            sql = (
                "SELECT id, paper_id, title, section, chunk_index, content, year, venue, tags, "
                "MATCH(content) AGAINST(%s IN BOOLEAN MODE) AS score "
                f"FROM chunks WHERE {where} "
                "AND MATCH(content) AGAINST(%s IN BOOLEAN MODE) "
                "ORDER BY score DESC LIMIT %s"
            )
            return sql, (fts_query, *params, fts_query)
        # 退化路径：没有全文索引时仍保留关键词通道，保证功能可演示。
        first = fts_query.split(" ")[0]
        sql = (
            "SELECT id, paper_id, title, section, chunk_index, content, year, venue, tags, "
            "1.0 AS score "
            f"FROM chunks WHERE {where} AND content LIKE %s LIMIT %s"
        )
        return sql, (*params, f"%{first}%")

    @staticmethod
    def _hit_from_row(
        row: Dict[str, Any],
        *,
        vector_rank: Optional[int] = None,
        fts_rank: Optional[int] = None,
        distance: Optional[float] = None,
    ) -> SearchHit:
        return SearchHit(
            chunk_id=row["id"],
            paper_id=row["paper_id"],
            title=row["title"],
            section=row.get("section", "") or "",
            chunk_index=int(row["chunk_index"]),
            content=row["content"],
            year=row.get("year"),
            venue=row.get("venue", "") or "",
            tags=row.get("tags", "") or "",
            distance=distance,
            vector_rank=vector_rank,
            fts_rank=fts_rank,
        )

    def _rrf(
        self, vector_hits: List[SearchHit], fts_hits: List[SearchHit], top_k: int
    ) -> List[SearchHit]:
        """加权 Reciprocal Rank Fusion，两路权重相同。"""
        k = max(1, self.cfg.rrf_k)
        merged: Dict[str, SearchHit] = {}
        scores: Dict[str, float] = {}

        for rank, hit in enumerate(vector_hits, start=1):
            merged[hit.chunk_id] = hit
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)
            hit.matched_by.append("vector")
        for rank, hit in enumerate(fts_hits, start=1):
            target = merged.get(hit.chunk_id)
            if target is None:
                merged[hit.chunk_id] = hit
                target = hit
                target.distance = None
            target.fts_rank = rank
            target.matched_by.append("fulltext")
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (k + rank)

        ranked = sorted(
            merged.values(), key=lambda item: scores[item.chunk_id], reverse=True
        )[: max(1, top_k)]
        for hit in ranked:
            hit.rrf_score = scores[hit.chunk_id]
        return ranked

    # ------------------------------------------------------------------ 日志
    def _log_retrieval(
        self,
        *,
        scope_id: str,
        query: str,
        filters: Dict[str, Any],
        vector_hits: List[SearchHit],
        fts_hits: List[SearchHit],
        fused: List[SearchHit],
        mode: str,
        latency_ms: int,
        note: str,
    ) -> None:
        try:
            self._execute(
                """
                INSERT INTO retrieval_logs
                    (scope_id, query_text, filters_json, vector_ids, fts_ids,
                     fused_ids, mode, latency_ms, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    scope_id,
                    query[:1000],
                    json.dumps(filters, ensure_ascii=False),
                    json.dumps([hit.chunk_id for hit in vector_hits], ensure_ascii=False),
                    json.dumps([hit.chunk_id for hit in fts_hits], ensure_ascii=False),
                    json.dumps(
                        [
                            {"id": hit.chunk_id, "rrf": round(hit.rrf_score, 5),
                             "matched_by": hit.matched_by}
                            for hit in fused
                        ],
                        ensure_ascii=False,
                    ),
                    mode,
                    latency_ms,
                    note,
                ),
            )
        except Exception:
            # 日志失败不应影响主流程，页面上会显示“暂无日志”。
            self.warnings.append("检索日志写入失败")

    def recent_logs(self, scope_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._execute(
            """
            SELECT id, query_text, filters_json, vector_ids, fts_ids, fused_ids,
                   mode, latency_ms, note, created_at
            FROM retrieval_logs WHERE scope_id = %s
            ORDER BY id DESC LIMIT %s
            """,
            (scope_id, max(1, min(limit, 100))),
        )
        result = []
        for row in rows:
            created = row.get("created_at")
            result.append(
                {
                    "id": row["id"],
                    "query": row["query_text"],
                    "filters": _safe_json(row.get("filters_json")),
                    "vector_ids": _safe_json(row.get("vector_ids"), default=[]),
                    "fts_ids": _safe_json(row.get("fts_ids"), default=[]),
                    "fused_ids": _safe_json(row.get("fused_ids"), default=[]),
                    "mode": row["mode"],
                    "latency_ms": row["latency_ms"],
                    "note": row["note"],
                    "created_at": created.strftime("%Y-%m-%d %H:%M:%S")
                    if created
                    else "",
                }
            )
        return result

    # ------------------------------------------------------------------ 行为
    def log_activity(
        self,
        scope_id: str,
        kind: str,
        ref_id: str = "",
        title: str = "",
        detail: str = "",
    ) -> None:
        try:
            self._execute(
                """
                INSERT INTO activity (scope_id, kind, ref_id, title, detail)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (scope_id, kind[:32], ref_id[:128], title[:512], detail[:8000]),
            )
        except Exception:
            self.warnings.append("活动日志写入失败")

    def weekly_activity(self, scope_id: str, days: int = 7) -> List[Dict[str, Any]]:
        since = datetime.now() - timedelta(days=max(1, days))
        rows = self._execute(
            """
            SELECT id, kind, ref_id, title, detail, created_at
            FROM activity WHERE scope_id = %s AND created_at >= %s
            ORDER BY id DESC
            """,
            (scope_id, since),
        )
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "ref_id": row["ref_id"],
                "title": row["title"],
                "detail": row["detail"],
                "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M")
                if row.get("created_at")
                else "",
            }
            for row in rows
        ]

    def stats(self, scope_id: str) -> Dict[str, Any]:
        rows = self._execute(
            """
            SELECT
              (SELECT COUNT(*) FROM papers WHERE scope_id = %s) AS papers,
              (SELECT COUNT(*) FROM chunks WHERE scope_id = %s) AS chunks,
              (SELECT COUNT(*) FROM retrieval_logs WHERE scope_id = %s) AS searches,
              (SELECT COUNT(*) FROM activity WHERE scope_id = %s) AS activities
            """,
            (scope_id, scope_id, scope_id, scope_id),
        )
        row = rows[0] if rows else {}
        return {
            "papers": int(row.get("papers", 0) or 0),
            "chunks": int(row.get("chunks", 0) or 0),
            "searches": int(row.get("searches", 0) or 0),
            "activities": int(row.get("activities", 0) or 0),
        }

    def health(self) -> Dict[str, Any]:
        rows = self._execute("SELECT VERSION() AS version")
        return {
            "ok": True,
            "version": rows[0]["version"] if rows else "",
            "host": f"{self.cfg.seekdb_host}:{self.cfg.seekdb_port}",
            "database": self.database,
            "fts_available": self.fts_available,
            "vector_index": self.vector_index,
            "warnings": self.warnings[-5:],
        }


def _sanitize_term(term: str) -> str:
    cleaned = re.sub(r'[+\-<>()~*"@]', "", term)
    return cleaned.strip()


def _safe_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
