"""统一错误响应：{"error": {"code": "...", "message": "..."}}"""

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.extra:
        detail.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content={"error": detail})
