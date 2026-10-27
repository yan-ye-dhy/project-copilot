"""Agent的HTTP输入、模型最终合同与后端可审计结果；不接受客户端工具消息。"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def valid_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("文本不能包含NUL字符")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("文本必须是有效UTF-8") from None
    return value


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    content: str = Field(strict=True, min_length=1, max_length=4000)

    @field_validator("content")
    @classmethod
    def check_text(cls, value: str) -> str:
        return valid_text(value)


class AgentQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    question: str = Field(strict=True, min_length=1, max_length=2000)
    history: list[HistoryMessage] = Field(default_factory=list, max_length=8)

    @field_validator("question")
    @classmethod
    def check_text(cls, value: str) -> str:
        return valid_text(value)

    @model_validator(mode="after")
    def bound_history(self) -> Self:
        if sum(len(message.content) for message in self.history) > 10000:
            raise ValueError("历史输入总长度超过10000字符")
        return self


class AgentFinal(BaseModel):
    """模型只选择本轮已见source ID；真实工单/引用由后端返回。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)

    status: Literal["answered", "insufficient_evidence"]
    answer: str = Field(strict=True, min_length=1, max_length=4000)
    source_ids: list[str] = Field(max_length=20)

    @field_validator("answer")
    @classmethod
    def check_text(cls, value: str) -> str:
        return valid_text(value)

    @model_validator(mode="after")
    def check_sources(self) -> Self:
        if any(not source or len(source) > 100 for source in self.source_ids):
            raise ValueError("来源ID非法")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("来源ID不能重复")
        if self.status == "answered" and not self.source_ids:
            raise ValueError("回答必须选择本轮真实来源")
        if self.status == "insufficient_evidence" and self.source_ids:
            raise ValueError("证据不足时不能附带来源")
        return self


class AgentSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(strict=True, min_length=1, max_length=100)
    kind: Literal["ticket", "knowledge"]
    data: dict[str, Any]

    @model_validator(mode="after")
    def check_identity(self) -> Self:
        if not self.id.startswith(self.kind + ":") or not self.id[len(self.kind) + 1:]:
            raise ValueError("来源类型与ID不匹配")
        return self


class ToolEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_call_id: str
    name: str
    status: Literal["ok", "error"]
    failure_type: str | None = None
    elapsed_ms: float = Field(ge=0)


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: Literal["answered", "insufficient_evidence", "failed", "budget_exhausted"]
    answer: str
    sources: list[AgentSource]
    events: list[ToolEvent]
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    tool_executions: int = Field(ge=0)
    usage: dict[str, int] | None
    model_events: list[dict[str, Any]]
    elapsed_ms: float = Field(ge=0)
    failure_type: str | None = None
    trace_exported: Literal[False] = False
