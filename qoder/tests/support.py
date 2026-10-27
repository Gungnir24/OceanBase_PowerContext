"""测试辅助：把项目根目录加入 sys.path，并提供一个假的 PowerContext 服务。"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakePowerContext:
    """实现本项目用到的 PowerContext HTTP 接口，用于离线联调。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.memories: list[dict] = []
        self.counter = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakePowerContext":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return

            def _json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _read(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self):  # noqa: N802
                outer.calls.append({"method": "GET", "path": self.path})
                if self.path.startswith("/health/live"):
                    self._json(200, {"status": "ok"})
                    return
                if self.path.startswith("/v1/scopes"):
                    self._json(200, {"scopes": [], "next_cursor": None})
                    return
                self._json(404, {"error": {"code": "not_found", "message": "no"}})

            def do_POST(self):  # noqa: N802
                payload = self._read()
                outer.calls.append({"method": "POST", "path": self.path, "body": payload})
                auth = self.headers.get("Authorization", "")
                if self.path == "/v1/scopes":
                    self._json(201, {
                        "scope_id": "scope-test",
                        "title": payload.get("title", ""),
                        "summary": payload.get("summary", ""),
                        "context_references": [],
                        "external_references": [],
                        "version": 1,
                    })
                    return
                if self.path == "/v1/memory/remember":
                    outer.counter += 1
                    entry = {
                        "kind": payload.get("kind", "fact"),
                        "text": payload.get("text", ""),
                        "state": "active",
                        "version": 1,
                        "citation": {
                            "entry_id": f"entry-{outer.counter}",
                            "entry_version_id": f"ver-{outer.counter}",
                            "memory_ref": {"family": "memory", "artifact_id": "mem", "revision": 1},
                        },
                    }
                    outer.memories.append(entry)
                    self._json(200, {
                        "memory": {"family": "memory", "artifact_id": "mem", "revision": 1},
                        "entry": entry,
                    })
                    return
                if self.path == "/v1/memory/search":
                    hits = [
                        {
                            "citation": item["citation"],
                            "text": item["text"],
                            "score": 0.88,
                            "matched_by": ["fulltext"],
                        }
                        for item in outer.memories
                    ]
                    self._json(200, {"hits": hits, "memory": None, "mode": "auto"})
                    return
                if self.path == "/v1/memory/entries/list":
                    self._json(200, {"entries": outer.memories, "memory": None})
                    return
                if self.path == "/v1/memory/entries/retire":
                    self._json(200, {"retired": True})
                    return
                if self.path == "/v1/context/prepare":
                    content = "# Prepared\n\n- 用户偏好：先结论后依据"
                    self._json(200, {
                        "status": "ready",
                        "content": content,
                        "content_bytes": len(content.encode("utf-8")),
                    })
                    return
                if self.path == "/v1/sources/content":
                    self._json(200, {"source": {
                        "source_id": payload.get("source_id"),
                        "metadata": payload.get("metadata", {}),
                    }})
                    return
                self._json(404, {"error": {"code": "not_found", "message": "no"}})

        return Handler
