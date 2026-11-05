"""Session authentication endpoints; standalone until explicitly mounted by the app."""
from collections.abc import Iterator
from dataclasses import dataclass, field
import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from project_copilot.db.models import LoginSession
from project_copilot.schemas.auth import LoginInput, LoginRead, RegisterInput, UserRead
from project_copilot.services.auth import AuthService, AuthenticationRejected, Principal, RegistrationRejected, csrf_matches, csrf_token
from project_copilot.services.auth_rate import consume_auth_attempt
from project_copilot.services.release_control import ReleaseClosed


logger = logging.getLogger(__name__)


class _RedactedAuthRoute(APIRoute):
    """FastAPI's default JSON/validation errors can echo credential input."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "请检查请求格式"},
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail},
                                    headers={**(exc.headers or {}), "Cache-Control": "no-store", "Pragma": "no-cache"})

        return handle


router = APIRouter(prefix="/auth", tags=["auth"], route_class=_RedactedAuthRoute)


def _is_secure(request: Request) -> bool:
    settings = request.app.state.settings
    return getattr(settings, "public_demo", False) or settings.environment == "production"


def _cookie_name(request: Request) -> str:
    return "__Host-pc_session" if _is_secure(request) else "pc_session"


def _canonical_origin(value: object, *, require_https: bool) -> str:
    if not isinstance(value, str) or not value or value == "null" or any(c in value for c in "\r\n\t "):
        raise ValueError("invalid origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError:
        raise ValueError("invalid origin") from None
    if (parsed.scheme not in ("http", "https") or (require_https and parsed.scheme != "https") or
        not host or parsed.username is not None or parsed.password is not None or
        parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("invalid origin")
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None and (parsed.scheme, port) not in (("http", 80), ("https", 443)) else ""
    return f"{parsed.scheme}://{host.lower()}{suffix}"


def require_origin(request: Request) -> None:
    secure = _is_secure(request)
    configured = getattr(request.app.state.settings, "auth_public_origin", None)
    if configured is None:
        if secure:
            raise HTTPException(503, "认证服务未正确配置")
        configured = f"{request.url.scheme}://{request.url.netloc}"
    else:
        # Pydantic Settings may supply an HttpUrl rather than a Python string.
        configured = str(configured)
    try:
        expected = _canonical_origin(configured, require_https=secure)
    except ValueError:
        raise HTTPException(503, "认证服务未正确配置") from None
    headers = request.headers.getlist("origin")
    try:
        incoming = _canonical_origin(headers[0], require_https=secure) if len(headers) == 1 else None
    except ValueError:
        incoming = None
    if incoming != expected:
        raise HTTPException(403, "请求来源无效")


def _token_from_cookie(request: Request) -> str:
    # Reject ambiguous duplicate session cookies instead of choosing the first/last.
    matches = []
    for header in request.headers.getlist("cookie"):
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == _cookie_name(request):
                matches.append(value)
    if len(matches) != 1:
        raise AuthenticationRejected()
    return matches[0]


def service(request: Request) -> Iterator[AuthService]:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(503, "认证服务暂不可用")
    try:
        with database.transaction() as session:
            settings = request.app.state.settings
            yield AuthService(session, ttl_seconds=getattr(settings, "auth_session_ttl_seconds", 86400))
    except AuthenticationRejected:
        raise HTTPException(401, "账号、密码或会话无效") from None
    except RegistrationRejected:
        raise HTTPException(400, "注册信息或邀请码无效") from None
    except ReleaseClosed:
        # Let the admission failure leave database.transaction first. Converting
        # it in the endpoint can be caught by _RedactedAuthRoute before a
        # function-scoped dependency exits, accidentally committing a session.
        raise HTTPException(503, "认证服务暂不可用") from None
    except IntegrityError as exc:
        # A concurrent duplicate username rolls back the whole transaction,
        # including an invitation increment. Do not expose database details.
        if getattr(getattr(exc.orig, "diag", None), "constraint_name", None) == "user_accounts_username_key":
            raise HTTPException(400, "注册信息或邀请码无效") from None
        logger.warning("auth database integrity error type=%s", type(exc).__name__)
        raise HTTPException(503, "认证服务暂不可用") from None
    except SQLAlchemyError as exc:
        logger.warning("auth database error type=%s", type(exc).__name__)
        raise HTTPException(503, "认证服务暂不可用") from None


Service = Annotated[AuthService, Depends(service, scope="function")]


def _release_for_auth(request: Request, svc: AuthService, *, user_id=None, registration=False):
    """Keep personal authentication in the same admission transaction.

    Gateway admission precedes body parsing. Recheck here so a close/expiry
    during that gap cannot create a session, and hold the admission lock until
    authentication commits. Standalone local auth routes keep their behavior.
    """
    # PERSONAL_DEMO itself requires production's dedicated deployment. Do not
    # impose release admission on standalone development/test auth routers.
    if (not _is_secure(request) or request.app.state.settings.environment != "production" or
            getattr(request.app.state, "database", None) is None):
        return
    from project_copilot.services.release_control import require_open
    release = require_open(svc.session, request.app.state.settings)
    if release.state == "PERSONAL_DEMO":
        if registration:
            raise HTTPException(403, "个人体验模式不允许注册")
        if user_id is not None:
            if str(user_id) not in release.allowed_user_ids:
                raise AuthenticationRejected()
            require_open(svc.session, request.app.state.settings, user_id=user_id)


@dataclass(frozen=True)
class AuthContext:
    principal: Principal
    login_session: LoginSession = field(repr=False)
    token: str = field(repr=False)


def authenticated(request: Request, svc: Service) -> AuthContext:
    token = _token_from_cookie(request)
    principal, login_session = svc.authenticate(token)
    _release_for_auth(request, svc, user_id=principal.id)
    return AuthContext(principal=principal, login_session=login_session, token=token)


Authenticated = Annotated[AuthContext, Depends(authenticated)]


def csrf_protected(request: Request, identity: Authenticated, svc: Service) -> AuthContext:
    require_origin(request)
    values = request.headers.getlist("x-csrf-token")
    if len(values) != 1 or not csrf_matches(identity.token, values[0]):
        raise HTTPException(403, "请求校验失败")
    principal, login_session = svc.authenticate(identity.token, for_update=True)
    return AuthContext(principal=principal, login_session=login_session, token=identity.token)


CsrfProtected = Annotated[AuthContext, Depends(csrf_protected)]


def _parse(model, body):
    try:
        return model.model_validate(body)
    except ValidationError:
        # Pydantic's usual validation details can include password input.
        raise HTTPException(422, "请检查账号和密码格式") from None


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _limit_public_attempt(request: Request, username: str) -> None:
    """Commit the reservation before Argon2, including unsuccessful attempts."""
    if not _is_secure(request):
        return
    database = getattr(request.app.state, "database", None)
    secret = getattr(request.app.state.settings, "public_proxy_secret", None)
    if database is None or secret is None:
        raise HTTPException(503, "认证服务暂不可用")
    try:
        with database.transaction() as session:
            allowed = consume_auth_attempt(session, username=username, secret=secret.get_secret_value())
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning("auth rate limit unavailable type=%s", type(exc).__name__)
        raise HTTPException(503, "认证服务暂不可用") from None
    if not allowed:
        raise HTTPException(429, "认证请求过于频繁，请稍后再试", headers={"Retry-After": "60"})


def _acquire_hash_slot(request: Request, svc: AuthService) -> None:
    """Bound simultaneous expensive password hashes across all API workers.

    Transaction-scoped advisory locks are released on commit, rollback, or DB
    connection loss; they never rely on client-provided IP addresses.
    """
    if not _is_secure(request):
        return
    for slot in range(2):
        if svc.session.scalar(text("SELECT pg_try_advisory_xact_lock(:namespace, :slot)"),
                              {"namespace": 1578566001, "slot": slot}):
            return
    raise HTTPException(429, "认证服务繁忙，请稍后再试", headers={"Retry-After": "5"})


@router.post("/register", status_code=201, response_model=UserRead)
def register(request: Request, response: Response, svc: Service, body: object = Body(...)):
    require_origin(request)
    _no_store(response)
    _release_for_auth(request, svc, registration=True)
    payload = _parse(RegisterInput, body)
    _limit_public_attempt(request, payload.username)
    _acquire_hash_slot(request, svc)
    return svc.register(payload, open_registration=getattr(request.app.state.settings, "auth_open_registration", False))


@router.post("/login", response_model=LoginRead)
def login(request: Request, response: Response, svc: Service, body: object = Body(...)):
    require_origin(request)
    _no_store(response)
    _release_for_auth(request, svc)
    payload = _parse(LoginInput, body)
    _limit_public_attempt(request, payload.username)
    _acquire_hash_slot(request, svc)
    issued = svc.login(payload)
    # The account id is produced by real password verification, never trusted
    # from the caller's username or a browser-supplied release/user header.
    _release_for_auth(request, svc, user_id=issued.user.id)
    response.set_cookie(key=_cookie_name(request), value=issued.token, max_age=svc.ttl_seconds,
                        path="/", secure=_is_secure(request), httponly=True, samesite="lax")
    return LoginRead(user=issued.user, csrf_token=csrf_token(issued.token))


@router.get("/me", response_model=LoginRead)
def me(response: Response, identity: Authenticated):
    _no_store(response)
    return LoginRead(user=UserRead(id=identity.principal.id, username=identity.principal.username,
                                   role=identity.principal.role), csrf_token=csrf_token(identity.token))


@router.post("/logout")
def logout(request: Request, response: Response, svc: Service, identity: CsrfProtected):
    _no_store(response)
    svc.logout(identity.login_session)
    response.delete_cookie(key=_cookie_name(request), path="/", secure=_is_secure(request), httponly=True, samesite="lax")
    return {"status": "ok"}
