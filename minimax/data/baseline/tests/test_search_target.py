"""目标功能测试：本次任务要新增的行为。

对应规格：
* §FILTER-001 价格区间筛选（闭区间、元、最多两位小数）
* §FILTER-002 仅看有货
* §FILTER-003 非法过滤参数 -> 400 / INVALID_FILTER
* §FILTER-004 过滤可与关键字/排序/分页组合，total 为过滤后总数
* ADR-0011 同时规定：已废弃的 ``min_price`` / ``max_price`` 参数名必须被忽略

这些用例在本次修改前应当全部失败（目标行为尚未实现），修改后必须全部通过。
"""

from __future__ import annotations

from decimal import Decimal

from conftest import ids, search


def _expected_ids(seed_payload, *, min_yuan=None, max_yuan=None, in_stock_only=False) -> list[str]:
    """独立于被测实现，直接从种子数据算出期望集合（差分测试）。"""
    expected = []
    for item in seed_payload["products"]:
        price = Decimal(item["price"])
        if min_yuan is not None and price < min_yuan:
            continue
        if max_yuan is not None and price > max_yuan:
            continue
        if in_stock_only and int(item["stock"]) <= 0:
            continue
        expected.append(item["id"])
    return expected


def test_price_range_is_inclusive_on_both_bounds(repo):
    status, payload = search(repo, price_min="59", price_max="129", page_size=100)
    assert status == 200
    assert payload["total"] == 6
    assert ids(payload) == ["p-1002", "p-1005", "p-1006", "p-1012", "p-1014", "p-1017"]
    # 闭区间：59.00 与 129.00 两端都必须保留
    assert "p-1005" in ids(payload)
    assert "p-1006" in ids(payload)


def test_price_min_only(repo):
    status, payload = search(repo, price_min="899", page_size=100)
    assert status == 200
    assert ids(payload) == ["p-1003", "p-1007", "p-1009", "p-1016"]


def test_price_max_only(repo):
    status, payload = search(repo, price_max="45", page_size=100)
    assert status == 200
    assert ids(payload) == ["p-1008", "p-1010", "p-1020"]


def test_decimal_bounds_keep_two_digit_precision(repo):
    status, payload = search(repo, price_min="39.9", price_max="39.90", page_size=100)
    assert status == 200
    assert ids(payload) == ["p-1008"]


def test_zero_lower_bound_keeps_all_products(repo):
    status, payload = search(repo, price_min="0", page_size=100)
    assert status == 200
    assert payload["total"] == 20


def test_in_stock_only_true_filters_out_sold_out(repo):
    status, payload = search(repo, in_stock_only="true", page_size=100)
    assert status == 200
    assert payload["total"] == 15
    returned = set(ids(payload))
    assert returned.isdisjoint({"p-1002", "p-1005", "p-1010", "p-1015", "p-1019"})


def test_in_stock_only_accepts_yes_and_one(repo):
    _, from_yes = search(repo, in_stock_only="yes", page_size=100)
    _, from_one = search(repo, in_stock_only="1", page_size=100)
    assert from_yes["total"] == 15
    assert from_one["total"] == 15


def test_in_stock_only_false_is_a_noop(repo):
    for value in ("false", "0", "no"):
        status, payload = search(repo, in_stock_only=value, page_size=100)
        assert status == 200, value
        assert payload["total"] == 20, value


def test_invalid_price_range_returns_invalid_filter(repo):
    status, payload = search(repo, price_min="200", price_max="100")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_FILTER"


def test_negative_price_returns_invalid_filter(repo):
    status, payload = search(repo, price_min="-1")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_FILTER"


def test_non_numeric_price_returns_invalid_filter(repo):
    for value in ("abc", "12x", "1,299"):
        status, payload = search(repo, price_max=value)
        assert status == 400, value
        assert payload["error"]["code"] == "INVALID_FILTER", value


def test_more_than_two_decimal_places_is_rejected(repo):
    status, payload = search(repo, price_min="1.999")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_FILTER"


def test_invalid_in_stock_value_returns_invalid_filter(repo):
    status, payload = search(repo, in_stock_only="maybe")
    assert status == 400
    assert payload["error"]["code"] == "INVALID_FILTER"


def test_total_is_counted_after_filter_and_page_can_overflow(repo, seed_payload):
    expected = _expected_ids(seed_payload, max_yuan=Decimal("100"), in_stock_only=True)
    status, payload = search(repo, price_max="100", in_stock_only="true", page=99, page_size=2)
    assert status == 200
    assert payload["total"] == len(expected)
    assert payload["items"] == []


def test_filter_combines_with_sort_and_pagination(repo, seed_payload):
    expected = _expected_ids(seed_payload, max_yuan=Decimal("100"), in_stock_only=True)
    prices = {
        item["id"]: Decimal(item["price"])
        for item in seed_payload["products"]
    }
    expected.sort(key=lambda product_id: (prices[product_id], product_id))
    status, payload = search(
        repo, price_max="100", in_stock_only="true", sort="price_asc", page=2, page_size=2
    )
    assert status == 200
    assert payload["total"] == len(expected)
    assert ids(payload) == expected[2:4]


def test_filter_combines_with_keyword(repo):
    status, payload = search(repo, keyword="键盘", in_stock_only="true", page_size=100)
    assert status == 200
    assert payload["total"] == 1
    assert ids(payload) == ["p-1001"]


def test_deprecated_parameter_names_are_ignored(repo):
    """ADR-0011：旧参数名 min_price / max_price 已被删除，必须忽略而不是静默生效。"""
    status, payload = search(repo, min_price="1000", max_price="2000", page_size=100)
    assert status == 200
    assert payload["total"] == 20
