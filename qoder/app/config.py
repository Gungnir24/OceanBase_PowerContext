"""研栖 ResearchNest —— 配置加载。

所有配置都来自环境变量；项目根目录下的 .env 会被读取，但不会覆盖已经存在的
环境变量。这样 `scripts/run.ps1`、Docker 与手工启动可以共用同一份配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = PROJECT_ROOT / "data"
STATE_DIR = PROJECT_ROOT / ".state"


def _read_dotenv(path: Path) -> dict[str, str]:
    """读取最简单的 KEY=VALUE 形式 .env，忽略注释与空行。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    demo: bool = False
    state_dir: Path = STATE_DIR

    # seekdb（MySQL 协议）
    seekdb_host: str = "127.0.0.1"
    seekdb_port: int = 2881
    seekdb_user: str = "root"
    seekdb_password: str = ""
    seekdb_database: str = "test"
    seekdb_charset: str = "utf8mb4"
    fts_parser: str = "ik"
    vector_dim: int = 384
    top_k_vector: int = 12
    top_k_fts: int = 12
    rrf_k: int = 60

    # PowerContext
    powercontext_url: str = "http://127.0.0.1:8000"
    powercontext_token: str = ""
    powercontext_scope_id: str = ""
    powercontext_scope_title: str = "研栖 · 科研工作台"
    powercontext_scope_summary: str = (
        "个人科研文献阅读、检索问答与组会周报的工作台 scope。"
    )

    # Embedding
    embedding_provider: str = "local"  # local | openai
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # LLM（可选；缺省时使用可复现的抽取式回答）
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout: int = 60

    extra: dict[str, str] = field(default_factory=dict)

    @property
    def use_llm(self) -> bool:
        return bool(self.llm_api_key)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        env_file = env_file or PROJECT_ROOT / ".env"
        for key, value in _read_dotenv(env_file).items():
            os.environ.setdefault(key, value)

        cfg = cls(
            host=_env("APP_HOST", "127.0.0.1"),
            port=_env_int("APP_PORT", 8787),
            demo=_env_bool("APP_DEMO", False),
            state_dir=Path(_env("APP_STATE_DIR", str(STATE_DIR))).expanduser(),
            seekdb_host=_env("SEEKDB_HOST", "127.0.0.1"),
            seekdb_port=_env_int("SEEKDB_PORT", 2881),
            seekdb_user=_env("SEEKDB_USER", "root"),
            seekdb_password=_env("SEEKDB_PASSWORD", ""),
            seekdb_database=_env("SEEKDB_DATABASE", "test"),
            fts_parser=_env("SEEKDB_FTS_PARSER", "ik"),
            vector_dim=_env_int("EMBEDDING_DIM", 384),
            top_k_vector=_env_int("RETRIEVAL_TOP_K_VECTOR", 12),
            top_k_fts=_env_int("RETRIEVAL_TOP_K_FTS", 12),
            rrf_k=_env_int("RETRIEVAL_RRF_K", 60),
            powercontext_url=_env("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/"),
            powercontext_token=_env("POWERCONTEXT_TOKEN", ""),
            powercontext_scope_id=_env("POWERCONTEXT_SCOPE_ID", ""),
            powercontext_scope_title=_env(
                "POWERCONTEXT_SCOPE_TITLE", "研栖 · 科研工作台"
            ),
            embedding_provider=_env("EMBEDDING_PROVIDER", "local").lower(),
            embedding_base_url=_env(
                "EMBEDDING_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/"),
            embedding_api_key=_env("EMBEDDING_API_KEY", ""),
            embedding_model=_env("EMBEDDING_MODEL", "text-embedding-3-small"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_api_key=_env("LLM_API_KEY", ""),
            llm_model=_env("LLM_MODEL", "gpt-4o-mini"),
            llm_timeout=_env_int("LLM_TIMEOUT", 60),
        )
        if cfg.vector_dim < 8:
            raise ValueError("EMBEDDING_DIM 至少为 8")
        if cfg.vector_dim > 4096:
            raise ValueError("EMBEDDING_DIM 过大（上限 4096）")
        return cfg
