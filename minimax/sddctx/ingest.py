"""入库：把规格、代码、测试写进 seekdb（资料 + 条目 + 片段 + 符号）。

设计要点：
* 以「文档摘要」做增量判断，内容没变就不重建片段，重复执行是幂等的；
* 每个规格条目单独成行（``clauses``），因为上下文输出要求精确到条目而不是文件；
* 代码用 AST 切成「文件 + 符号」两级，输出能定位到 ``path#symbol``；
* 文档不存在时会清理其残留行，保证「规格删除/重命名后不会幽灵命中」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .codeindex import index_tree, infer_kind
from .config import PROJECT_ROOT, AppConfig
from .db import SeekDb
from .embeddings import embed, to_literal
from .specs import load_specs


@dataclass
class IngestReport:
    project: str
    documents: int = 0
    clauses: int = 0
    chunks: int = 0
    symbols: int = 0
    skipped: int = 0
    removed: int = 0
    elapsed_ms: int = 0
    spec_versions: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "documents": self.documents,
            "clauses": self.clauses,
            "chunks": self.chunks,
            "symbols": self.symbols,
            "skipped_unchanged": self.skipped,
            "removed_stale": self.removed,
            "elapsed_ms": self.elapsed_ms,
            "spec_versions": self.spec_versions,
        }


def _upsert_document(
    db: SeekDb,
    *,
    project: str,
    kind: str,
    path: str,
    title: str,
    version: str,
    status: str,
    digest: str,
    size_bytes: int,
) -> None:
    db.execute(
        "INSERT INTO documents (project, kind, path, title, version, status, digest, size_bytes)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " ON DUPLICATE KEY UPDATE title=VALUES(title), kind=VALUES(kind), version=VALUES(version),"
        " status=VALUES(status), digest=VALUES(digest), size_bytes=VALUES(size_bytes)",
        (project, kind, path, title, version, status, digest, size_bytes),
    )


def _doc_digest(db: SeekDb, project: str, path: str) -> str:
    rows = db.query(
        "SELECT digest FROM documents WHERE project=%s AND path=%s", (project, path)
    )
    return rows[0]["digest"] if rows else ""


def _clear_doc_children(db: SeekDb, project: str, path: str) -> None:
    db.execute("DELETE FROM chunks WHERE project=%s AND doc_path=%s", (project, path))
    db.execute("DELETE FROM clauses WHERE project=%s AND doc_path=%s", (project, path))
    db.execute("DELETE FROM symbols WHERE project=%s AND doc_path=%s", (project, path))


def _insert_chunk(
    db: SeekDb,
    *,
    project: str,
    doc_path: str,
    kind: str,
    clause_id: str,
    symbol: str,
    ordinal: int,
    heading: str,
    body: str,
) -> None:
    db.execute(
        "INSERT INTO chunks (project, doc_path, kind, clause_id, symbol, ordinal, heading, body, embedding)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            project,
            doc_path,
            kind,
            clause_id,
            symbol,
            ordinal,
            heading[:255],
            body,
            to_literal(embed(body)),
        ),
    )


def ingest(
    cfg: AppConfig,
    db: SeekDb,
    *,
    project: str | None = None,
    specs_dir: Path | None = None,
    code_dir: Path | None = None,
    tests_dir: Path | None = None,
    reset: bool = False,
    force: bool = False,
    project_root: Path | None = None,
) -> IngestReport:
    started = time.perf_counter()
    project = project or cfg.project
    specs_dir = specs_dir or cfg.specs_dir
    code_dir = code_dir or cfg.code_dir
    tests_dir = tests_dir or cfg.tests_dir
    root = project_root or PROJECT_ROOT

    if reset:
        db.reset()

    report = IngestReport(project=project)
    seen: set[str] = set()

    for doc in load_specs(specs_dir, root):
        seen.add(doc.path)
        existed = _doc_digest(db, project, doc.path)
        _upsert_document(
            db,
            project=project,
            kind="spec",
            path=doc.path,
            title=doc.title,
            version=doc.version,
            status=doc.status,
            digest=doc.digest,
            size_bytes=len(doc.text.encode("utf-8")),
        )
        report.spec_versions[doc.doc_id] = doc.version
        if existed == doc.digest and not force:
            report.skipped += 1
            continue
        _clear_doc_children(db, project, doc.path)
        for ordinal, clause in enumerate(doc.clauses):
            db.execute(
                "INSERT INTO clauses (project, doc_path, clause_id, title, status, applies_to, body,"
                " code_refs, test_refs, replaced_by, version, digest)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project,
                    doc.path,
                    clause.clause_id,
                    clause.title,
                    clause.status,
                    clause.applies_to,
                    clause.body,
                    ",".join(clause.code_refs),
                    ",".join(clause.test_refs),
                    clause.replaced_by,
                    clause.version,
                    clause.digest,
                ),
            )
            report.clauses += 1
            _insert_chunk(
                db,
                project=project,
                doc_path=doc.path,
                kind="spec",
                clause_id=clause.clause_id,
                symbol="",
                ordinal=ordinal,
                heading=f"{clause.clause_id} {clause.title}",
                body=clause.text_for_index,
            )
            report.chunks += 1
        report.documents += 1

    for index_root in (code_dir, tests_dir):
        if not Path(index_root).is_dir():
            continue
        symbols, chunks = index_tree(Path(index_root), root)
        by_doc: dict[str, list] = {}
        for chunk in chunks:
            by_doc.setdefault(chunk.doc_path, []).append(chunk)
        for doc_path, doc_chunks in by_doc.items():
            seen.add(doc_path)
            kind = infer_kind(doc_path)
            absolute = root / doc_path
            source = absolute.read_text(encoding="utf-8")
            digest = __import__("hashlib").sha256(source.encode("utf-8")).hexdigest()
            existed = _doc_digest(db, project, doc_path)
            _upsert_document(
                db,
                project=project,
                kind=kind,
                path=doc_path,
                title=Path(doc_path).name,
                version="",
                status="active",
                digest=digest,
                size_bytes=len(source.encode("utf-8")),
            )
            if existed == digest and not force:
                report.skipped += 1
                continue
            _clear_doc_children(db, project, doc_path)
            for ordinal, chunk in enumerate(doc_chunks):
                _insert_chunk(
                    db,
                    project=project,
                    doc_path=doc_path,
                    kind=kind,
                    clause_id="",
                    symbol=chunk.symbol,
                    ordinal=ordinal,
                    heading=chunk.heading,
                    body=chunk.body,
                )
                report.chunks += 1
            for symbol in [s for s in symbols if s.doc_path == doc_path]:
                db.execute(
                    "INSERT INTO symbols (project, doc_path, name, kind, start_line, end_line, signature)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    " ON DUPLICATE KEY UPDATE kind=VALUES(kind), start_line=VALUES(start_line),"
                    " end_line=VALUES(end_line), signature=VALUES(signature)",
                    (
                        project,
                        symbol.doc_path,
                        symbol.name,
                        symbol.kind,
                        symbol.start_line,
                        symbol.end_line,
                        symbol.signature[:500],
                    ),
                )
                report.symbols += 1
            report.documents += 1

    stale = [
        row["path"]
        for row in db.query("SELECT path FROM documents WHERE project=%s", (project,))
        if row["path"] not in seen
    ]
    for path in stale:
        _clear_doc_children(db, project, path)
        db.execute("DELETE FROM documents WHERE project=%s AND path=%s", (project, path))
        report.removed += 1

    report.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return report
