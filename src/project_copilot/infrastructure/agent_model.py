"""独立Agent工具调用适配；保留D5 RAG的两消息JSON契约。"""

from collections.abc import Sequence
import json
import re
from typing import Self

import httpx
from project_copilot.infrastructure.execution_tracking import async_provider_request
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from openai import (
    APIConnectionError, APIResponseValidationError, APIStatusError, APITimeoutError, AsyncOpenAI,
)

from project_copilot.config import Settings
from project_copilot.infrastructure.models import (
    ChatConfigError, ChatRequestError, ChatResponseError, DeepSeekChat,
)
from project_copilot.services.quotas import QuotaExceeded


_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_REQUEST_FAILURES = frozenset({
    "model_auth_error", "model_rate_limit", "model_provider_error", "model_request_rejected",
    "model_timeout", "model_connection_error", "model_input_invalid",
})
_RESPONSE_FAILURES = frozenset({"model_response_invalid", "model_output_limit", "model_tool_protocol"})
AGENT_MODEL_FAILURE_TYPES = _REQUEST_FAILURES | _RESPONSE_FAILURES


class AgentModelRequestError(ChatRequestError):
    """Agent-only safe category; no provider text, body, headers or arguments."""

    def __init__(self, failure_type: str, *, status_code: int | None = None) -> None:
        if not isinstance(failure_type, str) or failure_type not in _REQUEST_FAILURES:
            raise ValueError("Invalid Agent request failure category")
        if status_code is not None and (type(status_code) is not int or not 100 <= status_code <= 599):
            raise ValueError("Invalid Agent HTTP status")
        super().__init__(failure_type, status_code=status_code)
        self.failure_type = failure_type


class AgentModelResponseError(ChatResponseError):
    """Agent-only fixed category for a response rejected by existing contracts."""

    def __init__(self, failure_type: str) -> None:
        if not isinstance(failure_type, str) or failure_type not in _RESPONSE_FAILURES:
            raise ValueError("Invalid Agent response failure category")
        super().__init__(failure_type)
        self.failure_type = failure_type


def _reject_constant(_value: str) -> None:
    raise ValueError("非有限JSON数值")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("重复JSON字段")
        result[key] = value
    return result


def _parse_calls(value: object) -> tuple[list[dict], list[dict], list[dict]]:
    """协议不完整则拒绝；有ID的坏参数保留，供执行器配对返回校验错误。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise AgentModelResponseError("model_tool_protocol")
    valid, invalid, raw = [], [], []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise AgentModelResponseError("model_tool_protocol")
        call_id = item.get("id")
        function = item.get("function")
        if (not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 256
                or call_id in seen or not isinstance(function, dict)):
            raise AgentModelResponseError("model_tool_protocol")
        seen.add(call_id)
        name, arguments = function.get("name"), function.get("arguments")
        if (not isinstance(name, str) or not _TOOL_NAME.fullmatch(name)
                or not isinstance(arguments, str) or len(arguments) > 20000):
            raise AgentModelResponseError("model_tool_protocol")
        raw.append({"id": call_id, "type": "function",
                    "function": {"name": name, "arguments": arguments}})
        try:
            args = json.loads(arguments, parse_constant=_reject_constant, object_pairs_hook=_unique_object)
            if not isinstance(args, dict):
                raise ValueError("工具参数须为对象")
        except (ValueError, TypeError, RecursionError):
            invalid.append({"id": call_id, "name": name, "args": arguments,
                            "error": "工具参数不是合法JSON对象", "type": "invalid_tool_call"})
        else:
            valid.append({"id": call_id, "name": name, "args": args, "type": "tool_call"})
    return valid, invalid, raw


def encode_messages(messages: Sequence[BaseMessage]) -> list[dict]:
    """把LangChain消息编码为供应商协议，校验每轮请求/结果ID的一一对应。"""
    if (not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages
            or not isinstance(messages[0], SystemMessage)):
        raise ValueError("Agent消息必须以System消息开始")
    encoded, pending, seen = [], set(), set()
    for index, message in enumerate(messages):
        if not isinstance(message, BaseMessage) or not isinstance(message.content, str):
            raise ValueError("Agent只支持文本消息")
        if pending and not isinstance(message, ToolMessage):
            raise ValueError("必须先回传当前轮全部工具结果")
        if isinstance(message, SystemMessage):
            if index != 0 or not message.content.strip():
                raise ValueError("Agent只允许首条System消息")
            encoded.append({"role": "system", "content": message.content})
        elif isinstance(message, HumanMessage):
            if not message.content.strip():
                raise ValueError("用户消息不能为空")
            encoded.append({"role": "user", "content": message.content})
        elif isinstance(message, AIMessage):
            calls = message.additional_kwargs.get("tool_calls")
            if calls is None:
                calls = [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["args"], ensure_ascii=False, allow_nan=False),
                    }} for call in message.tool_calls
                ]
                calls.extend({"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": call["args"],
                }} for call in message.invalid_tool_calls)
            item = {"role": "assistant", "content": message.content}
            if calls:
                try:
                    _, _, raw = _parse_calls(calls)
                except ChatResponseError:
                    raise ValueError("历史工具调用协议不合法") from None
                ids = {call["id"] for call in raw}
                if ids & seen:
                    raise ValueError("历史工具调用ID重复")
                pending.update(ids)
                seen.update(ids)
                item["tool_calls"] = raw
            elif not message.content.strip():
                raise ValueError("Assistant消息不能为空")
            encoded.append(item)
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError("工具结果缺少对应请求或被重复回传")
            pending.remove(message.tool_call_id)
            encoded.append({"role": "tool", "tool_call_id": message.tool_call_id,
                            "content": message.content})
        else:
            raise ValueError("不支持的Agent消息角色")
    if pending or not isinstance(messages[-1], (HumanMessage, ToolMessage)):
        raise ValueError("模型请求前必须有用户输入或完整工具结果")
    return encoded


def _validate_tools(tools: list[dict]) -> list[dict]:
    if not isinstance(tools, list) or not 1 <= len(tools) <= 16:
        raise ValueError("Agent工具Schema列表不合法")
    seen = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise ValueError("Agent工具Schema须为function")
        function = tool.get("function")
        if not isinstance(function, dict):
            raise ValueError("Agent工具Schema缺少function")
        name = function.get("name")
        if (not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in seen
                or not isinstance(function.get("parameters"), dict)
                or function["parameters"].get("type") != "object"):
            raise ValueError("Agent工具Schema名称或参数不合法")
        seen.add(name)
    return tools


class DeepSeekToolModel:
    """每次一次异步请求；模型只提出调用，实际权限/执行由后端图控制。"""

    def __init__(self, client: AsyncOpenAI, *, model: str, temperature: float, max_tokens: int) -> None:
        self._client = client
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def ainvoke(self, messages: Sequence[BaseMessage], tools: list[dict]) -> AIMessage:
        try:
            encoded = encode_messages(messages)
            tools = _validate_tools(tools)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise AgentModelRequestError("model_input_invalid") from None
        try:
            async with async_provider_request() as attempt:
                raw_response = await self._client.chat.completions.with_raw_response.create(
                    model=self.model, messages=encoded, tools=tools,
                    temperature=self.temperature, max_tokens=self.max_tokens, stream=False,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                attempt.completed()
                response = raw_response.http_response.json()
        except QuotaExceeded:
            # Admission rejects before HTTP. Do not label our own quota as a
            # malformed provider response (QuotaExceeded is a ValueError).
            raise
        except APITimeoutError:
            raise AgentModelRequestError("model_timeout") from None
        except APIConnectionError:
            raise AgentModelRequestError("model_connection_error") from None
        except APIStatusError as exc:
            status = exc.status_code
            failure_type = ("model_auth_error" if status in (401, 403) else
                            "model_rate_limit" if status == 429 else
                            "model_provider_error" if status >= 500 else
                            "model_request_rejected")
            raise AgentModelRequestError(failure_type, status_code=status) from None
        except (APIResponseValidationError, ValueError, TypeError):
            raise AgentModelResponseError("model_response_invalid") from None
        try:
            return self._validate_response(response)
        except AgentModelResponseError:
            raise
        except (ChatResponseError, ValueError, TypeError):
            raise AgentModelResponseError("model_response_invalid") from None

    @staticmethod
    def _validate_response(response: object) -> AIMessage:
        if not isinstance(response, dict):
            raise AgentModelResponseError("model_response_invalid")
        model, choices = response.get("model"), response.get("choices")
        if (not isinstance(model, str) or not model.strip() or not isinstance(choices, list)
                or len(choices) != 1 or not isinstance(choices[0], dict)):
            raise AgentModelResponseError("model_response_invalid")
        choice = choices[0]
        message, reason = choice.get("message"), choice.get("finish_reason")
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or message.get("refusal") is not None):
            raise AgentModelResponseError("model_response_invalid")
        if message.get("function_call") is not None:
            raise AgentModelResponseError("model_tool_protocol")
        content, calls = message.get("content"), message.get("tool_calls")
        if content is not None and not isinstance(content, str):
            raise AgentModelResponseError("model_response_invalid")
        if isinstance(content, str) and len(content) > 64000:
            raise AgentModelResponseError("model_response_invalid")
        if reason == "length":
            raise AgentModelResponseError("model_output_limit")
        usage = DeepSeekChat._validate_usage(response["usage"]) if "usage" in response else None
        metadata = {"model": model, "usage": usage}
        if reason == "tool_calls":
            valid, invalid, raw = _parse_calls(calls)
            return AIMessage(content=content or "", tool_calls=valid, invalid_tool_calls=invalid,
                             additional_kwargs={"tool_calls": raw}, response_metadata=metadata)
        if calls is not None and calls != []:
            raise AgentModelResponseError("model_tool_protocol")
        if reason != "stop" or not content or not content.strip():
            raise AgentModelResponseError("model_response_invalid")
        return AIMessage(content=content, response_metadata=metadata)

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def create_agent_model(settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> DeepSeekToolModel:
    """复用新项目独立配置；无key回退、隐式请求、自动重试或工具执行。"""
    key = settings.deepseek_api_key.get_secret_value().strip() if settings.deepseek_api_key else ""
    if not key or not key.isascii() or any(character.isspace() for character in key):
        raise ChatConfigError("请在新项目配置有效的PROJECT_COPILOT_DEEPSEEK_API_KEY")
    client = AsyncOpenAI(api_key=key, base_url=str(settings.deepseek_base_url),
                         timeout=settings.deepseek_timeout_seconds, max_retries=0, http_client=http_client)
    return DeepSeekToolModel(client, model=settings.deepseek_model, temperature=settings.deepseek_temperature,
                             max_tokens=settings.deepseek_max_tokens)
