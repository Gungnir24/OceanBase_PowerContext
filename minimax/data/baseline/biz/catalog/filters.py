"""检索过滤与排序。

契约来源：
* ``data/specs/API-catalog-v2.md``  §COMPAT-001（关键字匹配）
* ``data/specs/API-catalog-v2.md``  §COMPAT-002（排序）
* ``data/specs/API-catalog-v2.md``  §COMPAT-004（分页默认值）

本模块只做「纯函数式」的过滤/排序，不感知 HTTP。新增过滤条件应加在这里，
再由 ``service.search_products`` 组合，避免把参数解析散落到接口层。
"""

from __future__ import annotations

from typing import Iterable

from .errors import ApiError
from .models import Product

SORT_OPTIONS: tuple[str, ...] = ("relevance", "price_asc", "price_desc", "name_asc")
DEFAULT_SORT = "relevance"


def normalize_keyword(keyword: str | None) -> str:
    return (keyword or "").strip().lower()


def matches_keyword(product: Product, keyword: str | None) -> bool:
    """大小写不敏感的子串匹配：命中商品名或任意 tag 即算命中。"""
    needle = normalize_keyword(keyword)
    if not needle:
        return True
    if needle in product.name.lower():
        return True
    return any(needle in tag.lower() for tag in product.tags)


def apply_keyword(products: Iterable[Product], keyword: str | None) -> list[Product]:
    return [product for product in products if matches_keyword(product, keyword)]


def sort_products(products: list[Product], sort: str | None) -> list[Product]:
    """按 ``sort`` 排序；未知取值属于非法请求。"""
    key = (sort or DEFAULT_SORT).strip().lower()
    if key not in SORT_OPTIONS:
        raise ApiError(
            "INVALID_SORT",
            f"sort 仅支持 {', '.join(SORT_OPTIONS)}，收到 {sort!r}",
        )
    if key == "relevance":
        # 默认顺序即种子顺序，保持稳定，不能因为过滤而被打乱。
        return list(products)
    if key == "price_asc":
        return sorted(products, key=lambda product: (product.price_cents, product.id))
    if key == "price_desc":
        return sorted(products, key=lambda product: (-product.price_cents, product.id))
    return sorted(products, key=lambda product: (product.name, product.id))
