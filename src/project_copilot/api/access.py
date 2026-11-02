"""Server-derived identity for public endpoints; local mode remains unchanged."""
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from project_copilot.api.auth import _token_from_cookie, require_origin
from project_copilot.services.auth import AuthService, AuthenticationRejected, csrf_matches


def public_mode(settings) -> bool:
    return bool(getattr(settings, "public_demo", False) or settings.environment == "production")


def public_identity(request: Request, session: Session, *, write: bool = False, lock: bool = False):
    if not public_mode(request.app.state.settings):
        return None
    try:
        token = _token_from_cookie(request)
        if write:
            require_origin(request)
            values = request.headers.getlist("x-csrf-token")
            if len(values) != 1 or not csrf_matches(token, values[0]):
                raise HTTPException(403, "请求校验失败")
        principal, _ = AuthService(session).authenticate(token, for_update=write or lock)
        return principal
    except AuthenticationRejected:
        raise HTTPException(401, "账号或会话无效") from None
