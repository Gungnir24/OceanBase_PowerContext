"""最小可运行的 HTTP 服务：只暴露 /health 与 /api/products。

启动方式::

    python -m catalog.server --port 8791 --data workdir/data/products.json

业务工程本身不依赖任何第三方库，方便评审在干净环境直接跑起来。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .api import PRODUCTS_PATH, handle_products_query
from .store import ProductRepository


def _flatten_query(raw_query: str) -> dict[str, str]:
    parsed = parse_qs(raw_query, keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def build_handler(repo: ProductRepository):
    class Handler(BaseHTTPRequestHandler):
        server_version = "catalog/0.2"

        def _write_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._write_json(200, {"status": "ok", "products": len(repo)})
                return
            if parsed.path == PRODUCTS_PATH:
                status, payload = handle_products_query(repo, _flatten_query(parsed.query))
                self._write_json(status, payload)
                return
            self._write_json(404, {"error": {"code": "NOT_FOUND", "message": parsed.path}})

        def log_message(self, fmt: str, *args) -> None:  # 静默，避免污染测试输出
            return

    return Handler


def serve(data_path: str | Path, host: str = "127.0.0.1", port: int = 8791) -> None:
    repo = ProductRepository.from_json_file(data_path)
    httpd = ThreadingHTTPServer((host, port), build_handler(repo))
    print(f"catalog serving on http://{host}:{port}  (/health, {PRODUCTS_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="catalog demo service")
    parser.add_argument("--data", default=str(Path(__file__).parents[2] / "data" / "products.json"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args(argv)
    serve(args.data, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
