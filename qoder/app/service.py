"""业务编排：把 seekdb 的文献检索与 PowerContext 的记忆串成闭环。"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import DATA_DIR
from .embeddings import get_embedder
from .generation import Generator
from .ingest import chunk_text, make_chunk_id, make_paper_id, parse_document
from .powercontext import PowerContextClient, PowerContextError
from .seekdb_store import SeekDBError, SeekDBStore

# 一次提问的措辞可能和任何一条记忆都没有字面交集（中文短问句尤其明显）。
# 这时退回按常驻主题召回，保证「研究方向 / 回答偏好 / 实验约束」这类长期
# 上下文不会因为问法变化而丢失。
STANDING_MEMORY_QUERY = "研究方向 回答偏好 实验约束 本周进展"


class AppService:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        if cfg.demo:
            from .demo import MemoryPowerContext, MemorySeekStore

            self.store: Any = MemorySeekStore()
            self.pc: Any = MemoryPowerContext()
            self.scope_id = "demo-scope"
        else:
            self.store = SeekDBStore(cfg)
            self.pc = PowerContextClient(
                cfg.powercontext_url,
                cfg.powercontext_token,
                cfg.powercontext_scope_id,
                state_dir=cfg.state_dir,
            )
            self.scope_id = cfg.powercontext_scope_id or "local"
        self.embedder = get_embedder(cfg)
        self.generator = Generator(cfg)
        self.ready = False
        self.seekdb_ok = False
        self.powercontext_ok = False
        self.startup_error = ""
        self.warnings: List[str] = []

    # ------------------------------------------------------------------ 启动
    def initialize(self) -> None:
        try:
            self.store.ensure_schema()
            self.seekdb_ok = True
        except SeekDBError as exc:
            self.startup_error = str(exc)
            self.warnings.append(self.startup_error)
        except Exception as exc:  # noqa: BLE001
            self.startup_error = f"seekdb 初始化失败：{exc}"
            self.warnings.append(self.startup_error)

        try:
            if self.pc.ping():
                self.powercontext_ok = True
                self.scope_id = self.pc.resolve_scope(
                    self.cfg.powercontext_scope_title,
                    self.cfg.powercontext_scope_summary,
                )
        except PowerContextError as exc:
            self.warnings.append(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.warnings.append(f"PowerContext 不可用：{exc}")

        self.ready = self.seekdb_ok

    def health(self) -> Dict[str, Any]:
        seekdb: Dict[str, Any] = {"ok": self.seekdb_ok}
        if self.seekdb_ok:
            try:
                seekdb.update(self.store.health())
            except Exception as exc:  # noqa: BLE001
                seekdb = {"ok": False, "error": str(exc)[:300]}
        return {
            "app": "研栖 ResearchNest",
            "ready": self.ready,
            "demo": self.cfg.demo,
            "scope_id": self.scope_id,
            "seekdb": seekdb,
            "powercontext": {
                "ok": self.powercontext_ok,
                "url": self.cfg.powercontext_url,
                "scope_id": self.scope_id if self.powercontext_ok else "",
            },
            "embedding": {
                "provider": self.embedder.name,
                "dim": self.cfg.vector_dim,
            },
            "llm": {
                "enabled": self.cfg.use_llm,
                "model": self.cfg.llm_model if self.cfg.use_llm else "",
            },
            "warnings": self.warnings[-6:],
        }

    # ------------------------------------------------------------------ 文献
    def ingest(self, name: str, text: str) -> Dict[str, Any]:
        self._require_seekdb()
        document = parse_document(name, text)
        chunks = chunk_text(document.body)
        if not chunks:
            raise ValueError("文档内容为空，无法切片")
        paper_id = make_paper_id(name, document.title)
        rows = []
        vectors = self.embedder.embed_many([chunk.content for chunk in chunks])
        for chunk, vector in zip(chunks, vectors):
            rows.append(
                (
                    make_chunk_id(paper_id, chunk.index),
                    chunk.index,
                    chunk.section,
                    chunk.content,
                    vector,
                )
            )
        count = self.store.upsert_paper(self.scope_id, paper_id, document, rows)
        self.store.log_activity(
            self.scope_id,
            "ingest",
            paper_id,
            f"导入《{document.title}》",
            f"{count} 个切片",
        )
        return {
            "paper_id": paper_id,
            "title": document.title,
            "chunks": count,
            "year": document.year,
            "venue": document.venue,
            "tags": document.tags,
        }

    def import_samples(self) -> List[Dict[str, Any]]:
        results = []
        samples = sorted((DATA_DIR / "samples").rglob("*.md"))
        for path in samples:
            text = path.read_text(encoding="utf-8")
            results.append(self.ingest(path.name, text))
        return results

    def list_papers(self) -> List[Dict[str, Any]]:
        self._require_seekdb()
        return self.store.list_papers(self.scope_id)

    def get_paper(self, paper_id: str) -> Optional[Dict[str, Any]]:
        self._require_seekdb()
        return self.store.get_paper(self.scope_id, paper_id)

    def delete_paper(self, paper_id: str) -> bool:
        self._require_seekdb()
        deleted = self.store.delete_paper(self.scope_id, paper_id)
        if deleted:
            self.store.log_activity(self.scope_id, "delete", paper_id, "删除文献")
        return deleted

    # ------------------------------------------------------------------ 问答
    def ask(
        self, question: str, filters: Optional[Dict[str, Any]] = None, top_k: int = 6
    ) -> Dict[str, Any]:
        self._require_seekdb()
        question = (question or "").strip()
        if not question:
            raise ValueError("问题不能为空")
        filters = filters or {}
        started = time.perf_counter()

        embedding = self.embedder.embed(question)
        search = self.store.search(
            self.scope_id, question, embedding, filters=filters, top_k=top_k
        )

        memories: List[Dict[str, Any]] = []
        prepared: Dict[str, Any] = {}
        memory_warning = ""
        memory_source = "none"
        prepared_source = "none"
        if self.powercontext_ok:
            try:
                memories = self.pc.search(question, limit=6)
                memory_source = "query" if memories else "none"
                if not memories:
                    memories = self.pc.search(STANDING_MEMORY_QUERY, limit=6)
                    memory_source = "standing" if memories else "none"
                prepared = self.pc.prepare(question, max_bytes=4000)
                prepared_source = (
                    "query" if (prepared.get("content") or "").strip() else "none"
                )
                if prepared_source == "none":
                    prepared = self.pc.prepare(STANDING_MEMORY_QUERY, max_bytes=4000)
                    prepared_source = (
                        "standing"
                        if (prepared.get("content") or "").strip()
                        else "none"
                    )
            except PowerContextError as exc:
                memory_warning = f"记忆读取失败：{exc}"
        else:
            memory_warning = "PowerContext 未连接，本次回答不含个人记忆。"

        result = self.generator.answer(
            question,
            search["hits"],
            memories,
            prepared,
            retrieval_meta={
                "vector_candidates": search["vector_candidates"],
                "fts_candidates": search["fts_candidates"],
                "latency_ms": search["latency_ms"],
                "warning": search["warning"],
            },
        )
        result["question"] = question
        result["filters"] = filters
        result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        result["prepared_context"] = prepared.get("content") or ""
        result["memory_warning"] = memory_warning
        result["memory_source"] = memory_source
        result["prepared_source"] = prepared_source
        result["search"] = {
            "query": search["query"],
            "hits": search["hits"],
            "vector_candidates": search["vector_candidates"],
            "fts_candidates": search["fts_candidates"],
            "latency_ms": search["latency_ms"],
            "warning": search["warning"],
        }
        self.store.log_activity(
            self.scope_id, "ask", "", question[:120], result.get("answer", "")[:500]
        )
        return result

    def search_only(
        self, query: str, filters: Optional[Dict[str, Any]] = None, top_k: int = 8
    ) -> Dict[str, Any]:
        self._require_seekdb()
        embedding = self.embedder.embed(query)
        result = self.store.search(
            self.scope_id, query, embedding, filters=filters or {}, top_k=top_k
        )
        return result

    # ------------------------------------------------------------------ 记忆
    def memories(self) -> List[Dict[str, Any]]:
        if not self.powercontext_ok:
            raise PowerContextError(
                "PowerContext 未连接。请先启动 powercontext server 并刷新页面。"
            )
        return self.pc.list_entries()

    def remember(self, text: str, kind: str = "fact", reason: str = "") -> Dict[str, Any]:
        if not self.powercontext_ok:
            raise PowerContextError("PowerContext 未连接，无法保存记忆。")
        text = (text or "").strip()
        if not text:
            raise ValueError("记忆内容不能为空")
        result = self.pc.remember(kind=kind or "fact", text=text, reason=reason)
        self.store.log_activity(self.scope_id, "remember", "", f"写入记忆 · {kind}", text)
        entry = result.get("entry") or {}
        return {
            "saved": True,
            "entry_id": (entry.get("citation") or {}).get("entry_id", ""),
            "kind": kind,
            "text": text,
        }

    def retire_memory(self, citation: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
        if not self.powercontext_ok:
            raise PowerContextError("PowerContext 未连接，无法停用记忆。")
        result = self.pc.retire(citation, reason=reason)
        self.store.log_activity(self.scope_id, "retire", "", "停用记忆", reason)
        return {"retired": True, "result": result}

    # ------------------------------------------------------------------ 周报
    def weekly(self, days: int = 7, focus: str = "") -> Dict[str, Any]:
        self._require_seekdb()
        days = max(1, min(int(days or 7), 60))
        activity = self.store.weekly_activity(self.scope_id, days=days)

        queries: List[str] = []
        for item in activity:
            if item["kind"] == "ask" and item.get("title"):
                queries.append(item["title"])
            if len(queries) >= 4:
                break
        if focus:
            queries.insert(0, focus)
        if not queries:
            queries = ["本周读过的文献里最重要的结论与可复现实验设置"]

        hits: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for query in queries:
            embedding = self.embedder.embed(query)
            search = self.store.search(
                self.scope_id, query, embedding, filters={}, top_k=3
            )
            for hit in search["hits"]:
                if hit["chunk_id"] in seen:
                    continue
                hit["query"] = query
                hits.append(hit)
                seen.add(hit["chunk_id"])
            if len(hits) >= 8:
                break

        memories: List[Dict[str, Any]] = []
        prepared: Dict[str, Any] = {}
        memory_warning = ""
        if self.powercontext_ok:
            try:
                memories = self.pc.search("本周进展 阻塞 下一步 计划", limit=8)
                if not memories:
                    memories = self.pc.search(STANDING_MEMORY_QUERY, limit=8)
                prepared = self.pc.prepare("本周科研进展与周报", max_bytes=4000)
                if not (prepared.get("content") or "").strip():
                    prepared = self.pc.prepare(STANDING_MEMORY_QUERY, max_bytes=4000)
            except PowerContextError as exc:
                memory_warning = str(exc)
        else:
            memory_warning = "PowerContext 未连接，周报不含研究记忆。"

        stats = self.store.stats(self.scope_id)
        result = self.generator.weekly_report(
            days=days,
            activity=activity,
            hits=hits[:6],
            memories=memories,
            stats=stats,
            prepared=prepared,
        )
        result.update(
            {
                "days": days,
                "activity": activity[:30],
                "citations": [
                    {
                        "index": position + 1,
                        "chunk_id": hit["chunk_id"],
                        "title": hit["title"],
                        "section": hit.get("section", ""),
                        "chunk_index": hit.get("chunk_index", 0),
                    }
                    for position, hit in enumerate(hits[:6])
                ],
                "memory_warning": memory_warning,
                "stats": stats,
            }
        )
        return result

    def archive_weekly(self, markdown: str, summary: str = "") -> Dict[str, Any]:
        """把周报正文存为 Source，并把一句摘要写入记忆。"""
        if not self.powercontext_ok:
            raise PowerContextError("PowerContext 未连接，无法归档周报。")
        markdown = (markdown or "").strip()
        if not markdown:
            raise ValueError("周报内容为空")
        source_id = f"weekly-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        source = self.pc.capture_source(
            source_id,
            markdown,
            metadata={"kind": "weekly-report", "app": "researchnest"},
        )
        summary = (summary or markdown.split("\n## ")[0]).strip()
        memory = self.pc.remember(
            kind="status",
            text=f"本周进展摘要：{summary[:600]}",
            reason="周报归档后自动沉淀，便于下次组会复用",
        )
        self.store.log_activity(self.scope_id, "archive", source_id, "归档周报", summary[:200])
        return {"source": source, "memory": memory, "source_id": source_id}

    # ------------------------------------------------------------------ 核验
    def logs(self, limit: int = 20) -> Dict[str, Any]:
        self._require_seekdb()
        return {
            "retrieval": self.store.recent_logs(self.scope_id, limit=limit),
            "powercontext": self.pc.read_calls(limit=limit),
        }

    def stats(self) -> Dict[str, Any]:
        self._require_seekdb()
        return self.store.stats(self.scope_id)

    # ------------------------------------------------------------------ 工具
    def _require_seekdb(self) -> None:
        if not self.seekdb_ok:
            raise SeekDBError(
                self.startup_error
                or "seekdb 未连接：请先启动 seekdb 并检查 .env 中的 SEEKDB_* 配置。"
            )
