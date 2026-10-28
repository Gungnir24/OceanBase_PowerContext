"""检索服务：把仓储 + 过滤 + 分页组合成一次查询。

契约来源：
* ``data/specs/API-catalog-v2.md``  §COMPAT-002 §COMPAT-003 §COMPAT-004
* ``data/specs/API-catalog-v2.md``  §FILTER-001（价格区间筛选）
* ``data/specs/API-catalog-v2.md``  §FILTER-002（仅看有货）
* ``data/specs/API-catalog-v2.md``  §FILTER-004（组合与 total 语义）

本函数是对外行为的中枢：所有筛选条件都在这里按「过滤 -> 排序 -> 分页」
的顺序组合，``total`` 必须是过滤后、分页前的总数。
"""

from __future__ import annotations

from .errors import ApiError
from .filters import apply_keyword, sort_products
from .models import Product
from .store import ProductRepository

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def parse_page(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_PAGE
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError("INVALID_PAGINATION", f"page 必须是正整数，收到 {raw!r}") from None
    if value < 1:
        raise ApiError("INVALID_PAGINATION", f"page 必须 >= 1，收到 {value}")
    return value


def parse_page_size(raw: str | int | None) -> int:
    if raw is None or raw == "":
        return DEFAULT_PAGE_SIZE
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        raise ApiError("INVALID_PAGINATION", f"page_size 必须是正整数，收到 {raw!r}") from None
    if value < 1:
        raise ApiError("INVALID_PAGINATION", f"page_size 必须 >= 1，收到 {value}")
    if value > MAX_PAGE_SIZE:
        raise ApiError("INVALID_PAGINATION", f"page_size 最大 {MAX_PAGE_SIZE}，收到 {value}")
    return value


def paginate(products: list[Product], page: int, page_size: int) -> list[Product]:
    start = (page - 1) * page_size
    if start >= len(products):
        return []
    return products[start : start + page_size]


def search_products(
    repo: ProductRepository,
    *,
    keyword: str | None = "",
    sort: str | None = "relevance",
    page: int = DEFAULT_PAGE,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict:
    """返回 ``{"items": [...], "total": int, "page": int, "page_size": int}``。"""
    products = repo.all()
    products = apply_keyword(products, keyword)
    products = sort_products(products, sort)

    total = len(products)
    page_items = paginate(products, page, page_size)
    return {
        "items": [product.to_public_dict() for product in page_items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
