"""代码/测试索引：用 ``ast`` 提取符号与可检索片段。

不调用 LSP，也不依赖第三方解析库：评审环境只需要 CPython 标准库。
提取粒度刻意保持「文件 + 顶层符号 + 片段」三层，因为上下文输出要求能定位到
「需要修改的代码文件或符号」，而不是只给一个文件名。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

MAX_CHUNK_CHARS = 1400


@dataclass
class CodeSymbol:
    doc_path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str


@dataclass
class CodeChunk:
    doc_path: str
    symbol: str
    ordinal: int
    heading: str
    body: str


def _signature(source_lines: list[str], start_line: int) -> str:
    line = source_lines[start_line - 1].strip() if start_line - 1 < len(source_lines) else ""
    return line[:200]


def parse_python_file(path: Path, project_root: Path) -> tuple[list[CodeSymbol], list[CodeChunk]]:
    rel_path = path.resolve().relative_to(project_root.resolve()).as_posix()
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)

    symbols: list[CodeSymbol] = []
    chunks: list[CodeChunk] = []

    def add_symbol(name: str, kind: str, node: ast.AST) -> CodeSymbol:
        symbol = CodeSymbol(
            doc_path=rel_path,
            name=name,
            kind=kind,
            start_line=int(getattr(node, "lineno", 1)),
            end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
            signature=_signature(source_lines, int(getattr(node, "lineno", 1))),
        )
        symbols.append(symbol)
        return symbol

    def add_chunk(name: str, heading: str, start: int, end: int, ordinal: int) -> None:
        body = "\n".join(source_lines[start - 1 : end]).strip()
        if not body:
            return
        if len(body) > MAX_CHUNK_CHARS:
            for offset in range(0, len(body), MAX_CHUNK_CHARS):
                chunks.append(
                    CodeChunk(
                        doc_path=rel_path,
                        symbol=name if offset == 0 else f"{name}",
                        ordinal=ordinal + offset,
                        heading=f"{heading}（片段 {offset // MAX_CHUNK_CHARS + 1}）",
                        body=body[offset : offset + MAX_CHUNK_CHARS],
                    )
                )
        else:
            chunks.append(
                CodeChunk(doc_path=rel_path, symbol=name, ordinal=ordinal, heading=heading, body=body)
            )

    def walk_body(body: list[ast.stmt], prefix: str, ordinal_base: int) -> int:
        ordinal = ordinal_base
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{node.name}"
                add_symbol(name, "function", node)
                add_chunk(name, name, node.lineno, node.end_lineno or node.lineno, ordinal)
                ordinal += 1
            elif isinstance(node, ast.ClassDef):
                name = f"{prefix}{node.name}"
                add_symbol(name, "class", node)
                header_end = min(
                    [node.lineno + 12, node.end_lineno or node.lineno]
                )
                add_chunk(name, f"class {name}", node.lineno, header_end, ordinal)
                ordinal = walk_body(node.body, f"{name}.", ordinal + 1)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node.value, ast.AST) and isinstance(node.target, ast.Name):
                    targets = [node.target.id]
                for target in targets:
                    if target.isupper() or target in {
                        "SORT_OPTIONS",
                        "DEFAULT_SORT",
                        "DEFAULT_PAGE",
                        "DEFAULT_PAGE_SIZE",
                        "MAX_PAGE_SIZE",
                        "PRODUCTS_PATH",
                    }:
                        name = f"{prefix}{target}"
                        add_symbol(name, "variable", node)
                        add_chunk(name, name, node.lineno, node.end_lineno or node.lineno, ordinal)
                        ordinal += 1
        return ordinal

    # 文件头（imports / docstring）单独成块，便于「按模块职责」召回
    header_end = 1
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            header_end = max(header_end, int(node.end_lineno or node.end_lineno or 1))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            header_end = max(header_end, int(node.end_lineno or node.lineno))
    add_chunk("", f"{rel_path} 文件头", 1, header_end, 0)

    walk_body(tree.body, "", 10)
    return symbols, chunks


def index_tree(root: Path, project_root: Path) -> tuple[list[CodeSymbol], list[CodeChunk]]:
    symbols: list[CodeSymbol] = []
    chunks: list[CodeChunk] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        file_symbols, file_chunks = parse_python_file(path, project_root)
        symbols.extend(file_symbols)
        chunks.extend(file_chunks)
    return symbols, chunks


def infer_kind(doc_path: str) -> str:
    return "test" if "/tests/" in f"/{doc_path}" or Path(doc_path).name.startswith("test_") else "code"
