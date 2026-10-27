import unittest

from tests.support import PROJECT_ROOT  # noqa: F401
from app.config import Config
from app.seekdb_store import SearchHit, SeekDBStore


def row(chunk_id, distance=None, score=None):
    data = {
        "id": chunk_id,
        "paper_id": f"p-{chunk_id}",
        "title": "测试论文",
        "section": "方法",
        "chunk_index": 0,
        "content": f"内容 {chunk_id}",
        "year": 2024,
        "venue": "示例会议",
        "tags": "检索",
    }
    if distance is not None:
        data["distance"] = distance
    if score is not None:
        data["score"] = score
    return data


class SearchSQLTest(unittest.TestCase):
    def setUp(self):
        self.store = SeekDBStore(Config())
        self.store.vector_index = True
        self.store.fts_available = True
        self.calls = []

        def fake_execute(sql, params=None):
            self.calls.append((sql, params))
            if sql.startswith("SELECT") and "l2_distance" in sql:
                return [row("c1", distance=0.1), row("c2", distance=0.3)]
            if "MATCH" in sql:
                return [row("c2", score=2.0), row("c3", score=1.0)]
            return []

        self.store._execute = fake_execute

    def test_hybrid_search_parameter_order(self):
        result = self.store.search(
            "s1", "混合检索", [0.0] * 384, filters={"year_from": 2020}, top_k=5
        )
        self.assertEqual(result["vector_candidates"], 2)
        self.assertEqual(result["fts_candidates"], 2)
        ids = [hit["chunk_id"] for hit in result["hits"]]
        self.assertEqual(ids[0], "c2")  # 两路都命中，RRF 最高
        self.assertEqual(result["hits"][0]["matched_by"], ["vector", "fulltext"])
        self.assertEqual(result["latency_ms"] >= 0, True)

        fts_call = next(call for call in self.calls if "MATCH" in call[0])
        sql, params = fts_call
        # SQL 顺序：SELECT 里的 AGAINST → WHERE 过滤条件 → WHERE 里的 AGAINST → LIMIT
        self.assertTrue(sql.startswith("SELECT"))
        self.assertEqual(params[0], params[3])
        self.assertEqual(params[1], "s1")
        self.assertEqual(params[2], 2020)
        self.assertEqual(params[-1], self.store.cfg.top_k_fts)
        self.assertIn("year >= %s", sql)

    def test_vector_statement_uses_approximate(self):
        result = self.store.search("s1", "检索", [0.0] * 384, top_k=3)
        self.assertIn("APPROXIMATE", result["vector_statement"])

    def test_retrieval_log_written(self):
        self.store.search("s1", "检索", [0.0] * 384, top_k=3)
        insert_calls = [call for call in self.calls if "INSERT INTO retrieval_logs" in call[0]]
        self.assertEqual(len(insert_calls), 1)
        self.assertEqual(insert_calls[0][1][0], "s1")

    def test_fulltext_fallback_when_index_missing(self):
        self.store.fts_available = False
        result = self.store.search("s1", "检索", [0.0] * 384, top_k=3)
        self.assertEqual(result["fts_candidates"], 0)
        like_call = next(call for call in self.calls if "LIKE" in call[0])
        self.assertIn("content LIKE %s", like_call[0])


class RowMappingTest(unittest.TestCase):
    def test_paper_row(self):
        from datetime import datetime

        mapped = SeekDBStore._paper_row(
            {
                "id": "p1",
                "title": "标题",
                "authors": "作者",
                "year": 2024,
                "venue": "会议",
                "tags": "a,b",
                "source_name": "x.md",
                "created_at": datetime(2026, 1, 2, 3, 4),
                "chunk_count": 7,
            }
        )
        self.assertEqual(mapped["paper_id"], "p1")
        self.assertEqual(mapped["created_at"], "2026-01-02 03:04")
        self.assertEqual(mapped["chunk_count"], 7)


if __name__ == "__main__":
    unittest.main()
