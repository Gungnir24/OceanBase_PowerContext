"""表结构契约测试：检索 SQL 里出现的列，必须在建表语句里真实存在。

这个用例不需要数据库，专门拦截一类真实事故：`chunks` 表没有 `title` 列，但
向量/全文两条检索 SQL 都 `SELECT title`，导致 seekdb 抛
`1054 Unknown column 'title' in 'field list'`，两路候选全空，问答拿不到任何证据。
"""

import re
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401
from app.config import Config
from app.seekdb_store import SeekDBStore
from app.seekdb_store import __file__ as STORE_FILE

_DDL = re.compile(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\)\s*DEFAULT CHARSET", re.S)
_COLUMN = re.compile(r"^\s*([a-z][a-z0-9_]*)\s+[A-Z]", re.M)
SIMPLE_COLUMN = re.compile(r"^[a-z][a-z0-9_]*$")


def schema_columns():
    """从建表用的 DDL 里解析出每张表的列名。"""
    with open(STORE_FILE, encoding="utf-8") as handle:
        source = handle.read()
    tables = {}
    for name, body in _DDL.findall(source):
        columns = set()
        for line in body.splitlines():
            match = _COLUMN.match(line)
            if match:
                columns.add(match.group(1).lower())
        # 索引/键定义行不是列
        columns -= {"key", "primary", "unique", "vector", "fulltext", "constraint", "index"}
        tables[name.lower()] = columns
    return tables


def select_columns(sql):
    """取出 `SELECT ... FROM <table>` 中的裸列名，忽略函数表达式与别名。"""
    match = re.search(r"SELECT\s+(.*?)\s+FROM\s+(\w+)", sql, re.S | re.I)
    if not match:
        return None, set()
    body, table = match.group(1), match.group(2)
    names = set()
    for raw in body.split(","):
        piece = raw.strip()
        if "(" in piece:
            continue  # 距离/打分表达式由数据库函数产出，不校验
        token = piece.split()[0].strip("`")
        if SIMPLE_COLUMN.match(token):
            names.add(token.lower())
    return table.lower(), names


class SchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = schema_columns()
        cls.store = SeekDBStore(Config())
        cls.store.vector_index = True
        cls.store.fts_available = True
        cls.captured = []

        def fake_execute(sql, params=None):
            cls.captured.append(sql)
            return []

        cls.store._execute = fake_execute

    def test_ddl_parsed(self):
        self.assertIn("chunks", self.tables)
        self.assertIn("title", self.tables["chunks"], "chunks 必须自带 title 供引用使用")
        self.assertIn("content", self.tables["chunks"])

    def _check_sql(self, sql, label):
        table, columns = select_columns(sql)
        self.assertTrue(columns, f"{label} 没解析出列：{sql[:80]}")
        self.assertIn(table, self.tables, f"{label} 引用了未知表 {table}")
        missing = columns - self.tables[table]
        self.assertFalse(missing, f"{label} 引用了不存在的列 {sorted(missing)}（表 {table}）")

    def test_vector_and_fulltext_sql_columns_exist(self):
        self.store.search("s1", "混合检索 RRF", [0.0] * 384, top_k=3)
        self.assertTrue(self.captured, "search 没有产生 SQL")
        for sql in self.captured:
            upper = sql.upper()
            if upper.startswith("SELECT") and " FROM CHUNKS" in upper:
                self._check_sql(sql, "检索 SQL")

    def test_like_fallback_sql_columns_exist(self):
        self.captured.clear()
        self.store.fts_available = False
        try:
            self.store.search("s1", "混合检索", [0.0] * 384, top_k=3)
        finally:
            self.store.fts_available = True
        like_sqls = [sql for sql in self.captured if "LIKE" in sql.upper()]
        self.assertTrue(like_sqls, "没有触发 LIKE 退化路径")
        for sql in like_sqls:
            self._check_sql(sql, "LIKE 退化 SQL")

    def test_row_fields_match_ddl(self):
        """_hit_from_row 读取的键必须来自检索列，否则会在映射时 KeyError。"""
        row = {
            "id": "c1", "paper_id": "p1", "title": "t", "section": "",
            "chunk_index": 0, "content": "x", "year": 2024, "venue": "", "tags": "",
        }
        hit = SeekDBStore._hit_from_row(row, vector_rank=1, distance=0.1)
        self.assertEqual(hit.title, "t")
        for name in ("id", "paper_id", "title"):
            self.assertIn(name, self.tables["chunks"])


if __name__ == "__main__":
    unittest.main()
