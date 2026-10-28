"""领域模型：商品与统一的 JSON 序列化形式。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Product:
    """商品。

    价格以「分」为整数存储，避免浮点误差；对外展示时再换算成元。
    """

    id: str
    name: str
    price_cents: int
    stock: int
    category: str
    tags: tuple[str, ...]

    @property
    def price_yuan(self) -> float:
        return round(self.price_cents / 100, 2)

    def to_public_dict(self) -> dict[str, Any]:
        """对外响应里的商品字段；字段集合属于兼容性契约。"""
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price_yuan,
            "stock": self.stock,
            "category": self.category,
            "tags": list(self.tags),
        }
