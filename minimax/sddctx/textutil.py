"""文本工具：分词、摘要、引用解析。

检索面向中英混排的技术文档，不引入第三方分词器：
* 中文按「连续汉字串的 1-gram + 2-gram」切；
* 英文/数字按词切，保留 ``usb-c`` 这类连字符词；
* 这样既能让 seekdb 的 ngram 全文索引命中，也能让向量哈希稳定复现。
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
LATIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*")
WS_RE = re.compile(r"\s+")

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "需要", "进行",
    "实现", "增加", "支持", "使用", "可以", "以及", "一个", "我们",
}


def normalize(text: str) -> str:
    return WS_RE.sub(" ", text or "").strip()


def tokenize(text: str) -> list[str]:
    """返回去重保序的词元列表（中文 bi-gram 优先）。"""
    tokens: list[str] = []
    seen: set[str] = set()

    def push(token: str) -> None:
        token = token.strip().lower()
        if len(token) < 2 or token in STOPWORDS or token in seen:
            return
        seen.add(token)
        tokens.append(token)

    for run in CJK_RE.findall(text or ""):
        if len(run) == 1:
            push(run)
            continue
        for index in range(len(run) - 1):
            push(run[index : index + 2])
    for word in LATIN_RE.findall(text or ""):
        push(word)
    return tokens


def term_text(text: str, limit: int = 24) -> str:
    """给全文索引用空格分隔的词元串。"""
    return " ".join(tokenize(text)[:limit])


def boolean_query(text: str, limit: int = 16) -> str:
    """构造 MySQL boolean 模式查询串。"""
    return " ".join(tokenize(text)[:limit])


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(text: str, limit: int = 200) -> str:
    flat = normalize(text)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def split_ref(ref: str) -> tuple[str, str]:
    """``path#symbol`` -> (path, symbol)；没有 ``#`` 时 symbol 为空串。"""
    ref = (ref or "").strip()
    if "#" in ref:
        path, symbol = ref.split("#", 1)
        return path.strip(), symbol.strip()
    return ref, ""


def iter_refs(raw: str) -> Iterable[str]:
    for piece in re.split(r"[,，;；\s]+", raw or ""):
        piece = piece.strip()
        if piece:
            yield piece
