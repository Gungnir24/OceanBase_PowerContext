import unittest

from tests.support import PROJECT_ROOT  # noqa: F401  (设置 sys.path)
from app.ingest import (
    ChunkingOptions,
    chunk_text,
    make_chunk_id,
    make_paper_id,
    parse_document,
)


class ParseDocumentTest(unittest.TestCase):
    def test_front_matter(self):
        text = (
            "---\n"
            "title: 测试论文\n"
            "authors: 张三, 李四\n"
            "year: 2024\n"
            "venue: 示例会议\n"
            "tags: 检索, 记忆\n"
            "---\n\n"
            "## 摘要\n\n这是正文。\n"
        )
        doc = parse_document("paper.md", text)
        self.assertEqual(doc.title, "测试论文")
        self.assertEqual(doc.authors, "张三, 李四")
        self.assertEqual(doc.year, 2024)
        self.assertEqual(doc.venue, "示例会议")
        self.assertEqual(doc.tags, "检索,记忆")
        self.assertIn("这是正文", doc.body)
        self.assertNotIn("title:", doc.body)

    def test_title_from_heading(self):
        doc = parse_document("note.txt", "# 我的笔记\n\n内容")
        self.assertEqual(doc.title, "我的笔记")

    def test_year_invalid(self):
        doc = parse_document("x.md", "---\nyear: 2024年\n---\n正文")
        self.assertIsNone(doc.year)


class ChunkTest(unittest.TestCase):
    def test_sections_are_kept(self):
        body = "## 方法\n\n第一段内容。\n\n## 实验\n\n第二段内容。\n"
        chunks = chunk_text(body)
        self.assertEqual([chunk.section for chunk in chunks], ["方法", "实验"])
        self.assertTrue(all(chunk.content for chunk in chunks))

    def test_long_paragraph_is_split(self):
        paragraph = "这是一句很长的中文。" * 120
        chunks = chunk_text(paragraph, ChunkingOptions(max_chars=200, min_chars=10))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.content) <= 260 for chunk in chunks))

    def test_ids_are_stable(self):
        first = make_paper_id("a.md", "标题")
        second = make_paper_id("a.md", "标题")
        self.assertEqual(first, second)
        self.assertNotEqual(first, make_paper_id("b.md", "标题"))
        self.assertEqual(make_chunk_id(first, 3), f"{first}-003")

    def test_empty_document(self):
        self.assertEqual(chunk_text("   \n\n"), [])


if __name__ == "__main__":
    unittest.main()
