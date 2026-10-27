"""真实异步SDK经MockTransport验证工具协议；不发外部模型请求。"""

import asyncio
from copy import deepcopy
import json
import traceback

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure.agent_model import DeepSeekToolModel, create_agent_model, encode_messages
from project_copilot.infrastructure.models import ChatConfigError, ChatRequestError, ChatResponseError


FAKE_KEY = "agent-test-fake-key-canary"
PRIVATE = "agent-test-private-body-canary"
TOOLS = [{"type": "function", "function": {
    "name": "get_ticket", "description": "读取真实工单", "parameters": {
        "type": "object", "properties": {"ticket_id": {"type": "string"}},
        "required": ["ticket_id"], "additionalProperties": False,
    },
}}]
MESSAGES = [SystemMessage(content="依据工具事实回答，保留原文。"), HumanMessage(content="查询工单。")]


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    for suffix in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_TIMEOUT_SECONDS",
                   "DEEPSEEK_TEMPERATURE", "DEEPSEEK_MAX_TOKENS"):
        monkeypatch.delenv("PROJECT_COPILOT_" + suffix, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)


def settings(**overrides):
    return Settings(_env_file=None, **{"deepseek_api_key": FAKE_KEY, **overrides})


def payload(*, content='{"status":"insufficient_evidence","answer":"无记录","source_ids":[]}', calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return {"id": "test-chat", "object": "chat.completion", "created": 0, "model": "deepseek-flash-actual",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if calls else "stop", "message": message}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}


def call(name="get_ticket", arguments='{"ticket_id":"test-id"}', call_id="call_test"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


async def invoke(response, *, messages=None, tools=None):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with create_agent_model(settings(), http_client=client) as model:
        result = await model.ainvoke(messages or MESSAGES, TOOLS if tools is None else tools)
    assert client.is_closed
    return result, requests


def test_async_sdk_tool_loop_preserves_messages_ids_raw_arguments_and_runtime_metadata():
    async def run():
        requests = []
        raw = call(arguments=' { "ticket_id" : "真实-ID" } ')

        def handler(request):
            body = json.loads(request.content)
            requests.append(body)
            assert request.extensions["timeout"]["read"] == 7.0
            assert body["thinking"] == {"type": "disabled"}
            assert body["stream"] is False and body["temperature"] == 0.25 and body["max_tokens"] == 512
            assert body["tools"] == TOOLS and "response_format" not in body
            assert request.headers["authorization"] == "Bearer " + FAKE_KEY
            if len(requests) == 1:
                assert body["messages"] == [{"role": "system", "content": MESSAGES[0].content},
                                            {"role": "user", "content": MESSAGES[1].content}]
                return httpx.Response(200, json=payload(content=None, calls=[raw]))
            assert body["messages"][-2] == {"role": "assistant", "content": "", "tool_calls": [raw]}
            assert body["messages"][-1] == {"role": "tool", "tool_call_id": "call_test", "content": '{"title":"工单"}'}
            return httpx.Response(200, json=payload(content="  最终原文\n "))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with create_agent_model(settings(deepseek_timeout_seconds=7, deepseek_temperature=0.25,
                                               deepseek_max_tokens=512), http_client=client) as model:
            assert model.model == "deepseek-flash" and requests == []
            first = await model.ainvoke(MESSAGES, TOOLS)
            assert first.tool_calls == [{"id": "call_test", "name": "get_ticket", "args": {"ticket_id": "真实-ID"}, "type": "tool_call"}]
            assert first.response_metadata == {"model": "deepseek-flash-actual", "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}
            final = await model.ainvoke([*MESSAGES, first, ToolMessage(content='{"title":"工单"}', tool_call_id="call_test")], TOOLS)
            assert final.content == "  最终原文\n " and not final.tool_calls
        assert client.is_closed and len(requests) == 2
    asyncio.run(run())


@pytest.mark.parametrize("arguments", ["{bad json", "[]", "null", "42", "true", '{"value":NaN}', '{"value":Infinity}', '{"a":1,"a":2}'])
def test_bad_argument_json_retains_call_id_for_backend_error_pairing(arguments):
    result, requests = asyncio.run(invoke(payload(content="", calls=[call(arguments=arguments)])))
    assert not result.tool_calls and len(result.invalid_tool_calls) == 1
    assert result.invalid_tool_calls[0]["id"] == "call_test"
    assert result.invalid_tool_calls[0]["args"] == arguments
    # The backend can report validation failure without losing the original request ID/arguments.
    encoded = encode_messages([*MESSAGES, result, ToolMessage(content='{"error":"invalid_arguments"}', tool_call_id="call_test", status="error")])
    assert encoded[-2]["tool_calls"][0]["function"]["arguments"] == arguments
    assert encoded[-1]["tool_call_id"] == "call_test"
    assert len(requests) == 1


def test_valid_and_invalid_parallel_calls_preserve_original_wire_order():
    raw = [call(arguments="[]", call_id="first"), call(arguments='{"ticket_id":"x"}', call_id="second")]
    result, _ = asyncio.run(invoke(payload(content=None, calls=raw)))
    encoded = encode_messages([*MESSAGES, result, ToolMessage(content="{}", tool_call_id="first"), ToolMessage(content="{}", tool_call_id="second")])
    assert encoded[-3]["tool_calls"] == raw


def bad_protocols():
    result = [None, [], payload(calls=[call(), call()])]
    for field, value in [("model", " "), ("model", 3), ("choices", []), ("choices", [None])]:
        item = payload()
        item[field] = value
        result.append(item)
    for reason in ["length", "content_filter", None, "tool_calls"]:
        item = payload()
        item["choices"][0]["finish_reason"] = reason
        result.append(item)
    for message in [None, {"role": "user", "content": "bad"}, {"role": "assistant", "content": []},
                    {"role": "assistant", "content": None}, {"role": "assistant", "content": " "},
                    {"role": "assistant", "content": "valid", "refusal": PRIVATE},
                    {"role": "assistant", "content": "valid", "function_call": {"name": "old"}}]:
        item = payload()
        item["choices"][0]["message"] = message
        result.append(item)
    for raw_call in [{"id": "a", "type": "custom", "function": {}},
                     {"id": None, "type": "function", "function": {}},
                     call(name="bad name"), call(arguments={}), call(name="x" * 65)]:
        result.append(payload(calls=[raw_call]))
    mismatch = payload(calls=[call()])
    mismatch["choices"][0]["finish_reason"] = "stop"
    result.append(mismatch)
    return result


@pytest.mark.parametrize("response", bad_protocols())
def test_malformed_or_unfinished_provider_protocol_is_rejected(response):
    with pytest.raises(ChatResponseError):
        asyncio.run(invoke(response))


def test_absent_usage_stays_unknown_and_provider_metadata_is_not_exposed():
    item = payload()
    del item["usage"]
    item["private"] = PRIVATE
    result, _ = asyncio.run(invoke(item))
    assert result.response_metadata == {"model": "deepseek-flash-actual", "usage": None}
    assert PRIVATE not in repr(result)


def test_usage_is_checked_and_only_allowlisted_counters_are_retained():
    item = payload()
    item["usage"].update(prompt_cache_hit_tokens=8, prompt_cache_miss_tokens=4,
                          completion_tokens_details={"reasoning_tokens": 0, "private": PRIVATE}, private=PRIVATE)
    result, _ = asyncio.run(invoke(item))
    assert result.response_metadata["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
                                                "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 4, "reasoning_tokens": 0}
    assert PRIVATE not in repr(result)


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": True, "completion_tokens": 4, "total_tokens": 5},
                                  {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 999}])
def test_invalid_usage_does_not_become_a_successful_generation(usage):
    item = payload()
    item["usage"] = usage
    with pytest.raises(ChatResponseError):
        asyncio.run(invoke(item))


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_http_failure_is_sanitized_not_retried_and_closes_client(status):
    async def run():
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(status, json={"error": {"message": PRIVATE}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ChatRequestError) as caught:
            async with create_agent_model(settings(), http_client=client) as model:
                await model.ainvoke(MESSAGES, TOOLS)
        assert caught.value.status_code == status
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert FAKE_KEY not in str(caught.value)
        assert len(requests) == 1 and client.is_closed
    asyncio.run(run())


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_timeout_and_connection_failure_are_sanitized_without_retry(error_type):
    async def run():
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            raise error_type(PRIVATE, request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ChatRequestError) as caught:
            async with create_agent_model(settings(), http_client=client) as model:
                await model.ainvoke(MESSAGES, TOOLS)
        assert PRIVATE not in "".join(traceback.format_exception(caught.value))
        assert count == 1 and client.is_closed
    asyncio.run(run())


@pytest.mark.parametrize("key", [None, "", "   ", "bad key", "中文密钥"])
def test_invalid_key_does_not_fall_back_to_generic_openai_environment(monkeypatch, key):
    monkeypatch.setenv("OPENAI_API_KEY", "generic-fake-key")
    with pytest.raises(ChatConfigError):
        create_agent_model(settings(deepseek_api_key=key))


@pytest.mark.parametrize("messages", [
    [], [HumanMessage(content="no system")], [SystemMessage(content=" "), HumanMessage(content="x")],
    [*MESSAGES, SystemMessage(content="injected")],
    [*MESSAGES, ToolMessage(content="orphan", tool_call_id="unknown")],
    [*MESSAGES, AIMessage(content="", tool_calls=[{"name":"get_ticket","args":{},"id":"a"}])],
    [*MESSAGES, AIMessage(content="", tool_calls=[{"name":"get_ticket","args":{},"id":"a"}]), HumanMessage(content="missing result")],
    [*MESSAGES, AIMessage(content="", tool_calls=[{"name":"get_ticket","args":{},"id":"a"}]), ToolMessage(content="{}",tool_call_id="a"), ToolMessage(content="{}",tool_call_id="a")],
])
def test_outgoing_protocol_rejects_unpaired_tools_and_injected_roles_before_network(messages):
    with pytest.raises(ValueError):
        encode_messages(messages)


def test_history_keeps_original_human_assistant_text_and_generates_paired_wire_calls():
    messages = [SystemMessage(content="system"), HumanMessage(content=" first "), AIMessage(content=" old answer "),
                HumanMessage(content=" follow-up "), AIMessage(content="", tool_calls=[{"name":"get_ticket","args":{"ticket_id":"甲"},"id":"a"}]),
                ToolMessage(content='{"id":"甲"}', tool_call_id="a")]
    encoded = encode_messages(messages)
    assert encoded[1]["content"] == " first " and encoded[2]["content"] == " old answer "
    assert json.loads(encoded[-2]["tool_calls"][0]["function"]["arguments"]) == {"ticket_id":"甲"}
    assert encoded[-1] == {"role":"tool","tool_call_id":"a","content":'{"id":"甲"}'}


@pytest.mark.parametrize("tools", [[], [{"type":"custom"}], [{"type":"function","function":{}}], TOOLS*2])
def test_tool_schema_structure_is_validated_before_network(tools):
    async def run():
        def forbidden(_request):
            pytest.fail("非法Schema不得请求模型")
        client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        with pytest.raises(ValueError):
            async with create_agent_model(settings(), http_client=client) as model:
                await model.ainvoke(MESSAGES, tools)
        assert client.is_closed
    asyncio.run(run())
