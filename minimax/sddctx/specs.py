"""规格文档解析：Markdown + 条目化约定 -> 结构化 spec clause。

约定（见 data/specs/ 下的实际文件）：

* 文件头是 YAML 风格的 front matter，取 ``doc_id / title / version / status``；
* 每个条目以 ``### <ID> <标题>`` 开头；
* 条目内的字段行是 ``- 键: 值``（也接受中文冒号）；
* ``正文:`` 后面缩进的行是该条目的要求正文；
* 其余散落文本归入文档级说明，不进入条目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .textutil import digest, iter_refs

STATUS_MAP = {
    "生效": "active",
    "已生效": "active",
    "active": "active",
    "草案": "draft",
    "draft": "draft",
    "已取代": "superseded",
    "superseded": "superseded",
    "已废弃": "deprecated",
    "deprecated": "deprecated",
}

CLAUSE_RE = re.compile(r"^###\s+([A-Za-z][A-Za-z0-9\-]*)\s+(.+?)\s*$")
FIELD_RE = re.compile(r"^-\s*([^:：]+)[:：]\s*(.*)$")


@dataclass
class Clause:
    doc_path: str
    doc_id: str
    doc_title: str
    version: str
    clause_id: str
    title: str
    status: str
    applies_to: str
    code_refs: list[str] = field(default_factory=list)
    test_refs: list[str] = field(default_factory=list)
    replaced_by: str = ""
    body: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def text_for_index(self) -> str:
        """进入全文索引与向量的文本。"""
        parts = [self.clause_id, self.title, self.body]
        if self.applies_to:
            parts.append(self.applies_to)
        parts.extend(self.code_refs)
        parts.extend(self.test_refs)
        return "\n".join(parts)

    @property
    def digest(self) -> str:
        return digest(
            "\n".join(
                [
                    self.doc_path,
                    self.version,
                    self.clause_id,
                    self.status,
                    self.applies_to,
                    self.body,
                    ",".join(self.code_refs),
                    ",".join(self.test_refs),
                    self.replaced_by,
                ]
            )
        )


@dataclass
class SpecDocument:
    path: str
    meta: dict[str, str]
    clauses: list[Clause]
    text: str

    @property
    def doc_id(self) -> str:
        return self.meta.get("doc_id", Path(self.path).stem)

    @property
    def title(self) -> str:
        return self.meta.get("title", Path(self.path).stem)

    @property
    def version(self) -> str:
        return self.meta.get("version", "")

    @property
    def status(self) -> str:
        return STATUS_MAP.get(self.meta.get("status", "生效").strip(), "active")

    @property
    def digest(self) -> str:
        return digest(self.text)


def _parse_front_matter(text: str) -> tuple[dict[str, str], int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta: dict[str, str] = {}
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return meta, index + 1
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, 0


def parse_spec_file(path: Path, project_root: Path) -> SpecDocument:
    text = path.read_text(encoding="utf-8")
    meta, start = _parse_front_matter(text)
    rel_path = path.resolve().relative_to(project_root.resolve()).as_posix()

    clauses: list[Clause] = []
    current_id: str | None = None
    current_title = ""
    fields: dict[str, str] = {}
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_title, fields, body_lines
        if current_id is not None:
            status = STATUS_MAP.get(fields.get("状态", "生效").strip(), "active")
            clause = Clause(
                doc_path=rel_path,
                doc_id=meta.get("doc_id", path.stem),
                doc_title=meta.get("title", path.stem),
                version=meta.get("version", ""),
                clause_id=current_id,
                title=current_title,
                status=status,
                applies_to=fields.get("适用", "").strip(),
                code_refs=list(iter_refs(fields.get("关联代码", ""))),
                test_refs=list(iter_refs(fields.get("关联测试", ""))),
                replaced_by=fields.get("取代", "").strip(),
                body="\n".join(body_lines).strip(),
            )
            clauses.append(clause)
        current_id = None
        current_title = ""
        fields = {}
        body_lines = []

    for line in text.splitlines()[start:]:
        clause_match = CLAUSE_RE.match(line)
        if clause_match:
            flush()
            current_id = clause_match.group(1)
            current_title = clause_match.group(2).strip()
            continue
        if current_id is None:
            continue
        field_match = FIELD_RE.match(line)
        if field_match:
            key = field_match.group(1).strip()
            value = field_match.group(2).strip()
            fields[key] = value
            continue
        if line.strip():
            # 缩进正文行；去掉统一的两格缩进与列表符号
            body_lines.append(line.strip().removeprefix("- ").strip())
    flush()

    return SpecDocument(path=rel_path, meta=meta, clauses=clauses, text=text)


def load_specs(specs_dir: Path, project_root: Path) -> list[SpecDocument]:
    docs = []
    for path in sorted(specs_dir.rglob("*.md")):
        docs.append(parse_spec_file(path, project_root))
    return docs


def superseded_map(clauses: list[Clause]) -> dict[str, Clause]:
    """clause_id -> 取代它的新条目。"""
    mapping: dict[str, Clause] = {}
    for clause in clauses:
        if clause.replaced_by:
            mapping[clause.clause_id] = clause
    return mapping
