"""上下文模块自身的测试夹具。

集成测试需要 seekdb（2881）与 PowerContext（8000）同时在跑；
没有服务时自动跳过，而不是报环境错误——避免把「环境没起来」当成「功能失败」。
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import pytest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sddctx.config import load_config  # noqa: E402
from sddctx.db import open_db  # noqa: E402
from sddctx.ingest import ingest  # noqa: E402


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def services(cfg):
    host = cfg.seekdb.host
    parsed = urlparse(cfg.powercontext.base_url)
    if not _port_open(host, cfg.seekdb.port):
        pytest.skip(f"seekdb {host}:{cfg.seekdb.port} 未启动")
    if not _port_open(parsed.hostname or "127.0.0.1", parsed.port or 80):
        pytest.skip(f"PowerContext {cfg.powercontext.base_url} 未启动")
    return True


@pytest.fixture(scope="session")
def seeded_db(services, cfg):
    """确保库里至少有一份完整索引（不会清空已有数据）。"""
    db = open_db(cfg.seekdb)
    if db.stats()["clauses"] == 0:
        ingest(cfg, db)
    return db
