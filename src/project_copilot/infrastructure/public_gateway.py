"""Fail-closed public ingress guard; never log headers or secret values.

Public business traffic remains unavailable until authentication, tenant
isolation and quota controls have all been integrated and verified.
"""

import hmac
from project_copilot.services.auth import AuthenticationRejected

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class PublicGatewayMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _admit(scope):
        from project_copilot.api.auth import _token_from_cookie
        from project_copilot.services.auth import AuthService
        from project_copilot.services.release_control import ReleaseClosed, dependencies_ready, require_open
        database = scope["app"].state.database
        settings = scope["app"].state.settings
        if database is None:
            raise ReleaseClosed()
        path = scope.get("path", "")
        # Operational/debug/synchronous legacy endpoints are never opened.
        if path in {"/docs", "/redoc", "/openapi.json", "/agent/ask", "/rag/ask", "/documents/upload"}:
            raise ReleaseClosed()
        with database.transaction() as session:
            release = require_open(session, settings)
            personal = release.state == "PERSONAL_DEMO"
            if personal and path == "/auth/register":
                # No invitation can enlarge the personal demo's existing identity.
                raise ReleaseClosed()
            if path not in {"/health", "/auth/login", "/auth/register"}:
                principal, _ = AuthService(session).authenticate(_token_from_cookie(Request(scope)))
                if personal or path not in {"/auth/me", "/auth/logout"}:
                    require_open(session, settings, user_id=principal.id)
        dependencies_ready(settings)

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
            try:
                await run_in_threadpool(self._admit, scope)
            except AuthenticationRejected:
                response = JSONResponse({"detail": "登录状态已失效"}, status_code=401,
                                        headers={"Cache-Control": "no-store"})
                await response(scope, receive, send)
                return
            except Exception:
                # The explicit CLOSED hard gate remains the default. Missing
                # migration, expired evidence or unavailable dependencies close it.
                pass
            else:
                await self.app(scope, receive, send)
                return
            response = JSONResponse({"detail": "公开服务尚未完成安全验收"}, status_code=503,
                                    headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)
