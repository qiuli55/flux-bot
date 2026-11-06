"""异常 → HTTP 响应映射（主规格 §12.10 所有 API 必须有错误码）。

只注册可预期异常的处理器。未预期异常交给 FastAPI 默认的 500 处理，
让真正的 bug 在测试中暴露出来，而不是被包装成"友好的"错误响应。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from flux.api.response import fail
from flux.errors import AIOSError
from flux.logging import get_logger

logger = get_logger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AIOSError)
    async def _domain_error(_: Request, exc: AIOSError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("领域异常 code=%s message=%s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=fail(exc.code, exc.message, details=exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=fail(
                "validation_error",
                "请求参数校验失败",
                details=exc.errors(),
            ),
        )
