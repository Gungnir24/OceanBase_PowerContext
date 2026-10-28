"""数据访问层：把 JSON 种子数据加载成内存仓储。

真实项目里这一层会是 SQL，本示例为了「干净环境可跑」用内存实现，
接口边界保持一致，方便替换。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .models import Product


def yuan_to_cents(raw: str | int | float | Decimal) -> int:
    """把「元」金额换算成「分」，按四舍五入保留两位小数。"""
    value = Decimal(str(raw)).quantize(Decimal("0.01"))
    return int(value * 100)


class ProductRepository:
    def __init__(self, products: Iterable[Product]) -> None:
        self._products: tuple[Product, ...] = tuple(products)

    def all(self) -> list[Product]:
        """返回商品全集；顺序即种子顺序，也就是默认的相关度顺序。"""
        return list(self._products)

    def get(self, product_id: str) -> Product | None:
        for product in self._products:
            if product.id == product_id:
                return product
        return None

    def __len__(self) -> int:
        return len(self._products)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "ProductRepository":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        products = [
            Product(
                id=item["id"],
                name=item["name"],
                price_cents=yuan_to_cents(item["price"]),
                stock=int(item["stock"]),
                category=item["category"],
                tags=tuple(item.get("tags", [])),
            )
            for item in payload["products"]
        ]
        return cls(products)
