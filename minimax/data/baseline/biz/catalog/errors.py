"""统一错误对象：接口层只认 ApiError，其余异常一律 500。"""

from __future__ import annotations


class ApiError(Exception):
    """带错误码的请求错误。

    ``code`` 会原样出现在响应体的 ``error.code`` 字段里，因此错误码本身
    属于对外契约的一部分，修改需要同步规格（见 data/specs/API-catalog-v2.md）。
    """

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def to_payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
