"""seekdb 访问层（MySQL 协议）。

存储分工：
* ``documents`` / ``chunks`` / ``symbols`` —— 工程资料与索引（规格、代码、测试）；
* ``clauses`` —— 规格条目（可单独引用的最小单元，带状态与锚点）；
* ``context_runs`` / ``retrieval_logs`` —— 每次上下文装配的检索审计，
  评审可以直接在库里看到「哪条 SQL 命中了哪条资料」；
* ``task_states`` —— PowerContext 状态的服务端镜像，仅用于审计对账。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import pymysql
from pymysql.cursors import DictCursor

from .config import SeekDbConfig
from .embeddings import EMBEDDING_DIM

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS documents (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project VARCHAR(64) NOT NULL,
      kind VARCHAR(16) NOT NULL,
      path VARCHAR(512) NOT NULL,
      title VARCHAR(255) NOT NULL,
      version VARCHAR(64) NOT NULL DEFAULT '',
      status VARCHAR(32) NOT NULL DEFAULT 'active',
      digest CHAR(64) NOT NULL,
      size_bytes INT NOT NULL DEFAULT 0,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uk_project_path (project, path),
      KEY idx_kind (kind)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS clauses (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project VARCHAR(64) NOT NULL,
      doc_path VARCHAR(512) NOT NULL,
      clause_id VARCHAR(64) NOT NULL,
      title VARCHAR(255) NOT NULL,
      status VARCHAR(32) NOT NULL,
      applies_to VARCHAR(255) NOT NULL DEFAULT '',
      body MEDIUMTEXT NOT NULL,
      code_refs TEXT NOT NULL,
      test_refs TEXT NOT NULL,
      replaced_by VARCHAR(64) NOT NULL DEFAULT '',
      version VARCHAR(64) NOT NULL DEFAULT '',
      digest CHAR(64) NOT NULL,
      UNIQUE KEY uk_doc_clause (doc_path, clause_id),
      KEY idx_status (status),
      KEY idx_clause (clause_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    f"""
    CREATE TABLE IF NOT EXISTS chunks (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project VARCHAR(64) NOT NULL,
      doc_path VARCHAR(512) NOT NULL,
      kind VARCHAR(16) NOT NULL,
      clause_id VARCHAR(64) NOT NULL DEFAULT '',
      symbol VARCHAR(255) NOT NULL DEFAULT '',
      ordinal INT NOT NULL,
      heading VARCHAR(255) NOT NULL DEFAULT '',
      body MEDIUMTEXT NOT NULL,
      embedding VECTOR({EMBEDDING_DIM}) NOT NULL,
      UNIQUE KEY uk_doc_ordinal (doc_path, ordinal),
      KEY idx_chunk_kind (kind),
      FULLTEXT KEY ft_chunk_body (body) WITH PARSER ngram
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS symbols (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      project VARCHAR(64) NOT NULL,
      doc_path VARCHAR(512) NOT NULL,
      name VARCHAR(255) NOT NULL,
      kind VARCHAR(32) NOT NULL,
      start_line INT NOT NULL,
      end_line INT NOT NULL,
      signature VARCHAR(512) NOT NULL DEFAULT '',
      UNIQUE KEY uk_path_symbol (doc_path, name),
      KEY idx_symbol_name (name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
      id VARCHAR(64) PRIMARY KEY,
      title VARCHAR(255) NOT NULL,
      description MEDIUMTEXT NOT NULL,
      status VARCHAR(32) NOT NULL DEFAULT 'open',
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS context_runs (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      task_id VARCHAR(64) NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      latency_ms INT NOT NULL,
      payload MEDIUMTEXT NOT NULL,
      KEY idx_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS retrieval_logs (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      run_id BIGINT NOT NULL,
      channel VARCHAR(16) NOT NULL,
      query VARCHAR(512) NOT NULL,
      doc_path VARCHAR(512) NOT NULL,
      clause_id VARCHAR(64) NOT NULL DEFAULT '',
      score DOUBLE NOT NULL,
      rank_no INT NOT NULL,
      KEY idx_run (run_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS task_states (
      id BIGINT AUTO_INCREMENT PRIMARY KEY,
      task_id VARCHAR(64) NOT NULL,
      kind VARCHAR(24) NOT NULL,
      text MEDIUMTEXT NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      source VARCHAR(32) NOT NULL DEFAULT 'powercontext',
      KEY idx_task_state (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

TABLES = (
    "retrieval_logs",
    "context_runs",
    "chunks",
    "clauses",
    "symbols",
    "documents",
    "tasks",
    "task_states",
)


class SeekDbError(RuntimeError):
    pass


class SeekDb:
    def __init__(self, cfg: SeekDbConfig) -> None:
        self.cfg = cfg
        self._conn: pymysql.connections.Connection | None = None

    # ---------------------------------------------------------------- 连接
    def connect(self) -> pymysql.connections.Connection:
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:  # noqa: BLE001 - 连接失效则重建
                self._conn = None
        try:
            conn = pymysql.connect(
                host=self.cfg.host,
                port=self.cfg.port,
                user=self.cfg.user,
                password=self.cfg.password,
                charset="utf8mb4",
                cursorclass=DictCursor,
                autocommit=True,
                connect_timeout=10,
            )
        except Exception as exc:  # noqa: BLE001
            raise SeekDbError(
                f"无法连接 seekdb {self.cfg.host}:{self.cfg.port}：{exc}。"
                "请先执行 `docker compose up -d seekdb`（见 README 第 3 节）。"
            ) from exc
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.cfg.database}` "
                        "DEFAULT CHARACTER SET utf8mb4")
            cur.execute(f"USE `{self.cfg.database}`")
        self._conn = conn
        return conn

    def ensure_schema(self) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def reset(self) -> None:
        conn = self.connect()
        with conn.cursor() as cur:
            for table in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        self.ensure_schema()

    # ---------------------------------------------------------------- 查询
    @contextmanager
    def cursor(self):
        conn = self.connect()
        cur = conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def query(self, sql: str, args: Sequence[Any] | None = None) -> list[dict]:
        with self.cursor() as cur:
            cur.execute(sql, args or ())
            rows = cur.fetchall()
        return list(rows or [])

    def execute(self, sql: str, args: Sequence[Any] | None = None) -> int:
        with self.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self.cursor() as cur:
            cur.executemany(sql, rows)
            return cur.rowcount

    def scalar(self, sql: str, args: Sequence[Any] | None = None) -> Any:
        rows = self.query(sql, args)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    # ---------------------------------------------------------------- 审计
    def log_context_run(self, task_id: str, latency_ms: int, payload: dict) -> int:
        self.execute(
            "INSERT INTO context_runs (task_id, latency_ms, payload) VALUES (%s, %s, %s)",
            (task_id, latency_ms, json.dumps(payload, ensure_ascii=False)),
        )
        return int(self.scalar("SELECT LAST_INSERT_ID() AS id"))

    def log_retrievals(self, run_id: int, rows: Iterable[dict]) -> None:
        self.executemany(
            "INSERT INTO retrieval_logs (run_id, channel, query, doc_path, clause_id, score, rank_no)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    run_id,
                    row.get("channel", ""),
                    str(row.get("query", ""))[:500],
                    row.get("doc_path", ""),
                    row.get("clause_id", ""),
                    float(row.get("score", 0.0)),
                    int(row.get("rank", 0)),
                )
                for row in rows
            ],
        )

    def stats(self) -> dict[str, int]:
        return {
            table: int(self.scalar(f"SELECT COUNT(*) AS n FROM `{table}`") or 0)
            for table in TABLES
        }

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


def open_db(cfg: SeekDbConfig | None = None, *, ensure: bool = True) -> SeekDb:
    db = SeekDb(cfg or SeekDbConfig.from_env())
    db.connect()
    if ensure:
        db.ensure_schema()
    return db
