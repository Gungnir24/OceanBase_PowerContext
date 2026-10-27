"""PowerContext HTTP 客户端与记忆读写记录。

项目通过 PowerContext Server 的 HTTP API 保存与复用科研记忆：
* Memory：研究方向、表达偏好、实验约束、本周进展等长期事实；
* PreparedContext：在回答与周报生成前，取回有字节上限的历史上下文；
* Source：把周报正文作为可追溯证据保存。

每一次调用都会追加到 .state/powercontext-calls.jsonl，页面“技术核验”直接读取
这份文件，评审可以看到记忆什么时候被写入、什么时候被读取、读到了什么。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import STATE_DIR


class PowerContextError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class PowerContextClient:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        scope_id: str = "",
        state_dir: Path = STATE_DIR,
        timeout: int = 20,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.scope_id = scope_id
        self.state_dir = state_dir
        self.timeout = timeout
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = self.state_dir / "powercontext-calls.jsonl"
        self._state_path = self.state_dir / "powercontext-scope.json"
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- HTTP
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
            self._record("http", path, payload, status, started, ok=False,
                         detail=_error_detail(raw))
            raise self._error_from(status, raw) from None
        except Exception as exc:  # noqa: BLE001
            self._record("http", path, payload, 0, started, ok=False, detail=str(exc)[:300])
            raise PowerContextError(
                f"无法连接 PowerContext（{self.base_url}）：{exc}。"
                "请先按 README 启动 powercontext server。"
            ) from exc

        self._record("http", path, payload, status, started, ok=True)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PowerContextError(f"PowerContext 返回非 JSON：{raw[:200]}") from exc

    @staticmethod
    def _error_from(status: int, raw: str) -> PowerContextError:
        code = ""
        message = raw[:300]
        try:
            payload = json.loads(raw)
            error = payload.get("error") or {}
            code = error.get("code", "")
            message = error.get("message", message)
            details = error.get("details") or {}
            if isinstance(details, dict) and details.get("message"):
                message = f"{message}（{details['message']}）"
        except json.JSONDecodeError:
            pass
        return PowerContextError(f"PowerContext HTTP {status}：{message}", status, code)

    def _record(
        self,
        kind: str,
        path: str,
        payload: Optional[dict],
        status: int,
        started: float,
        *,
        ok: bool,
        detail: str = "",
    ) -> None:
        summary: Dict[str, Any] = {}
        if isinstance(payload, dict):
            for key in ("scope_id", "kind", "query", "source_id", "text", "reason"):
                if key in payload and payload[key] is not None:
                    value = str(payload[key])
                    summary[key] = value if len(value) <= 160 else value[:160] + "…"
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "op": _op_name(path),
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

    # ---------------------------------------------------------------- 健康
    def ping(self) -> bool:
        try:
            self._request("GET", "/health/live")
            return True
        except PowerContextError:
            return False

    # ---------------------------------------------------------------- Scope
    def resolve_scope(self, title: str, summary: str) -> str:
        """返回可用的 scope_id：环境变量 > 本地状态文件 > 新建。"""
        if self.scope_id:
            return self.scope_id
        if self._state_path.is_file():
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
                scope_id = str(state.get("scope_id", "")).strip()
                # 服务端数据可能被清空或换了实例，缓存里的 scope 会 404；
                # 这种情况必须重建，否则后续每次记忆读写都会失败。
                if scope_id and self._scope_exists(scope_id):
                    self.scope_id = scope_id
                    return scope_id
            except (json.JSONDecodeError, OSError):
                pass

        payload = {"title": title, "summary": summary, "idempotency_key": f"researchnest-{_slug(title)}"}
        try:
            result = self._request("POST", "/v1/scopes", payload)
            scope_id = result.get("scope_id")
        except PowerContextError as exc:
            if exc.status != 409:
                raise
            scope_id = self._find_scope(title)
        if not scope_id:
            raise PowerContextError("Scope 创建成功但响应缺少 scope_id")
        self.scope_id = str(scope_id)
        self._state_path.write_text(
            json.dumps(
                {"scope_id": self.scope_id, "title": title, "created_at": datetime.now().isoformat()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.scope_id

    def _scope_exists(self, scope_id: str) -> bool:
        """用一个轻量读接口确认 scope 在服务端仍然存在。"""
        try:
            self._request(
                "POST",
                "/v1/memory/entries/list",
                {"scope_id": scope_id, "include_inactive": False},
            )
            return True
        except PowerContextError as exc:
            if exc.status == 404:
                return False
            # 鉴权失败、网络抖动等不应被当成 scope 失效，否则会误建重复 scope。
            return True

    def _find_scope(self, title: str) -> str:
        from urllib.parse import quote

        page = self._request("GET", f"/v1/scopes?query={quote(title)}&limit=50")
        scopes = page.get("scopes") or []
        for item in scopes:
            if item.get("title") == title:
                return str(item.get("scope_id", ""))
        if len(scopes) == 1:
            return str(scopes[0].get("scope_id", ""))
        raise PowerContextError(
            "同名 Scope 已存在但无法定位。请删除 .state/powercontext-scope.json 后重试，"
            "或直接设置 POWERCONTEXT_SCOPE_ID。"
        )

    # ---------------------------------------------------------------- Memory
    def remember(self, kind: str, text: str, reason: str = "") -> Dict[str, Any]:
        assert self.scope_id, "请先调用 resolve_scope"
        payload = {
            "scope_id": self.scope_id,
            "kind": kind,
            "text": text,
        }
        if reason:
            payload["reason"] = reason
        return self._request("POST", "/v1/memory/remember", payload)

    def search(self, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        assert self.scope_id, "请先调用 resolve_scope"
        result = self._request(
            "POST",
            "/v1/memory/search",
            {"scope_id": self.scope_id, "query": query, "limit": limit},
        )
        hits = []
        for hit in result.get("hits") or []:
            citation = hit.get("citation") or {}
            hits.append(
                {
                    "text": hit.get("text", ""),
                    "score": hit.get("score", 0),
                    "matched_by": hit.get("matched_by", []),
                    "entry_id": citation.get("entry_id", ""),
                    "citation": citation,
                }
            )
        return hits

    def list_entries(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        assert self.scope_id, "请先调用 resolve_scope"
        result = self._request(
            "POST",
            "/v1/memory/entries/list",
            {"scope_id": self.scope_id, "include_inactive": include_inactive},
        )
        entries = []
        for entry in result.get("entries") or []:
            entries.append(
                {
                    "kind": entry.get("kind", ""),
                    "text": entry.get("text", ""),
                    "state": entry.get("state", ""),
                    "version": entry.get("version", 1),
                    "entry_id": (entry.get("citation") or {}).get("entry_id", ""),
                    "citation": entry.get("citation") or {},
                    "source_refs": entry.get("source_refs", []),
                }
            )
        return entries

    def retire(self, citation: Dict[str, Any], reason: str = "") -> Dict[str, Any]:
        assert self.scope_id, "请先调用 resolve_scope"
        payload: Dict[str, Any] = {"scope_id": self.scope_id, "citation": citation}
        if reason:
            payload["reason"] = reason
        return self._request("POST", "/v1/memory/entries/retire", payload)

    def prepare(self, query: str, max_bytes: int = 4000) -> Dict[str, Any]:
        assert self.scope_id, "请先调用 resolve_scope"
        payload = {
            "scope_id": self.scope_id,
            "query": query,
            "max_bytes": max_bytes,
            "assembly": {
                "format": "markdown",
                "sections": [{"family": "memory", "limit": 6}],
            },
        }
        return self._request("POST", "/v1/context/prepare", payload)

    # ---------------------------------------------------------------- Source
    def capture_source(
        self, source_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        assert self.scope_id, "请先调用 resolve_scope"
        payload = {
            "scope_id": self.scope_id,
            "source_id": source_id,
            "content": content,
            "metadata": metadata or {},
        }
        return self._request("POST", "/v1/sources/content", payload)

    # ---------------------------------------------------------------- 记录
    def read_calls(self, limit: int = 30) -> List[Dict[str, Any]]:
        if not self._log_path.is_file():
            return []
        lines = self._log_path.read_text(encoding="utf-8").splitlines()[-max(1, limit * 4) :]
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(records))[:limit]


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text.lower())
    return "-".join(filter(None, cleaned.split("-")))[:40] or "scope"


def _op_name(path: str) -> str:
    mapping = {
        "/v1/memory/remember": "写入记忆 remember",
        "/v1/memory/search": "检索记忆 search",
        "/v1/memory/entries/list": "列出记忆 list",
        "/v1/memory/entries/retire": "停用记忆 retire",
        "/v1/context/prepare": "准备上下文 prepare",
        "/v1/sources/content": "保存证据 source",
        "/v1/scopes": "创建 Scope",
        "/health/live": "健康检查",
    }
    for key, value in mapping.items():
        if path.startswith(key):
            return value
    return path


def _error_detail(raw: str) -> str:
    try:
        payload = json.loads(raw)
        error = payload.get("error") or {}
        return f"{error.get('code', '')}: {error.get('message', '')}"[:300]
    except json.JSONDecodeError:
        return raw[:300]
