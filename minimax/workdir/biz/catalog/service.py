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

from decimal import Decimal, InvalidOperation

from .errors import ApiError
from .filters import apply_keyword, apply_in_stock, apply_price_range, sort_products
from .models import Product
from .store import ProductRepository

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# in_stock_only 真值集合（大小写不敏感）
_IN_STOCK_TRUE: frozenset[str] = frozenset({"true", "1", "yes"})
_IN_STOCK_FALSE: frozenset[str] = frozenset({"false", "0", "no"})


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


def parse_price(raw: str | None) -> int | None:
    """把「元」字符串解析成「分」的整数。

    最多两位小数；负数、非数字、或超过两位小数视为非法，返回 None（由调用方抛出 ApiError）。
    """
    if raw is None or raw.strip() == "":
        return None
    s = raw.strip()
    try:
        # Decimal 会把逗号当千位分隔符（如 '1,299' -> 1299），按规格这应该被拒绝。
        # 先用字符串检查：只允许数字、单个可选小数点、前面可选负号。
        if "." in s:
            integer_part, fractional_part = s.split(".", 1)
            # 整数部分只允许数字，可选前缀负号
            if integer_part.startswith("-"):
                integer_digits = integer_part[1:]
            else:
                integer_digits = integer_part
            if not integer_digits.isdigit() or len(fractional_part) > 2:
                return None
        else:
            # 无小数点：必须是纯数字（可选负号）
            if s.startswith("-"):
                digits = s[1:]
            else:
                digits = s
            if not digits.isdigit():
                return None

        value = Decimal(s)
        if value < 0:
            return None
        # 量化到两位小数，四舍五入
        quantized = value.quantize(Decimal("0.01"))
        return int(quantized * 100)
    except (InvalidOperation, ValueError):
        return None


def parse_price_range(
    price_min: str | None, price_max: str | None
) -> tuple[int | None, int | None]:
    """解析价格区间参数。

    返回 (min_cents, max_cents)；任一参数非法则抛出 ApiError(INVALID_FILTER)。
    """
    min_cents = parse_price(price_min)
    max_cents = parse_price(price_max)

    # 检查非数字/负数/超过两位小数的情况
    if price_min is not None and price_min.strip() != "" and min_cents is None:
        raise ApiError("INVALID_FILTER", f"price_min 必须是有效金额，收到 {price_min!r}")
    if price_max is not None and price_max.strip() != "" and max_cents is None:
        raise ApiError("INVALID_FILTER", f"price_max 必须是有效金额，收到 {price_max!r}")

    # 检查区间有效性
    if min_cents is not None and max_cents is not None and min_cents > max_cents:
        raise ApiError(
            "INVALID_FILTER",
            f"price_min({price_min}) 不能大于 price_max({price_max})",
        )

    return min_cents, max_cents


def parse_in_stock_only(raw: str | None) -> bool | None:
    """解析 in_stock_only 参数。

    真值集合：true / 1 / yes（大小写不敏感）
    假值集合：false / 0 / no
    省略或为空：None（不过滤）
    其他值：抛出 ApiError(INVALID_FILTER)
    """
    if raw is None or raw.strip() == "":
        return None
    normalized = raw.strip().lower()
    if normalized in _IN_STOCK_TRUE:
        return True
    if normalized in _IN_STOCK_FALSE:
        return False
    raise ApiError("INVALID_FILTER", f"in_stock_only 取值非法，收到 {raw!r}")


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
    price_min: str | None = None,
    price_max: str | None = None,
    in_stock_only: str | None = None,
) -> dict:
    """返回 ``{"items": [...], "total": int, "page": int, "page_size": int}``。

    过滤顺序（FILTER-004）：keyword -> 价格区间 -> 库存 -> 排序 -> 分页。
    """
    # 解析并验证价格区间参数
    min_cents, max_cents = parse_price_range(price_min, price_max)

    # 解析库存状态参数
    stock_filter = parse_in_stock_only(in_stock_only)

    products = repo.all()
    # 1) keyword 过滤（COMPAT-001）
    products = apply_keyword(products, keyword)
    # 2) 价格区间过滤（FILTER-001）
    products = apply_price_range(products, min_cents, max_cents)
    # 3) 库存状态过滤（FILTER-002）
    products = apply_in_stock(products, stock_filter)
    # 4) 排序（COMPAT-002）
    products = sort_products(products, sort)

    # total 是全部过滤完成后的总数，与分页无关（FILTER-004）
    total = len(products)
    page_items = paginate(products, page, page_size)
    return {
        "items": [product.to_public_dict() for product in page_items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
