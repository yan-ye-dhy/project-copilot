"""HTTP工单输入/输出契约；ORM与数据库表的职责在db/models及迁移中。"""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


def _postgres_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("工单文本不能包含NUL字符")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("工单文本必须是有效UTF-8") from None
    return value


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class TicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    title: str = Field(strict=True, min_length=1, max_length=200)
    description: str = Field(strict=True, min_length=1, max_length=10000)

    @field_validator("title", "description")
    @classmethod
    def validate_postgres_text(cls, value: str) -> str:
        return _postgres_text(value)


class TicketFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    status: TicketStatus | None = None
    q: str | None = Field(default=None, strict=True, min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)

    @field_validator("q")
    @classmethod
    def validate_postgres_query(cls, value: str | None) -> str | None:
        return None if value is None else _postgres_text(value)


class TicketRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True, extra="forbid")

    id: UUID
    title: str
    description: str
    status: TicketStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime


class TicketList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[TicketRead, ...]
    limit: int
    offset: int
