"""确定性哈希向量。

不调用外部 embedding 服务，保证「干净环境 + 无网络」也能复现同一份上下文。
实现是标准的 feature hashing：
1. 把文本切成词元（见 ``textutil.tokenize``）；
2. 每个词元做 blake2b，取前 8 字节决定维度下标与符号；
3. 累加后做 L2 归一化。

它不追求语义泛化，只承担「词面相近的片段在向量空间里也相近」，
真正的语义理解交给规格里的结构化锚点与全文检索。需要更强召回时可用
``MINIMAX_EMBEDDING_MODEL`` 切换到远端 embedding（见 README「可选扩展」）。
"""

from __future__ import annotations

import hashlib
import math

from .textutil import tokenize

EMBEDDING_DIM = 384


def embed(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    vector = [0.0] * dim
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        payload = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(payload[:4], "big") % dim
        sign = 1.0 if payload[4] & 1 else -1.0
        weight = 1.0 + min(len(token), 8) / 8.0
        vector[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def to_literal(vector: list[float]) -> str:
    """转成 seekdb ``VECTOR`` 字段可接受的字面量。"""
    return "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
