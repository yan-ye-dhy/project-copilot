"""编译图状态与后端预算；客户端不能修改这些运行计数或来源。"""

from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from project_copilot.schemas.agent import AgentSource, ToolEvent


@dataclass(frozen=True)
class GraphLimits:
    max_model_calls: int = 4
    max_tool_calls: int = 6
    tool_timeout_seconds: float = 10.0
    model_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 120.0
    max_tool_result_chars: int = 24000
    max_context_chars: int = 60000
    max_tools_per_message: int = 12

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (
            self.max_model_calls, self.max_tool_calls, self.max_tool_result_chars,
            self.max_context_chars, self.max_tools_per_message,
        )):
            raise ValueError("执行预算必须是正整数")
        if any(type(value) not in (int, float) or not 0 < value <= 300 for value in (
            self.tool_timeout_seconds, self.model_timeout_seconds, self.total_timeout_seconds,
        )):
            raise ValueError("超时必须在0到300秒之间")


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    sources: list[AgentSource]
    events: list[ToolEvent]
    model_events: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    tool_executions: int
    used_call_ids: list[str]
    usage: dict[str, int] | None
    usage_complete: bool
    status: str
    answer: str
    failure_type: str | None
    started: float
