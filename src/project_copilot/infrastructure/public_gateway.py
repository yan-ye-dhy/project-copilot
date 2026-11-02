"""Fail-closed public ingress guard; never log headers or secret values.

Public business traffic remains unavailable until authentication, tenant
isolation and quota controls have all been integrated and verified.
"""

import hmac

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class PublicGatewayMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        settings = scope["app"].state.settings
        if not settings.public_demo and settings.environment != "production":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        supplied = [value for key, value in scope.get("headers", [])
                    if key.lower() == b"x-project-copilot-proxy-secret"]
        expected = settings.public_proxy_secret.get_secret_value().encode("ascii")
        valid = (len(supplied) == 1 and len(supplied[0]) == len(expected)
                 and hmac.compare_digest(supplied[0], expected))
        if not valid:
            response = JSONResponse({"detail": "请求未获授权"}, status_code=403,
                                    headers={"Cache-Control": "no-store"})
        else:
            # Do not expose the old single-user APIs during staged development.
            response = JSONResponse({"detail": "公开服务尚未完成安全验收"}, status_code=503,
                                    headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)
