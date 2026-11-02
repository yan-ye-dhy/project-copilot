"""Argon2id accounts and revocable session capabilities within a caller-owned transaction."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import hmac
import secrets
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from project_copilot.db.models import LoginSession, RegistrationInvite, UserAccount
from project_copilot.schemas.auth import LoginInput, RegisterInput, UserRead


class AuthenticationRejected(PermissionError):
    pass


class RegistrationRejected(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    id: UUID
    username: str
    role: str

    @classmethod
    def from_user(cls, user: UserAccount) -> "Principal":
        return cls(id=user.id, username=user.username, role=user.role)


@dataclass(frozen=True)
class IssuedSession:
    user: UserRead
    token: str
    expires_at: datetime


# Explicit Argon2id configuration; hashes contain their own algorithm and parameters.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16, type=Type.ID)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # Equalize the expensive password verification path for unknown users.
    return _hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _digest(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and len(token) == 43 and token.isascii() and all(
        c.isalnum() or c in "-_" for c in token
    )


def csrf_token(token: str) -> str:
    if not _valid_token(token):
        raise AuthenticationRejected()
    # The HttpOnly random token is a secret MAC key; only the derived value reaches JS.
    return hmac.new(token.encode("ascii"), b"project-copilot-csrf-v1", "sha256").hexdigest()


def csrf_matches(token: str, supplied: object) -> bool:
    if not _valid_token(token) or not isinstance(supplied, str) or len(supplied) != 64 or not supplied.isascii():
        return False
    return hmac.compare_digest(csrf_token(token), supplied)


class AuthService:
    def __init__(self, session: Session, *, ttl_seconds: int = 86400):
        if type(ttl_seconds) is not int or not 300 <= ttl_seconds <= 30 * 86400:
            raise ValueError("invalid session lifetime")
        self.session, self.ttl_seconds = session, ttl_seconds

    def now(self) -> datetime:
        return self.session.scalar(select(func.clock_timestamp()))

    def register(self, payload: RegisterInput, *, open_registration: bool = False) -> UserRead:
        invite_token = payload.invitation.get_secret_value() if payload.invitation is not None else None
        invitation = None
        if invite_token is not None:
            if not _valid_token(invite_token):
                raise RegistrationRejected()
            self.session.execute(text("SET LOCAL lock_timeout = '5s'"))
            invitation = self.session.scalar(
                select(RegistrationInvite).where(RegistrationInvite.token_hash == _digest(invite_token)).with_for_update()
            )
            now = self.now()
            if not invitation or invitation.revoked_at is not None or invitation.expires_at <= now or invitation.uses >= invitation.max_uses:
                raise RegistrationRejected()
        elif not open_registration:
            raise RegistrationRejected()
        if self.session.scalar(select(UserAccount.id).where(UserAccount.username == payload.username)) is not None:
            raise RegistrationRejected()
        user = UserAccount(username=payload.username, password_hash=hash_password(payload.password.get_secret_value()), role="user")
        self.session.add(user)
        if invitation is not None:
            invitation.uses += 1
        self.session.flush()
        return UserRead.model_validate(user)

    def login(self, payload: LoginInput) -> IssuedSession:
        user = self.session.scalar(select(UserAccount).where(UserAccount.username == payload.username))
        # Resolve the dummy hash consistently; callers receive the same error for both cases.
        dummy = _dummy_hash()
        verified = verify_password(user.password_hash if user else dummy, payload.password.get_secret_value())
        if not user or not user.active or not verified:
            raise AuthenticationRejected()
        if _hasher.check_needs_rehash(user.password_hash):
            user.password_hash = hash_password(payload.password.get_secret_value())
        token = secrets.token_urlsafe(32)
        expiry = self.now() + timedelta(seconds=self.ttl_seconds)
        self.session.add(LoginSession(user_id=user.id, token_hash=_digest(token), expires_at=expiry))
        self.session.flush()
        return IssuedSession(user=UserRead.model_validate(user), token=token, expires_at=expiry)

    def authenticate(self, token: object, *, for_update: bool = False) -> tuple[Principal, LoginSession]:
        if not _valid_token(token):
            raise AuthenticationRejected()
        return self._authenticate_where(LoginSession.token_hash == _digest(token), for_update=for_update)

    def authenticate_session_id(self, session_id: UUID, *, for_update: bool = False) -> tuple[Principal, LoginSession]:
        """Recheck a server-verified session reference in a durable background job.

        Never use a caller-supplied session ID as a substitute for a cookie; only
        server code that already authenticated the job may call this method.
        """
        if not isinstance(session_id, UUID):
            raise AuthenticationRejected()
        return self._authenticate_where(LoginSession.id == session_id, for_update=for_update)

    def _authenticate_where(self, condition, *, for_update: bool) -> tuple[Principal, LoginSession]:
        statement = select(LoginSession).where(condition)
        if for_update:
            # Refresh any earlier identity-map snapshot after acquiring the row lock.
            statement = statement.with_for_update().execution_options(populate_existing=True)
        session = self.session.scalar(statement)
        if session and for_update:
            # Sensitive writes keep the current user active/role stable until commit.
            user = self.session.scalar(select(UserAccount).where(UserAccount.id == session.user_id)
                                       .with_for_update(read=True).execution_options(populate_existing=True))
        else:
            user = self.session.get(UserAccount, session.user_id) if session else None
        # Obtain the clock after any lock wait so expired sessions fail closed.
        now = self.now()
        if not session or session.revoked_at is not None or session.expires_at <= now or not user or not user.active:
            raise AuthenticationRejected()
        return Principal.from_user(user), session

    def logout(self, session: LoginSession) -> None:
        session.revoked_at = self.now()
        self.session.flush()

    def bootstrap_admin(self, payload: RegisterInput) -> UserRead:
        # Serialize competing CLI initializations in PostgreSQL, even when no row exists yet.
        self.session.execute(text("SELECT pg_advisory_xact_lock(576572591121)"))
        if self.session.scalar(select(UserAccount.id).where(UserAccount.role == "admin")) is not None:
            raise RegistrationRejected()
        user = UserAccount(username=payload.username, password_hash=hash_password(payload.password.get_secret_value()), role="admin")
        self.session.add(user)
        self.session.flush()
        return UserRead.model_validate(user)

    def _verify_admin(self, admin_username: str, admin_password: str) -> UserAccount:
        admin = self.session.scalar(select(UserAccount).where(
            UserAccount.username == admin_username, UserAccount.role == "admin", UserAccount.active.is_(True)
        ).with_for_update(read=True).execution_options(populate_existing=True))
        dummy = _dummy_hash()
        valid = verify_password(admin.password_hash if admin else dummy, admin_password)
        if admin is None or not valid:
            raise RegistrationRejected()
        return admin

    def issue_invitation(self, admin_username: str, admin_password: str, *, ttl_seconds: int = 86400, max_uses: int = 1) -> str:
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 30 * 86400 or type(max_uses) is not int or not 1 <= max_uses <= 100:
            raise ValueError("invalid invitation limits")
        admin = self._verify_admin(admin_username, admin_password)
        token = secrets.token_urlsafe(32)
        self.session.add(RegistrationInvite(token_hash=_digest(token), created_by=admin.id, expires_at=self.now() + timedelta(seconds=ttl_seconds), max_uses=max_uses))
        self.session.flush()
        return token

    def set_role(self, admin_username: str, admin_password: str, target_username: str, role: str) -> UserRead:
        if role not in {"user", "admin"}:
            raise ValueError("invalid role")
        # Use the same lock as bootstrap so competing demotions cannot remove every admin.
        self.session.execute(text("SELECT pg_advisory_xact_lock(576572591121)"))
        self._verify_admin(admin_username, admin_password)
        target = self.session.scalar(select(UserAccount).where(UserAccount.username == target_username).with_for_update())
        if target is None:
            raise RegistrationRejected()
        if target.role == "admin" and target.active and role != "admin":
            active_admins = self.session.scalar(select(func.count()).select_from(UserAccount).where(
                UserAccount.role == "admin", UserAccount.active.is_(True)))
            if active_admins <= 1:
                raise RegistrationRejected()
        target.role = role
        self.session.flush()
        return UserRead.model_validate(target)
