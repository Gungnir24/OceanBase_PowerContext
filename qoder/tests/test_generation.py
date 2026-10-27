import unittest

from tests.support import PROJECT_ROOT  # noqa: F401
from app.config import Config
from app.generation import Generator


def hit(chunk_id="c1", title="混合检索论文", section="实验", index=1):
    return {
        "chunk_id": chunk_id,
        "paper_id": "p1",
        "title": title,
        "section": section,
        "chunk_index": index,
        "content": "在 1,200 篇中文论文上，RRF 融合把 Recall@10 从 0.71 提升到 0.83。延迟为 26 ms。",
        "year": 2024,
        "venue": "示例会议",
        "tags": "检索",
        "distance": 0.2,
        "vector_rank": 1,
        "fts_rank": None,
        "rrf_score": 0.03,
        "matched_by": ["vector", "fulltext"],
    }


class AnswerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.cfg.llm_api_key = ""
        self.generator = Generator(self.cfg)

    def test_extractive_answer_has_citation_and_memory(self):
        result = self.generator.answer(
            "RRF 融合效果如何？",
            [hit()],
            [{"text": "偏好：先结论后依据"}],
            {"content": "记忆上下文"},
        )
        self.assertEqual(result["engine"], "extractive")
        self.assertIn("[1]", result["answer"])
        self.assertIn("偏好：先结论后依据", result["answer"])
        self.assertEqual(result["citations"][0]["chunk_id"], "c1")
        self.assertIn("Recall@10", result["answer"])

    def test_no_hits(self):
        result = self.generator.answer("无关问题", [], [], None)
        self.assertIn("没有检索到", result["answer"])
        self.assertEqual(result["citations"], [])

    def test_weekly_sections(self):
        result = self.generator.weekly_report(
            days=7,
            activity=[
                {"kind": "ask", "title": "RRF 怎么选 k", "detail": "", "created_at": "2026-10-27 10:00"},
                {"kind": "ingest", "title": "导入论文", "detail": "", "created_at": "2026-10-26 09:00"},
            ],
            hits=[dict(hit(), query="RRF")],
            memories=[{"text": "下周计划补向量维度对比"}],
            stats={"papers": 4, "chunks": 24, "searches": 3, "activities": 2},
            prepared=None,
        )
        markdown = result["markdown"]
        for section in ("本周概览", "阅读与检索", "关键发现", "进展与阻塞", "下周计划"):
            self.assertIn(section, markdown)
        self.assertIn("[1]", markdown)
        self.assertIn("下周计划补向量维度对比", markdown)


if __name__ == "__main__":
    unittest.main()
