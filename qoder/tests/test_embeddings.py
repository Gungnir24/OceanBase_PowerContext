import math
import unittest

from tests.support import PROJECT_ROOT  # noqa: F401
from app.embeddings import (
    LocalHashEmbedder,
    cosine,
    l2_normalize,
    tokenize,
    tokens_for_query,
    vector_literal,
)


class EmbeddingTest(unittest.TestCase):
    def setUp(self):
        self.embedder = LocalHashEmbedder(128)

    def test_dimension_and_norm(self):
        vector = self.embedder.embed("混合检索与记忆")
        self.assertEqual(len(vector), 128)
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0, places=6)

    def test_deterministic(self):
        a = self.embedder.embed("RRF 融合 k=60")
        b = self.embedder.embed("RRF 融合 k=60")
        self.assertEqual(a, b)

    def test_similar_text_scores_higher(self):
        query = self.embedder.embed("混合检索的召回率")
        near = self.embedder.embed("混合检索召回率实验")
        far = self.embedder.embed("单卡显存与批大小")
        self.assertGreater(cosine(query, near), cosine(query, far))

    def test_cosine_edge_cases(self):
        self.assertEqual(cosine([0, 0], [1, 2]), 0.0)
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1.0)

    def test_tokenize_chinese_bigrams(self):
        tokens = tokenize("知识蒸馏")
        self.assertIn("知识", tokens)
        self.assertIn("蒸馏", tokens)

    def test_tokens_for_query_dedupes(self):
        tokens = tokens_for_query("检索 检索 记忆")
        self.assertEqual(len(tokens), len(set(tokens)))

    def test_vector_literal(self):
        self.assertEqual(vector_literal([0.5, -1.0]), "[0.500000,-1.000000]")
        with self.assertRaises(ValueError):
            vector_literal([float("nan")])

    def test_normalize_zero(self):
        self.assertEqual(l2_normalize([0.0, 0.0]), [0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
