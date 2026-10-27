"""文档解析与切片。

支持 Markdown / 纯文本。文件头部的简单 front matter（--- 包裹的 key: value）
用于填写标题、作者、年份、会议与标签；没有 front matter 时从一级标题推断标题。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, List

_FRONT_MATTER_KEYS = {
    "title": "title",
    "authors": "authors",
    "author": "authors",
    "year": "year",
    "venue": "venue",
    "tags": "tags",
    "tag": "tags",
}

_SENTENCE_END = re.compile(r"(?<=[。！？；!?;])|\n+")


@dataclass
class ParsedDocument:
    title: str
    body: str
    authors: str = ""
    year: int | None = None
    venue: str = ""
    tags: str = ""
    source_name: str = ""

    def as_row(self) -> dict:
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "tags": self.tags,
            "source_name": self.source_name,
        }


@dataclass
class TextChunk:
    index: int
    content: str
    section: str = ""

    def to_dict(self) -> dict:
        return {"index": self.index, "content": self.content, "section": self.section}


@dataclass
class ChunkingOptions:
    max_chars: int = 520
    overlap_chars: int = 80
    min_chars: int = 60


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.lstrip().startswith("---"):
        return {}, text
    stripped = text.lstrip("\ufeff \t\n")
    lines = stripped.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    end = None
    for position in range(1, len(lines)):
        line = lines[position].strip()
        if line == "---":
            end = position
            break
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip().strip('"').strip("'")
    if end is None:
        return {}, text
    return meta, "\n".join(lines[end + 1 :])


def parse_document(name: str, text: str) -> ParsedDocument:
    meta, body = _split_front_matter(text)
    title = meta.get("title", "").strip()
    if not title:
        heading = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = heading.group(1).strip() if heading else (name.rsplit(".", 1)[0] or "未命名文档")

    year: int | None = None
    raw_year = meta.get("year", "").strip()
    if raw_year.isdigit():
        year = int(raw_year)

    tags = meta.get("tags", "").strip()
    if tags:
        tags = ",".join(part.strip() for part in re.split(r"[,，;；\s]+", tags) if part.strip())

    return ParsedDocument(
        title=title,
        body=body.strip(),
        authors=meta.get("authors", "").strip(),
        year=year,
        venue=meta.get("venue", "").strip(),
        tags=tags,
        source_name=name,
    )


def _sections(body: str) -> Iterable[tuple[str, str]]:
    """按 Markdown 二级及以下标题切段，产出 (section, paragraph)。"""
    current = ""
    buffer: List[str] = []

    def flush() -> Iterable[tuple[str, str]]:
        text = "\n".join(buffer).strip()
        if text:
            yield current, text

    for line in body.split("\n"):
        heading = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if heading:
            yield from flush()
            buffer = []
            current = heading.group(2).strip()
            continue
        if not line.strip():
            yield from flush()
            buffer = []
            continue
        buffer.append(line)
    yield from flush()


def _split_long(text: str, max_chars: int) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    pieces: List[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        if not sentence:
            continue
        if len(current) + len(sentence) <= max_chars:
            current += sentence
            continue
        if current:
            pieces.append(current)
        while len(sentence) > max_chars:
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        current = sentence
    if current:
        pieces.append(current)
    return pieces


def chunk_text(body: str, options: ChunkingOptions | None = None) -> List[TextChunk]:
    options = options or ChunkingOptions()
    chunks: List[TextChunk] = []
    for section, paragraph in _sections(body):
        for piece in _split_long(paragraph, options.max_chars):
            piece = piece.strip()
            if not piece:
                continue
            if chunks and len(piece) < options.min_chars:
                # 过短的段落并入上一块，避免产生无信息的切片；跨小节不合并。
                previous = chunks[-1]
                if (
                    previous.section == section
                    and len(previous.content) + len(piece) + 2 <= options.max_chars + 120
                ):
                    previous.content = f"{previous.content}\n{piece}"
                    continue
            chunks.append(TextChunk(index=len(chunks), content=piece, section=section))
    if not chunks and body.strip():
        chunks.append(TextChunk(index=0, content=body.strip(), section=""))
    return chunks


def make_paper_id(name: str, title: str) -> str:
    digest = hashlib.sha1(f"{name}\x00{title}".encode("utf-8")).hexdigest()
    return digest[:16]


def make_chunk_id(paper_id: str, index: int) -> str:
    return f"{paper_id}-{index:03d}"


def strip_markdown(text: str) -> str:
    """把 Markdown 粗体/链接等符号去掉，用于生成更干净的回答摘录。"""
    text = re.sub(r"!\[[^\]]*]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", text)
    text = re.sub(r"[`*_>#]", "", text)
    return re.sub(r"\n{2,}", "\n", text).strip()
