"""Real SDK, controlled transport and completion tracking; no providers or DB."""
import asyncio
import logging
import traceback
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from project_copilot.config import Settings
from project_copilot.infrastructure import call_accounting, execution_tracking
from project_copilot.infrastructure.agent_model import (
    AGENT_MODEL_FAILURE_TYPES, AgentModelRequestError, AgentModelResponseError, create_agent_model,
)
from project_copilot.infrastructure.models import ChatRequestError, ChatResponseError
from project_copilot.services import execution_slots
from test_agent_model import TOOLS, payload, call

CANARY = "agent-failure-private-body-and-arguments-canary"
KEY = "agent-failure-fake-key-canary"
MESSAGES = [SystemMessage(content="synthetic system"), HumanMessage(content=CANARY)]


@pytest.fixture
def completed_attempts(monkeypatch):
    state = {"started": 0, "completed": []}
    def started():
        state["started"] += 1
        return True
    monkeypatch.setattr(call_accounting, "record_provider_attempt", started)
    tracker = SimpleNamespace(database=object(), job_id="synthetic-job", token="synthetic-token")
    monkeypatch.setattr(execution_tracking, "current_execution", lambda: tracker)
    monkeypatch.setattr(execution_slots, "provider_finished", lambda *args, completed: state["completed"].append(completed))
    return state


async def capture(handler, *, messages=MESSAGES, tools=TOOLS):
    calls = []
    def transport(request):
        calls.append(request)
        return handler(request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
    settings = Settings(_env_file=None, deepseek_api_key=KEY,
                        deepseek_base_url="https://provider.invalid/v1", deepseek_model="deepseek-flash")
    try:
        async with create_agent_model(settings, http_client=client) as model:
            await model.ainvoke(messages, tools)
    except (AgentModelRequestError, AgentModelResponseError) as error:
        assert client.is_closed
        return error, len(calls)
    pytest.fail("Invalid response must remain rejected")


def private_safe(error, caplog):
    rendered = "".join(traceback.format_exception(error)) + repr(error) + repr(vars(error)) + caplog.text
    assert CANARY not in rendered and KEY not in rendered
    assert error.failure_type in AGENT_MODEL_FAILURE_TYPES
    assert not hasattr(error, "response") and not hasattr(error, "body") and not hasattr(error, "request")


@pytest.mark.parametrize("status,category", [
    (400, "model_request_rejected"), (401, "model_auth_error"), (403, "model_auth_error"),
    (422, "model_request_rejected"), (429, "model_rate_limit"), (500, "model_provider_error"),
    (502, "model_provider_error"), (503, "model_provider_error"),
])
def test_http_categories_keep_numeric_status_once_and_complete_rejection(status, category, completed_attempts, caplog):
    caplog.set_level(logging.INFO)
    error, calls = asyncio.run(capture(lambda _: httpx.Response(status, json={"error": {"message": CANARY}})))
    assert isinstance(error, ChatRequestError)
    assert error.failure_type == category and error.status_code == status
    assert calls == 1 and completed_attempts == {"started": 1, "completed": [True]}
    private_safe(error, caplog)


@pytest.mark.parametrize("error_type,category", [
    (httpx.ReadTimeout, "model_timeout"), (httpx.ConnectTimeout, "model_timeout"),
    (httpx.ConnectError, "model_connection_error"), (httpx.ReadError, "model_connection_error"),
])
def test_transport_categories_do_not_claim_provider_completion(error_type, category, completed_attempts, caplog):
    def handler(request):
        raise error_type(CANARY, request=request)
    error, calls = asyncio.run(capture(handler))
    assert isinstance(error, ChatRequestError) and error.failure_type == category
    assert error.status_code is None
    assert calls == 1 and completed_attempts == {"started": 1, "completed": [False]}
    private_safe(error, caplog)


def bad_responses():
    cases = [(None, "model_response_invalid")]
    invalid_usage = payload(content=CANARY)
    invalid_usage["usage"]["total_tokens"] = 999
    cases.append((invalid_usage, "model_response_invalid"))
    limited = payload(content=CANARY)
    limited["choices"][0]["finish_reason"] = "length"
    cases.append((limited, "model_output_limit"))
    malformed = payload(content=CANARY)
    malformed["choices"][0]["message"]["role"] = CANARY
    cases.append((malformed, "model_response_invalid"))
    for raw in [call(call_id=""), call(name=CANARY + " invalid name"),
                {"type": "custom", "id": "call_test", "function": {"arguments": CANARY}},
                call(arguments=123), call(arguments=CANARY, call_id=None)]:
        cases.append((payload(calls=[raw]), "model_tool_protocol"))
    cases.append((payload(calls=[call(), call()]), "model_tool_protocol"))
    mismatch = payload(calls=[call(arguments=CANARY)])
    mismatch["choices"][0]["finish_reason"] = "stop"
    cases.append((mismatch, "model_tool_protocol"))
    missing = payload(content=CANARY)
    missing["choices"][0]["finish_reason"] = "tool_calls"
    cases.append((missing, "model_tool_protocol"))
    return cases


@pytest.mark.parametrize("body,category", bad_responses())
def test_protocol_failure_stays_rejected_after_completed_http(body, category, completed_attempts, caplog):
    error, calls = asyncio.run(capture(lambda _: httpx.Response(200, json=body)))
    assert isinstance(error, ChatResponseError) and error.failure_type == category
    assert calls == 1 and completed_attempts == {"started": 1, "completed": [True]}
    private_safe(error, caplog)


def test_non_json_response_is_invalid_but_complete_http(completed_attempts, caplog):
    error, calls = asyncio.run(capture(lambda _: httpx.Response(200, content=CANARY)))
    assert error.failure_type == "model_response_invalid"
    assert calls == 1 and completed_attempts == {"started": 1, "completed": [True]}
    private_safe(error, caplog)


@pytest.mark.parametrize("messages,tools", [
    ([HumanMessage(content=CANARY)], TOOLS),
    ([*MESSAGES, ToolMessage(content=CANARY, tool_call_id="orphan")], TOOLS),
    (MESSAGES, [{"type": "function", "function": {"name": CANARY + " invalid", "parameters": {}}}]),
])
def test_input_validation_precedes_provider_accounting_and_network(messages, tools, completed_attempts, caplog):
    def forbidden(_):
        pytest.fail("Invalid model input must never reach transport")
    error, calls = asyncio.run(capture(forbidden, messages=messages, tools=tools))
    assert isinstance(error, ChatRequestError) and error.failure_type == "model_input_invalid"
    assert calls == 0 and completed_attempts == {"started": 0, "completed": []}
    private_safe(error, caplog)


@pytest.mark.parametrize("error_class,bad_code", [
    (AgentModelRequestError, CANARY), (AgentModelResponseError, CANARY),
    (AgentModelRequestError, "model_response_invalid"), (AgentModelResponseError, "model_auth_error"),
    (AgentModelRequestError, []), (AgentModelResponseError, None),
])
def test_error_constructors_accept_only_fixed_categories(error_class, bad_code):
    with pytest.raises(ValueError) as caught:
        error_class(bad_code)
    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("status", [True, False, 99, 600, "401", CANARY])
def test_http_status_cannot_hold_untrusted_text_or_boolean(status):
    with pytest.raises(ValueError) as caught:
        AgentModelRequestError("model_auth_error", status_code=status)
    assert CANARY not in str(caught.value)
