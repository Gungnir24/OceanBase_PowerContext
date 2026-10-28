"""回归测试：修改前已有的行为，修改后必须原样保持。

对应规格：
* §COMPAT-001 关键字匹配（大小写不敏感的子串匹配，覆盖商品名与 tag）
* §COMPAT-002 排序（relevance / price_asc / price_desc / name_asc）
* §COMPAT-003 响应结构
* §COMPAT-004 分页默认值与上限
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from conftest import ids, search


def test_default_query_returns_first_page(repo):
    status, payload = search(repo)
    assert status == 200
    assert payload["page"] == 1
    assert payload["page_size"] == 20
    assert payload["total"] == 20
    assert len(payload["items"]) == 20
    assert ids(payload)[0] == "p-1001"


def test_response_shape_is_stable(repo):
    _, payload = search(repo, page_size=1)
    assert set(payload) == {"items", "total", "page", "page_size"}
    item = payload["items"][0]
    assert set(item) == {"id", "name", "price", "stock", "category", "tags"}
    assert isinstance(item["price"], float)
    assert item["price"] == 299.0
    assert item["tags"] == ["键盘", "外设", "办公"]


def test_pagination_slices_without_changing_total(repo):
    _, payload = search(repo, page=2, page_size=5)
    assert payload["total"] == 20
    assert payload["page"] == 2
    assert payload["page_size"] == 5
    assert ids(payload) == ["p-1006", "p-1007", "p-1008", "p-1009", "p-1010"]


def test_page_size_above_cap_is_rejected(repo):
    status, payload = search(repo, page_size=500)
    assert status == 400
    assert payload["error"]["code"] == "INVALID_PAGINATION"


def test_page_zero_is_rejected(repo):
    status, payload = search(repo, page=0)
    assert status == 400
    assert payload["error"]["code"] == "INVALID_PAGINATION"


def test_keyword_matches_name(repo):
    _, payload = search(repo, keyword="键盘")
    assert sorted(ids(payload)) == ["p-1001", "p-1010"]
    assert payload["total"] == 2


def test_keyword_is_case_insensitive_across_tags(repo):
    _, payload = search(repo, keyword="usb-c")
    assert ids(payload) == ["p-1004"]
    _, payload = search(repo, keyword="USB-C")
    assert ids(payload) == ["p-1004"]


def test_keyword_without_match_returns_empty_page(repo):
    _, payload = search(repo, keyword="不存在的商品")
    assert payload["total"] == 0
    assert payload["items"] == []


def test_sort_price_asc(repo):
    _, payload = search(repo, sort="price_asc", page_size=3)
    assert ids(payload) == ["p-1020", "p-1008", "p-1010"]


def test_sort_price_desc(repo):
    _, payload = search(repo, sort="price_desc", page_size=3)
    assert ids(payload) == ["p-1016", "p-1007", "p-1003"]


def test_sort_name_asc(repo):
    _, payload = search(repo, sort="name_asc", page_size=3)
    names = [item["name"] for item in payload["items"]]
    assert names == sorted(names)


def test_unknown_sort_is_rejected(repo):
    status, payload = search(repo, sort="stock_desc")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_SORT"


def test_relevance_order_keeps_seed_order_after_keyword(repo):
    _, payload = search(repo, keyword="外设")
    assert ids(payload) == ["p-1001", "p-1002", "p-1003", "p-1004"]


def test_http_server_serves_health_and_products(repo):
    from catalog.server import build_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(repo))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as response:
            assert json.loads(response.read())["status"] == "ok"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/products?page_size=1", timeout=5
        ) as response:
            payload = json.loads(response.read())
        assert payload["total"] == 20
        assert len(payload["items"]) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
