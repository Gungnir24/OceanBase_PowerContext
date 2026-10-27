"""真实 seekdb 集成测试（默认跳过）。

跑法：

    # 1) 起库
    docker compose up -d seekdb
    # 2) 打开开关
    set SEEKDB_LIVE=1          # PowerShell: $env:SEEKDB_LIVE="1"
    python -m unittest tests.test_seekdb_live -v

它会真的建表、导入示例文献、执行向量 + 全文混合检索，并断言：
两路候选都 > 0、返回的每条命中都带非空标题、检索日志落库。
单测里的 `_execute` mock 掉以后是发现不了 `Unknown column 'title'` 这类问题的，
这条用例专门补上这个缺口。
"""

import os
import unittest

from tests.support import PROJECT_ROOT
from app.config import Config
from app.embeddings import get_embedder
from app.ingest import chunk_text, make_chunk_id, make_paper_id, parse_document
from app.seekdb_store import SeekDBError, SeekDBStore

LIVE = os.environ.get("SEEKDB_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}
DATABASE = os.environ.get("SEEKDB_LIVE_DATABASE", "researchnest_live_test")
SCOPE = "live-test-scope"
QUESTION = "混合检索里 RRF 融合的 k 值应该怎么选？有实验数据吗？"


@unittest.skipUnless(LIVE, "设置 SEEKDB_LIVE=1 才会执行真实 seekdb 集成测试")
class LiveSeekDBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config()
        cfg.seekdb_database = DATABASE
        cls.cfg = cfg
        cls.store = SeekDBStore(cfg)
        try:
            cls.store.ensure_schema()
        except SeekDBError as exc:  # pragma: no cover - 环境没起时给出明确指引
            raise unittest.SkipTest(f"seekdb 不可用：{exc}") from exc
        cls.embedder = get_embedder(cfg)
        cls.paper_ids = []

    @classmethod
    def tearDownClass(cls):
        for paper_id in cls.paper_ids:
            try:
                cls.store.delete_paper(SCOPE, paper_id)
            except Exception:  # noqa: BLE001 - 清理失败不影响结论
                pass

    def _ingest(self, path):
        text = path.read_text(encoding="utf-8")
        document = parse_document(path.name, text)
        chunks = chunk_text(document.body)
        paper_id = make_paper_id(path.name, document.title)
        vectors = self.embedder.embed_many([chunk.content for chunk in chunks])
        rows = [
            (make_chunk_id(paper_id, chunk.index), chunk.index, chunk.section,
             chunk.content, vector)
            for chunk, vector in zip(chunks, vectors)
        ]
        count = self.store.upsert_paper(SCOPE, paper_id, document, rows)
        self.paper_ids.append(paper_id)
        return document, count

    def test_schema_has_title_and_indexes(self):
        health = self.store.health()
        self.assertTrue(health["ok"])
        self.assertTrue(health["fts_available"], "全文索引没建起来，检索会退化成 LIKE")
        self.assertTrue(health["vector_index"], "向量索引没建起来，ANN 会退化成精确扫描")

    def test_ingest_search_returns_evidence_with_title(self):
        samples = list((PROJECT_ROOT / "data" / "samples").rglob("*.md"))
        self.assertTrue(samples, "缺少示例材料")
        for path in samples:
            _, count = self._ingest(path)
            self.assertGreater(count, 0, f"{path.name} 没有切片")

        result = self.store.search(
            SCOPE, QUESTION, self.embedder.embed(QUESTION), top_k=6
        )
        self.assertEqual(result["warning"], "", f"检索通道报错：{result['warning']}")
        self.assertGreater(result["vector_candidates"], 0, "向量通道没有候选")
        self.assertGreater(result["fts_candidates"], 0, "全文通道没有候选")
        self.assertTrue(result["hits"], "融合后没有证据")
        for hit in result["hits"]:
            self.assertTrue(hit["paper_id"])
            self.assertTrue(hit["title"], f"命中缺少标题：{hit['chunk_id']}")
            self.assertIn("matched_by", hit)

    def test_retrieval_log_written(self):
        self.store.search(SCOPE, QUESTION, self.embedder.embed(QUESTION), top_k=3)
        logs = self.store.recent_logs(SCOPE, limit=5)
        self.assertTrue(logs, "没有写入 retrieval_logs")
        latest = logs[0]
        self.assertIn("vector", latest["mode"])
        self.assertTrue(latest["fused_ids"], "融合结果为空")


if __name__ == "__main__":
    unittest.main()
