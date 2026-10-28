"""规有所依 · SDD 上下文模块。

对外入口是 CLI（``python -m sddctx``）；本包同时暴露可编程接口，
方便被 Agent / MCP Server / 插件直接调用：

    from sddctx import load_config, open_db, build_context

    cfg = load_config()
    context = build_context(
        db=open_db(cfg.seekdb), project=cfg.project,
        task_text="给 /api/products 增加价格区间与库存筛选",
    )
    print(context["markdown"])
"""

from __future__ import annotations

from .assemble import build_context, detect_endpoint, render_markdown
from .config import AppConfig, load_config
from .db import SeekDb, open_db
from .ingest import IngestReport, ingest
from .retrieve import search
from .state import TaskState, TaskStateStore

__all__ = [
    "AppConfig",
    "IngestReport",
    "SeekDb",
    "TaskState",
    "TaskStateStore",
    "build_context",
    "detect_endpoint",
    "ingest",
    "load_config",
    "open_db",
    "render_markdown",
    "search",
]

__version__ = "1.0.0"
