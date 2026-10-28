"""测试公共夹具：加载种子数据，并把 query dict 直接喂给接口层。

这些测试是「验收口径」的一部分，写在规格 ``data/specs/API-catalog-v2.md`` 里，
修改前后使用同一份测试文件。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BIZ_DIR = Path(__file__).resolve().parents[1] / "biz"
if str(BIZ_DIR) not in sys.path:
    sys.path.insert(0, str(BIZ_DIR))

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "products.json"

from catalog.api import handle_products_query  # noqa: E402
from catalog.store import ProductRepository  # noqa: E402


@pytest.fixture(scope="session")
def seed_payload() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def repo(seed_payload) -> ProductRepository:
    return ProductRepository.from_json_file(DATA_FILE)


@pytest.fixture(scope="session")
def repo_from_payload(seed_payload) -> ProductRepository:
    """不读文件的等价仓储，防止种子文件被误改后测试仍然通过。"""
    from catalog.store import yuan_to_cents

    from catalog.models import Product

    return ProductRepository(
        Product(
            id=item["id"],
            name=item["name"],
            price_cents=yuan_to_cents(item["price"]),
            stock=int(item["stock"]),
            category=item["category"],
            tags=tuple(item.get("tags", [])),
        )
        for item in seed_payload["products"]
    )


def search(repo: ProductRepository, **query) -> tuple[int, dict]:
    """调用 ``GET /api/products`` 的接口层，返回 (status, payload)。"""
    return handle_products_query(repo, {key: str(value) for key, value in query.items()})


def ids(payload: dict) -> list[str]:
    return [item["id"] for item in payload["items"]]
