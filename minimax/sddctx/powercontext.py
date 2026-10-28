"""PowerContext HTTP 客户端（Server 1.2.0，REST /v1）。

任务状态、验证结果、上下文装配都由 PowerContext 负责持久化；seekdb 只是工程资料的
检索库。这样分工的理由：规格/代码/测试是「项目事实」，随仓库演进；任务进展是
「会话事实」，需要跨会话、跨 Agent 复用，正好是 PowerContext 的 Scope/Memory 模型。

所有调用都会追加到 ``.state/powercontext-calls.jsonl``，评审可直接核对
「什么时候写了什么、什么时候又读了回来」。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import PowerContextConfig


class PowerContextError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class PowerContextClient:
    def __init__(self, cfg: PowerContextConfig, state_dir: Path) -> None:
        self.cfg = cfg
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.state_dir / "powercontext-calls.jsonl"
        self._scope_file = self.state_dir / "powercontext-scope.json"
        self._scope_id = cfg.scope_id
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        return headers

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        url = f"{self.cfg.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        request = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.cfg.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            self._log(method, path, payload, exc.code, started, ok=False, detail=raw[:300])
            message = raw
            try:
                error = json.loads(raw).get("error") or {}
                message = error.get("message", raw)
                details = error.get("details") or {}
                if isinstance(details, dict) and details.get("message"):
                    message = f"{message}（{details['message']}）"
            except json.JSONDecodeError:
                pass
            raise PowerContextError(f"PowerContext {exc.code}：{message}", exc.code) from None
        except Exception as exc:  # noqa: BLE001
            self._log(method, path, payload, 0, started, ok=False, detail=str(exc)[:300])
            raise PowerContextError(
                f"无法连接 PowerContext（{self.cfg.base_url}）：{exc}。"
                "请先执行 `powercontext server run`，见 README 第 4 节。"
            ) from exc

        self._log(method, path, payload, status, started, ok=True)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PowerContextError(f"PowerContext 返回非 JSON：{raw[:200]}") from exc

    def _log(
        self,
        method: str,
        path: str,
        payload: dict | None,
        status: int,
        started: float,
        *,
        ok: bool,
        detail: str = "",
    ) -> None:
        summary: dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("scope_id", "kind", "query", "source_id", "reason", "max_bytes"):
                if payload.get(key) is not None:
                    value = str(payload[key])
                    summary[key] = value if len(value) <= 120 else value[:119] + "…"
            if "text" in payload:
                summary["text_bytes"] = len(str(payload["text"]).encode("utf-8"))
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": method,
            "path": path,
            "request": summary,
            "status": status,
            "ok": ok,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "detail": detail,
        }
        with self._lock:
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- Scope
    def ping(self) -> bool:
        try:
            self._request("GET", "/health/live")
            return True
        except PowerContextError:
            return False

    def scope_id(self) -> str:
        if self._scope_id:
            return self._scope_id
        if self._scope_file.is_file():
            try:
                cached = json.loads(self._scope_file.read_text(encoding="utf-8"))
                scope_id = str(cached.get("scope_id", "")).strip()
                if scope_id and self._scope_alive(scope_id):
                    self._scope_id = scope_id
                    return scope_id
            except (json.JSONDecodeError, OSError):
                pass
        payload = {
            "title": self.cfg.scope_title,
            "summary": "SDD 上下文模块：商品目录检索任务的规格、代码、测试与任务进展",
            "idempotency_key": "sddctx-catalog-scope-v1",
        }
        try:
            created = self._request("POST", "/v1/scopes", payload)
            scope_id = str(created.get("scope_id", ""))
        except PowerContextError as exc:
            if exc.status != 409:
                raise
            scope_id = self._find_scope()
        if not scope_id:
            raise PowerContextError("Scope 创建成功但响应缺少 scope_id")
        self._scope_id = scope_id
        self._scope_file.write_text(
            json.dumps(
                {
                    "scope_id": scope_id,
                    "title": self.cfg.scope_title,
                    "base_url": self.cfg.base_url,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return scope_id

    def _scope_alive(self, scope_id: str) -> bool:
        try:
            self._request("GET", f"/v1/scopes/{scope_id}")
            return True
        except PowerContextError as exc:
            return exc.status != 404

    def _find_scope(self) -> str:
        page = self._request("GET", f"/v1/scopes?query={quote(self.cfg.scope_title)}&limit=50")
        for item in page.get("scopes") or page.get("items") or []:
            if item.get("title") == self.cfg.scope_title:
                return str(item.get("scope_id", ""))
        raise PowerContextError(
            "同名 Scope 已存在但无法定位；可设置 POWERCONTEXT_SCOPE_ID、"
            "或删除 .state/powercontext-scope.json 后重试。"
        )

    # ---------------------------------------------------------------- Memory
    def remember(self, kind: str, text: str, reason: str = "") -> dict:
        payload: dict[str, Any] = {"scope_id": self.scope_id(), "kind": kind, "text": text}
        if reason:
            payload["reason"] = reason
        return self._request("POST", "/v1/memory/remember", payload)

    def search(self, query: str, limit: int = 8) -> list[dict]:
        result = self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": self.scope_id(), "query": query, "limit": limit},
        )
        return list(result.get("hits") or [])

    def list_entries(self, include_inactive: bool = False) -> list[dict]:
        result = self._request(
            "POST",
            "/v1/memory/entries/list",
            {"scope_id": self.scope_id(), "include_inactive": include_inactive},
        )
        return list(result.get("entries") or [])

    def prepare(self, query: str, max_bytes: int = 4000) -> dict:
        return self._request(
            "POST",
            "/v1/context/prepare",
            {
                "scope_id": self.scope_id(),
                "query": query,
                "max_bytes": max_bytes,
                "assembly": {
                    "format": "markdown",
                    "sections": [{"family": "memory", "limit": 8}],
                },
            },
        )

    def capture_source(self, source_id: str, content: str, metadata: dict | None = None) -> dict:
        return self._request(
            "POST",
            "/v1/sources/content",
            {
                "scope_id": self.scope_id(),
                "source_id": source_id,
                "content": content,
                "metadata": metadata or {},
            },
        )

    def read_calls(self, limit: int = 40) -> list[dict]:
        if not self._log_path.is_file():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()[-limit * 3 :]
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(records))[:limit]
