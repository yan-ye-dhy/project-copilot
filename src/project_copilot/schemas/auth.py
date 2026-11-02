"""Public account DTOs. Password and bearer tokens are never response fields."""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class _Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-z0-9][a-z0-9_.-]{2,31}$")
    password: SecretStr

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw.encode("utf-8")) > 1024:
            raise ValueError("密码过长")
        return value


class RegisterInput(_Credentials):
    invitation: SecretStr | None = None

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 12 or not raw.strip():
            raise ValueError("密码至少需要12个字符")
        return value

    @field_validator("invitation")
    @classmethod
    def invitation_length(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) > 128:
            raise ValueError("邀请码无效")
        return value


class LoginInput(_Credentials):
    pass


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    username: str
    role: Literal["user", "admin"]


class LoginRead(BaseModel):
    user: UserRead
    csrf_token: str
