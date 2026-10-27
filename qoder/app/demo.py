"""演示模式：不依赖 seekdb / PowerContext 的内存实现。

仅用于两类场景：
1. 评审在未启动数据库时先预览界面与交互；
2. 开发时快速验证前端。

页面顶部会明确显示“演示模式”，真实检索与记忆仍需 live 模式。演示模式使用与
正式模式相同的数据结构与融合逻辑（余弦相似度 + 关键词重合 + RRF），因此交互
与真实模式一致。
"""

from __future__ import annotations

import json
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .embeddings import cosine, tokenize


def _keyword_score(query: str, text: str) -> float:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    text_tokens = set(tokenize(text))
    return len(query_tokens & text_tokens) / len(query_tokens)


class MemorySeekStore:
    """与 SeekDBStore 同构的内存版，便于离线预览。"""

    def __init__(self) -> None:
        self.papers: Dict[str, Dict[str, Any]] = {}
        self.chunks: List[Dict[str, Any]] = []
        self.activities: List[Dict[str, Any]] = []
        self.logs: List[Dict[str, Any]] = []
        self.fts_available = True
        self.vector_index = True
        self.warnings: List[str] = []
        self._activity_id = 0
        self._log_id = 0

    def ensure_schema(self) -> None:
        return None

    def health(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "version": "memory-demo",
            "host": "in-memory",
            "database": "memory",
            "fts_available": True,
            "vector_index": True,
            "warnings": [],
            "demo": True,
        }

    def upsert_paper(self, scope_id, paper_id, document, chunks: Iterable[tuple]) -> int:
        self.chunks = [chunk for chunk in self.chunks if chunk["paper_id"] != paper_id]
        count = 0
        for chunk_id, index, section, content, vector in chunks:
            self.chunks.append(
                {
                    "chunk_id": chunk_id,
                    "paper_id": paper_id,
                    "title": document.title,
                    "section": section,
                    "chunk_index": index,
                    "content": content,
                    "embedding": list(vector),
                    "year": document.year,
                    "venue": document.venue,
                    "tags": document.tags,
                    "created_at": datetime.now(),
                }
            )
            count += 1
        self.papers[paper_id] = {
            "paper_id": paper_id,
            "title": document.title,
            "authors": document.authors,
            "year": document.year,
            "venue": document.venue,
            "tags": document.tags,
            "source_name": document.source_name,
            "content": document.body,
            "created_at": datetime.now(),
            "chunk_count": count,
        }
        return count

    def delete_paper(self, scope_id: str, paper_id: str) -> bool:
        existed = paper_id in self.papers
        self.papers.pop(paper_id, None)
        self.chunks = [chunk for chunk in self.chunks if chunk["paper_id"] != paper_id]
        return existed

    def list_papers(self, scope_id: str) -> List[Dict[str, Any]]:
        papers = []
        for paper in self.papers.values():
            item = dict(paper)
            created = item.get("created_at")
            item["created_at"] = created.strftime("%Y-%m-%d %H:%M") if created else ""
            papers.append(item)
        return sorted(papers, key=lambda item: item.get("created_at", ""), reverse=True)

    def get_paper(self, scope_id: str, paper_id: str) -> Optional[Dict[str, Any]]:
        paper = self.papers.get(paper_id)
        if not paper:
            return None
        item = dict(paper)
        item["created_at"] = paper["created_at"].strftime("%Y-%m-%d %H:%M")
        item["chunks"] = [
            {
                "chunk_id": chunk["chunk_id"],
                "chunk_index": chunk["chunk_index"],
                "section": chunk["section"],
                "content": chunk["content"],
            }
            for chunk in self.chunks
            if chunk["paper_id"] == paper_id
        ]
        return item

    def search(
        self,
        scope_id: str,
        query: str,
        embedding: Sequence[float],
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 6,
    ) -> Dict[str, Any]:
        filters = filters or {}
        started = time.perf_counter()
        vector_hits = []
        fts_hits = []
        for chunk in self.chunks:
            if not _match_filters(chunk, filters):
                continue
            distance = max(0.0, 2.0 - 2.0 * cosine(embedding, chunk["embedding"]))
            vector_hits.append((distance, chunk))
            keyword = _keyword_score(query, chunk["content"])
            if keyword > 0:
                fts_hits.append((keyword, chunk))
        vector_hits.sort(key=lambda item: item[0])
        fts_hits.sort(key=lambda item: item[0], reverse=True)
        vector_hits = vector_hits[:12]
        fts_hits = fts_hits[:12]

        scores: Dict[str, float] = {}
        merged: Dict[str, Dict[str, Any]] = {}
        for rank, (distance, chunk) in enumerate(vector_hits, start=1):
            item = _hit(chunk, distance=distance, vector_rank=rank)
            merged[chunk["chunk_id"]] = item
            scores[chunk["chunk_id"]] = 1.0 / (60 + rank)
            item["matched_by"] = ["vector"]
        for rank, (score, chunk) in enumerate(fts_hits, start=1):
            item = merged.get(chunk["chunk_id"]) or _hit(chunk)
            item["fts_rank"] = rank
            item["matched_by"] = list(dict.fromkeys(item["matched_by"] + ["fulltext"]))
            merged[chunk["chunk_id"]] = item
            scores[chunk["chunk_id"]] = scores.get(chunk["chunk_id"], 0.0) + 1.0 / (60 + rank)
        fused = sorted(merged.values(), key=lambda item: scores[item["chunk_id"]], reverse=True)[
            : max(1, top_k)
        ]
        for item in fused:
            item["rrf_score"] = round(scores[item["chunk_id"]], 6)

        latency = int((time.perf_counter() - started) * 1000)
        self._log_id += 1
        self.logs.insert(
            0,
            {
                "id": self._log_id,
                "query": query,
                "filters": filters,
                "vector_ids": [chunk["chunk_id"] for _, chunk in vector_hits],
                "fts_ids": [chunk["chunk_id"] for _, chunk in fts_hits],
                "fused_ids": [
                    {"id": item["chunk_id"], "rrf": item["rrf_score"],
                     "matched_by": item["matched_by"]}
                    for item in fused
                ],
                "mode": "vector+fts (demo)",
                "latency_ms": latency,
                "note": "演示模式：内存检索",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        return {
            "query": query,
            "filters": filters,
            "hits": fused,
            "vector_candidates": len(vector_hits),
            "fts_candidates": len(fts_hits),
            "latency_ms": latency,
            "vector_statement": "SELECT ... l2_distance(embedding, [...]) ... (demo)",
            "fts_statement": "SELECT ... MATCH(content) AGAINST('...') ... (demo)",
            "fts_query": " ".join(sorted(set(tokenize(query)))[:8]),
            "warning": "",
            "fts_available": True,
        }

    def log_activity(self, scope_id, kind, ref_id="", title="", detail="") -> None:
        self._activity_id += 1
        self.activities.insert(
            0,
            {
                "id": self._activity_id,
                "kind": kind,
                "ref_id": ref_id,
                "title": title,
                "detail": detail,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            },
        )

    def weekly_activity(self, scope_id: str, days: int = 7) -> List[Dict[str, Any]]:
        since = datetime.now() - timedelta(days=days)
        results = []
        for item in self.activities:
            try:
                created = datetime.strptime(item["created_at"], "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if created >= since:
                results.append(item)
        return results

    def stats(self, scope_id: str) -> Dict[str, Any]:
        return {
            "papers": len(self.papers),
            "chunks": len(self.chunks),
            "searches": len(self.logs),
            "activities": len(self.activities),
        }

    def recent_logs(self, scope_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self.logs[:limit]


class MemoryPowerContext:
    """与 PowerContextClient 同构的内存版。"""

    def __init__(self) -> None:
        self.scope_id = "demo-scope"
        self._calls: List[Dict[str, Any]] = []
        self._entries: List[Dict[str, Any]] = []
        self._counter = 0
        self._seed()

    def _seed(self) -> None:
        seeds = [
            ("profile", "研究方向：多模态检索增强生成（RAG），重点在中文长文档的混合检索与可追溯引用。"),
            ("preference", "回答偏好：中文、先结论后依据；每条事实必须带文献出处，不接受没有出处的断言。"),
            ("constraint", "实验约束：单卡 24GB 显存，向量索引优先 HNSW，batch size 不超过 64。"),
            ("status", "本周进展：完成了全文检索通道的消融，Recall@10 从 0.71 提升到 0.83；下周计划补向量维度对比。"),
            ("decision", "决定：周报固定包含「关键发现（带出处）」与「进展与阻塞」两节，直接复用检索结果。"),
        ]
        for kind, text in seeds:
            self._append(kind, text)

    def _append(self, kind: str, text: str) -> Dict[str, Any]:
        self._counter += 1
        entry = {
            "kind": kind,
            "text": text,
            "state": "active",
            "version": 1,
            "entry_id": f"demo-entry-{self._counter}",
            "citation": {
                "entry_id": f"demo-entry-{self._counter}",
                "entry_version_id": f"demo-version-{self._counter}",
                "memory_ref": {"family": "memory", "artifact_id": "demo-memory", "revision": 1},
            },
            "source_refs": [],
        }
        self._entries.append(entry)
        return entry

    def _call(self, op: str, request: dict, detail: str = "") -> None:
        self._calls.insert(
            0,
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "op": op,
                "path": "(demo)",
                "request": request,
                "status": 200,
                "ok": True,
                "duration_ms": 1,
                "detail": detail,
            },
        )
        self._calls = self._calls[:60]

    def ping(self) -> bool:
        return True

    def resolve_scope(self, title: str, summary: str) -> str:
        return self.scope_id

    def remember(self, kind: str, text: str, reason: str = "") -> Dict[str, Any]:
        entry = self._append(kind, text)
        self._call("写入记忆 remember", {"kind": kind, "text": text}, reason)
        return {"memory": {"family": "memory", "artifact_id": "demo-memory", "revision": 1},
                "entry": entry}

    def search(self, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        scored = []
        for entry in self._entries:
            if entry["state"] != "active":
                continue
            score = _keyword_score(query, entry["text"])
            if score <= 0:
                continue
            scored.append(
                {
                    "text": entry["text"],
                    "score": round(min(1.0, score + 0.2), 3),
                    "matched_by": ["fulltext"],
                    "entry_id": entry["entry_id"],
                    "citation": entry["citation"],
                }
            )
        scored.sort(key=lambda item: item["score"], reverse=True)
        hits = scored[:limit]
        self._call("检索记忆 search", {"query": query}, f"命中 {len(hits)} 条")
        return hits

    def list_entries(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        entries = [
            entry for entry in self._entries if include_inactive or entry["state"] == "active"
        ]
        self._call("列出记忆 list", {}, f"{len(entries)} 条")
        return entries

    def retire(self, citation: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
        entry_id = (citation or {}).get("entry_id", "")
        for entry in self._entries:
            if entry["entry_id"] == entry_id:
                entry["state"] = "inactive"
        self._call("停用记忆 retire", {"entry_id": entry_id}, reason)
        return {"retired": True}

    def prepare(self, query: str, max_bytes: int = 4000) -> Dict[str, Any]:
        selected = [entry for entry in self._entries if entry["state"] == "active"][:6]
        lines = ["# Prepared Context（演示）", "", "## memory"]
        for entry in selected:
            lines.append(f"- [{entry['kind']}] {entry['text']}")
        content = "\n".join(lines)
        self._call("准备上下文 prepare", {"query": query}, f"{len(content)} bytes")
        return {"status": "ready", "content": content, "content_bytes": len(content.encode("utf-8"))}

    def capture_source(self, source_id: str, content: str, metadata=None) -> Dict[str, Any]:
        self._call("保存证据 source", {"source_id": source_id}, f"{len(content)} 字")
        return {"source": {"source_id": source_id, "metadata": metadata or {}}}

    def read_calls(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self._calls[:limit]


def _hit(
    chunk: Dict[str, Any],
    *,
    distance: Optional[float] = None,
    vector_rank: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "paper_id": chunk["paper_id"],
        "title": chunk["title"],
        "section": chunk["section"],
        "chunk_index": chunk["chunk_index"],
        "content": chunk["content"],
        "year": chunk["year"],
        "venue": chunk["venue"],
        "tags": chunk["tags"],
        "distance": None if distance is None else round(distance, 4),
        "vector_rank": vector_rank,
        "fts_rank": None,
        "rrf_score": 0.0,
        "matched_by": [],
    }


def _match_filters(chunk: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if isinstance(year_from, int) and (chunk.get("year") or 0) < year_from:
        return False
    if isinstance(year_to, int) and (chunk.get("year") or 9999) > year_to:
        return False
    venue = str(filters.get("venue") or "").strip()
    if venue and venue not in (chunk.get("venue") or ""):
        return False
    tag = str(filters.get("tag") or "").strip()
    if tag and tag not in (chunk.get("tags") or ""):
        return False
    return True
