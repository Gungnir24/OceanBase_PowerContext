"""运行配置：全部来自环境变量 / .env，带可离线复现的默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取：不覆盖已存在的环境变量。"""
    env_file = path or PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # 配置写错时立刻暴露，而不是静默降级
        raise ValueError(f"环境变量 {name} 需要整数，收到 {raw!r}") from exc


@dataclass(frozen=True)
class SeekDbConfig:
    host: str = "127.0.0.1"
    port: int = 2881
    user: str = "root"
    password: str = ""
    database: str = "sddctx"

    @classmethod
    def from_env(cls) -> "SeekDbConfig":
        return cls(
            host=_env("SEEKDB_HOST", "127.0.0.1"),
            port=_env_int("SEEKDB_PORT", 2881),
            user=_env("SEEKDB_USER", "root"),
            password=_env("SEEKDB_PASSWORD", ""),
            database=_env("SEEKDB_DATABASE", "sddctx"),
        )


@dataclass(frozen=True)
class PowerContextConfig:
    base_url: str = "http://127.0.0.1:8000"
    token: str = ""
    scope_id: str = ""
    scope_title: str = "SDD 上下文 · 商品目录检索"
    timeout: int = 20

    @classmethod
    def from_env(cls) -> "PowerContextConfig":
        return cls(
            base_url=_env("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/"),
            token=_env("POWERCONTEXT_TOKEN", ""),
            scope_id=_env("POWERCONTEXT_SCOPE_ID", ""),
            scope_title=_env("POWERCONTEXT_SCOPE_TITLE", "SDD 上下文 · 商品目录检索"),
            timeout=_env_int("POWERCONTEXT_TIMEOUT", 20),
        )


@dataclass(frozen=True)
class MinimaxConfig:
    """MiniMax 通过 OpenAI 兼容网关调用，模型名即 MiniMax 官方模型标识。"""

    base_url: str = "https://api.ppio.com/openai/v1"
    api_key: str = ""
    model: str = "minimax/minimax-m2.5"
    temperature: float = 0.2
    max_tokens: int = 8000
    timeout: int = 300

    @classmethod
    def from_env(cls) -> "MinimaxConfig":
        return cls(
            base_url=_env("MINIMAX_BASE_URL", "https://api.ppio.com/openai/v1").rstrip("/"),
            api_key=_env("MINIMAX_API_KEY", _env("PPIO_API_KEY", "")),
            model=_env("MINIMAX_MODEL", "minimax/minimax-m2.5"),
            temperature=float(_env("MINIMAX_TEMPERATURE", "0.2")),
            max_tokens=_env_int("MINIMAX_MAX_TOKENS", 8000),
            timeout=_env_int("MINIMAX_TIMEOUT", 300),
        )


@dataclass(frozen=True)
class AppConfig:
    seekdb: SeekDbConfig = field(default_factory=SeekDbConfig.from_env)
    powercontext: PowerContextConfig = field(default_factory=PowerContextConfig.from_env)
    minimax: MinimaxConfig = field(default_factory=MinimaxConfig.from_env)
    project: str = "catalog"
    state_dir: Path = PROJECT_ROOT / ".state"
    log_dir: Path = PROJECT_ROOT / "docs" / "logs"
    specs_dir: Path = PROJECT_ROOT / "data" / "specs"
    code_dir: Path = PROJECT_ROOT / "workdir" / "biz"
    tests_dir: Path = PROJECT_ROOT / "workdir" / "tests"


def load_config() -> AppConfig:
    load_dotenv()
    return AppConfig(
        seekdb=SeekDbConfig.from_env(),
        powercontext=PowerContextConfig.from_env(),
        minimax=MinimaxConfig.from_env(),
        project=os.environ.get("SDDCTX_PROJECT", "catalog"),
        state_dir=Path(os.environ.get("SDDCTX_STATE_DIR", str(PROJECT_ROOT / ".state"))),
        log_dir=Path(os.environ.get("SDDCTX_LOG_DIR", str(PROJECT_ROOT / "docs" / "logs"))),
        specs_dir=Path(os.environ.get("SDDCTX_SPECS_DIR", str(PROJECT_ROOT / "data" / "specs"))),
        code_dir=Path(os.environ.get("SDDCTX_CODE_DIR", str(PROJECT_ROOT / "workdir" / "biz"))),
        tests_dir=Path(os.environ.get("SDDCTX_TESTS_DIR", str(PROJECT_ROOT / "workdir" / "tests"))),
    )
