"""HTTP 接口层：query string -> (status, payload)。

契约来源：``data/specs/API-catalog-v2.md`` §COMPAT-003 §FILTER-003

这一层只负责「把字符串参数原样交给 service」，参数取值是否合法由 service
与 filters 判断；错误统一由 ApiError 映射成 4xx（见 §FILTER-003）。
"""

from __future__ import annotations

from typing import Mapping

from .errors import ApiError
from .service import (
    DEFAULT_PAGE,
    DEFAULT_PAGE_SIZE,
    parse_page,
    parse_page_size,
    search_products,
)
from .store import ProductRepository

PRODUCTS_PATH = "/api/products"


def handle_products_query(repo: ProductRepository, query: Mapping[str, str]) -> tuple[int, dict]:
    """处理 ``GET /api/products``。

    ``query`` 是已经解码的 query string（多值取第一个）。

    新增筛选参数（FILTER-001, FILTER-002）：
    * price_min / price_max：价格区间（单位元，闭区间）。
    * in_stock_only：仅返回有货商品（true/1/yes 为真，false/0/no 为假，其余非法）。
    * 旧参数 min_price / max_price 按 ADR-0011 必须忽略。
    """
    try:
        page = parse_page(query.get("page", DEFAULT_PAGE))
        page_size = parse_page_size(query.get("page_size", DEFAULT_PAGE_SIZE))

        # ADR-0011：忽略旧参数名 min_price / max_price
        price_min = query.get("price_min")
        price_max = query.get("price_max")
        in_stock_only = query.get("in_stock_only")

        payload = search_products(
            repo,
            keyword=query.get("keyword", ""),
            sort=query.get("sort", "relevance"),
            page=page,
            page_size=page_size,
            price_min=price_min,
            price_max=price_max,
            in_stock_only=in_stock_only,
        )
        return 200, payload
    except ApiError as exc:
        return exc.status, exc.to_payload()
