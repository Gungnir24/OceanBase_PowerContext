"""研栖 ResearchNest 的 HTTP 服务（标准库实现，无 Web 框架依赖）。

运行：
    python app/server.py            # 读取 .env，连接 seekdb 与 PowerContext
    python app/server.py --demo     # 演示模式：内存数据，不依赖外部服务
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import STATIC_DIR, Config  # noqa: E402
from app.demo import MemoryPowerContext, MemorySeekStore  # noqa: E402
from app.powercontext import PowerContextError  # noqa: E402
from app.seekdb_store import SeekDBError  # noqa: E402
from app.service import AppService  # noqa: E402

MAX_BODY_BYTES = 6 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ResearchNestHandler(BaseHTTPRequestHandler):
    service: AppService = None  # type: ignore[assignment]
    server_version = "ResearchNest/1.0"

    # ------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        started = time.perf_counter()
        status = 500
        try:
            if path.startswith("/api/"):
                status = self._handle_api(method, path, parse_qs(parsed.query))
            elif method == "GET":
                status = self._serve_static(path)
            else:
                raise ApiError(405, "method_not_allowed", "不支持的请求方法")
        except ApiError as exc:
            status = exc.status
            self._send_json(status, {"error": {"code": exc.code, "message": exc.message}})
        except ValueError as exc:
            status = 400
            self._send_json(status, {"error": {"code": "invalid_request", "message": str(exc)}})
        except (SeekDBError, PowerContextError) as exc:
            status = 503
            self._send_json(status, {"error": {"code": "service_unavailable", "message": str(exc)}})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            status = 500
            traceback.print_exc()
            self._send_json(
                status,
                {"error": {"code": "internal_error", "message": f"{type(exc).__name__}: {exc}"}},
            )
        finally:
            elapsed = int((time.perf_counter() - started) * 1000)
            print(f"{method:6s} {path[:80]:80s} {status} {elapsed:4d}ms", flush=True)

    def _handle_api(self, method: str, path: str, query: Dict[str, list[str]]) -> int:
        service = self.service
        body: Dict[str, Any] = {}
        if method in {"POST", "PUT", "PATCH"}:
            body = self._read_json()

        if method == "GET" and path == "/api/health":
            self._send_json(200, service.health())
            return 200

        if method == "GET" and path == "/api/papers":
            self._send_json(200, {"papers": service.list_papers()})
            return 200

        if method == "POST" and path == "/api/papers":
            name = str(body.get("name") or "未命名.md").strip()
            content = str(body.get("content") or "")
            if not content.strip():
                raise ValueError("文档内容不能为空")
            result = service.ingest(name, content)
            self._send_json(201, result)
            return 201

        if method == "POST" and path == "/api/samples/import":
            results = service.import_samples()
            self._send_json(200, {"imported": results, "count": len(results)})
            return 200

        if method == "POST" and path == "/api/search":
            result = service.search_only(
                str(body.get("query") or ""),
                filters=body.get("filters") or {},
                top_k=int(body.get("top_k") or 8),
            )
            self._send_json(200, result)
            return 200

        if method == "POST" and path == "/api/ask":
            result = service.ask(
                str(body.get("question") or ""),
                filters=body.get("filters") or {},
                top_k=int(body.get("top_k") or 6),
            )
            self._send_json(200, result)
            return 200

        if method == "GET" and path == "/api/memories":
            self._send_json(200, {"memories": service.memories()})
            return 200

        if method == "POST" and path == "/api/memories":
            result = service.remember(
                str(body.get("text") or ""),
                kind=str(body.get("kind") or "fact"),
                reason=str(body.get("reason") or ""),
            )
            self._send_json(201, result)
            return 201

        if method == "POST" and path == "/api/memories/retire":
            citation = body.get("citation") or {}
            if not isinstance(citation, dict) or not citation.get("entry_id"):
                raise ValueError("缺少 citation.entry_id")
            result = service.retire_memory(citation, reason=str(body.get("reason") or ""))
            self._send_json(200, result)
            return 200

        if method == "GET" and path == "/api/weekly":
            days = int((query.get("days") or ["7"])[0])
            focus = (query.get("focus") or [""])[0]
            self._send_json(200, service.weekly(days=days, focus=focus))
            return 200

        if method == "POST" and path == "/api/weekly/archive":
            result = service.archive_weekly(
                str(body.get("markdown") or ""), str(body.get("summary") or "")
            )
            self._send_json(200, result)
            return 200

        if method == "GET" and path == "/api/logs":
            limit = int((query.get("limit") or ["20"])[0])
            self._send_json(200, service.logs(limit=limit))
            return 200

        if method == "GET" and path == "/api/stats":
            self._send_json(200, service.stats())
            return 200

        # /api/papers/{id}
        if path.startswith("/api/papers/"):
            paper_id = path[len("/api/papers/") :].strip("/")
            if method == "GET":
                paper = service.get_paper(paper_id)
                if not paper:
                    raise ApiError(404, "not_found", "文献不存在")
                self._send_json(200, paper)
                return 200
            if method == "DELETE":
                deleted = service.delete_paper(paper_id)
                if not deleted:
                    raise ApiError(404, "not_found", "文献不存在")
                self._send_json(200, {"deleted": True, "paper_id": paper_id})
                return 200

        raise ApiError(404, "not_found", f"未知接口：{method} {path}")

    # ------------------------------------------------------------- 静态文件
    def _serve_static(self, path: str) -> int:
        if path in {"/", "/index.html"}:
            file_path = STATIC_DIR / "index.html"
        elif path == "/app.js":
            file_path = STATIC_DIR / "app.js"
        elif path == "/styles.css":
            file_path = STATIC_DIR / "styles.css"
        else:
            candidate = (STATIC_DIR / path.lstrip("/")).resolve()
            if not str(candidate).startswith(str(STATIC_DIR.resolve())):
                raise ApiError(403, "forbidden", "非法路径")
            file_path = candidate
        if not file_path.is_file():
            raise ApiError(404, "not_found", "页面不存在")
        content = file_path.read_bytes()
        mime, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)
        return 200

    # ------------------------------------------------------------- 工具
    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "invalid_request", "Content-Length 非法") from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", "请求体过大（上限 6MB）")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_request", "请求体必须是 JSON 对象")
        return payload

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, fmt: str, *args: Any) -> None:  # 由 _dispatch 统一打印
        return


def build_service(cfg: Config) -> AppService:
    service = AppService(cfg)
    service.initialize()
    if cfg.demo:
        try:
            service.import_samples()
            service.remember(
                "演示模式：当前数据保存在内存中，重启后恢复为示例数据。",
                kind="status",
                reason="演示提示",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"演示数据初始化失败：{exc}", flush=True)
    return service


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="研栖 ResearchNest")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--demo", action="store_true", help="使用内存数据演示，不连接 seekdb")
    args = parser.parse_args(argv)

    cfg = Config.load()
    if args.demo:
        cfg.demo = True
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    service = build_service(cfg)
    ResearchNestHandler.service = service

    server = ThreadingHTTPServer((cfg.host, cfg.port), ResearchNestHandler)
    server.daemon_threads = True
    health = service.health()
    print("=" * 66, flush=True)
    print("研栖 ResearchNest 已启动", flush=True)
    print(f"  地址:   http://{cfg.host}:{cfg.port}", flush=True)
    print(f"  模式:   {'演示（内存）' if cfg.demo else 'live'}", flush=True)
    print(f"  seekdb: {'OK' if health['seekdb'].get('ok') else '未连接'}", flush=True)
    print(
        f"  PowerContext: {'OK · ' + health['scope_id'] if health['powercontext'].get('ok') else '未连接'}",
        flush=True,
    )
    for warning in health.get("warnings", []):
        print(f"  ! {warning}", flush=True)
    print("=" * 66, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
