"""文本向量化。

默认使用零依赖的本地哈希向量（feature hashing），保证评审在没有 Embedding
API Key 的情况下也能完整跑通“向量检索”链路；配置 EMBEDDING_PROVIDER=openai
后改用任意 OpenAI 兼容的 /embeddings 接口。

本地向量不是语义模型，它把中文单字/二元组与英文词元哈希到固定维度，再做 L2
归一化。优点是确定、可离线、零成本；局限是只能捕捉词面相近，论文里的同义改写
召回不如真实嵌入模型。README 的“技术说明”一节点明了这一点，混合检索中的
全文通道与结构化过滤可以弥补。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
import urllib.error
import urllib.request
from typing import Iterable, List, Sequence

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[a-z0-9_][a-z0-9_+\-.]*")
_SPACE = re.compile(r"\s+")


class EmbeddingError(RuntimeError):
    pass


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


def tokenize(text: str) -> List[str]:
    """中英混合分词：中文取单字 + 相邻二元组，英文取词。"""
    text = _normalize(text)
    tokens: List[str] = []
    for match in _CJK.finditer(text):
        run = match.group(0)
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    for match in _WORD.finditer(text):
        token = match.group(0).strip(".-+")
        if token:
            tokens.append(token)
    return tokens


def _hash_feature(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


def l2_normalize(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 1e-12:
        return [0.0] * len(vector)
    return [v / norm for v in vector]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("向量维度不一致")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return dot / (na * nb)


class LocalHashEmbedder:
    """确定性本地向量器（feature hashing）。"""

    name = "local-hashing"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> List[float]:
        counts: dict[str, int] = {}
        for token in tokenize(text):
            counts[token] = counts.get(token, 0) + 1
        vector = [0.0] * self.dim
        for token, count in counts.items():
            index, sign = _hash_feature(token, self.dim)
            vector[index] += sign * (1.0 + math.log(count))
        return l2_normalize(vector)

    def embed_many(self, texts: Iterable[str]) -> List[List[float]]:
        return [self.embed(text) for text in texts]


class OpenAIEmbedder:
    """任意 OpenAI 兼容 /embeddings 接口。"""

    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dim: int,
        timeout: int = 60,
    ) -> None:
        if not api_key:
            raise EmbeddingError("EMBEDDING_PROVIDER=openai 需要 EMBEDDING_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout = timeout

    def embed_many(self, texts: Iterable[str]) -> List[List[float]]:
        items = list(texts)
        if not items:
            return []
        body = json.dumps({"model": self.model, "input": items}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise EmbeddingError(f"Embedding HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 - 网络错误统一转成可读异常
            raise EmbeddingError(f"Embedding 请求失败: {exc}") from exc

        data = payload.get("data") or []
        vectors: List[List[float]] = []
        for item in sorted(data, key=lambda entry: entry.get("index", 0)):
            vector = [float(v) for v in item.get("embedding", [])]
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"Embedding 维度为 {len(vector)}，与 EMBEDDING_DIM={self.dim} 不一致"
                )
            vectors.append(l2_normalize(vector))
        if len(vectors) != len(items):
            raise EmbeddingError("Embedding 返回条数与请求不一致")
        return vectors

    def embed(self, text: str) -> List[float]:
        return self.embed_many([text])[0]


def get_embedder(cfg) -> object:
    if cfg.embedding_provider == "openai":
        return OpenAIEmbedder(
            cfg.embedding_base_url,
            cfg.embedding_api_key,
            cfg.embedding_model,
            cfg.vector_dim,
        )
    return LocalHashEmbedder(cfg.vector_dim)


def vector_literal(vector: Sequence[float]) -> str:
    """把向量格式化为 seekdb 的数组字面量；只接受有限数值。"""
    parts: List[str] = []
    for value in vector:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("向量包含非法数值")
        parts.append(f"{number:.6f}")
    return "[" + ",".join(parts) + "]"


def tokens_for_query(text: str) -> List[str]:
    """全文检索用的关键词串，去掉重复并保留顺序。"""
    seen: set[str] = set()
    result: List[str] = []
    for token in tokenize(text):
        if len(token) < 2 and not token.isascii():
            continue
        if token not in seen:
            seen.add(token)
            result.append(token)
    return result[:24]


__all__ = [
    "EmbeddingError",
    "LocalHashEmbedder",
    "OpenAIEmbedder",
    "cosine",
    "get_embedder",
    "l2_normalize",
    "tokenize",
    "tokens_for_query",
    "vector_literal",
]
