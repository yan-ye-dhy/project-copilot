"""D6请求关联与基本耗时；不创建Phoenix/OTel客户端或导出Trace。"""

import logging
import time
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger(__name__)
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class RequestContextMiddleware:
    """服务端生成run_id；限制实际HTTP body，业务层另限制文件字节。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Settings已在lifespan加载；不因模块导入或构造app而读取.env。
        settings = scope["app"].state.settings
        run_id = str(uuid4())
        scope.setdefault("state", {})["run_id"] = run_id
        started = time.monotonic()
        status = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Run-ID"] = run_id
            await send(message)

        limited = RequestBodyLimitMiddleware(
            self.app, max_body_size=settings.upload_max_bytes + MULTIPART_OVERHEAD_BYTES,
        )
        try:
            await limited(scope, receive, send_with_id)
        finally:
            # 不记录请求正文、文件内容、请求头、URL查询参数或异常原文。
            logger.info("run_id=%s method=%s path=%s status=%s elapsed_ms=%.2f trace_exported=false",
                        run_id, scope["method"], scope["path"], status,
                        (time.monotonic() - started) * 1000)
