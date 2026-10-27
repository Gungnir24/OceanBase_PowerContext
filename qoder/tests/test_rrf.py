import unittest

from tests.support import PROJECT_ROOT  # noqa: F401
from app.config import Config
from app.seekdb_store import SearchHit, SeekDBStore, _sanitize_term


def hit(chunk_id, title="论文", content="内容"):
    return SearchHit(
        chunk_id=chunk_id,
        paper_id=f"p-{chunk_id}",
        title=title,
        section="方法",
        chunk_index=0,
        content=content,
    )


class RRFFusionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.store = SeekDBStore(self.cfg)

    def test_vector_only(self):
        fused = self.store._rrf([hit("a"), hit("b")], [], top_k=5)
        self.assertEqual([item.chunk_id for item in fused], ["a", "b"])
        self.assertEqual(fused[0].matched_by, ["vector"])

    def test_overlap_wins(self):
        fused = self.store._rrf(
            [hit("a"), hit("b"), hit("c")],
            [hit("c"), hit("d")],
            top_k=2,
        )
        self.assertEqual([item.chunk_id for item in fused], ["c", "a"])
        self.assertEqual(fused[0].matched_by, ["vector", "fulltext"])

    def test_fulltext_only_candidate(self):
        fused = self.store._rrf([hit("a")], [hit("b")], top_k=5)
        self.assertEqual(len(fused), 2)
        self.assertIsNone(fused[1].distance)

    def test_top_k(self):
        fused = self.store._rrf([hit(str(i)) for i in range(10)], [], top_k=3)
        self.assertEqual(len(fused), 3)

    def test_sanitize_term(self):
        self.assertEqual(_sanitize_term('+RRF"'), "RRF")


class FilterClauseTest(unittest.TestCase):
    def setUp(self):
        self.store = SeekDBStore(Config())

    def test_scope_only(self):
        where, params = self.store._filter_clause("s1", {})
        self.assertEqual(where, "scope_id = %s")
        self.assertEqual(params, ("s1",))

    def test_structured_filters(self):
        where, params = self.store._filter_clause(
            "s1", {"year_from": 2020, "year_to": 2026, "venue": "会议", "tag": "检索"}
        )
        self.assertIn("year >= %s", where)
        self.assertIn("year <= %s", where)
        self.assertIn("venue LIKE %s", where)
        self.assertIn("tags LIKE %s", where)
        self.assertEqual(params, ("s1", 2020, 2026, "%会议%", "%检索%"))

    def test_invalid_years_ignored(self):
        where, params = self.store._filter_clause("s1", {"year_from": "2020"})
        self.assertEqual(where, "scope_id = %s")
        self.assertEqual(params, ("s1",))


if __name__ == "__main__":
    unittest.main()
