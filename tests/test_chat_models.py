"""D5离线契约：真实OpenAI SDK通过MockTransport验证DeepSeek请求与原始响应。"""

import json
import traceback
from collections.abc import Callable
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from project_copilot.config import Settings
from project_copilot.infrastructure.models import (
    ChatConfigError,
    ChatGeneration,
    ChatRequestError,
    ChatResponseError,
    DeepSeekChat,
    create_chat_client,
)


FAKE_KEY = "d5-fake-key-canary-do-not-log"
PRIVATE_BODY = "d5-private-body-canary-do-not-log"
MESSAGES = [
    {"role": "system", "content": "  依据证据用json回答。\n保留角色。  "},
    {"role": "user", "content": "问题：研发项目状态？json上下文：{}"},
]


@pytest.fixture(autouse=True)
def isolate_chat_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in (
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "DEEPSEEK_TIMEOUT_SECONDS", "DEEPSEEK_TEMPERATURE", "DEEPSEEK_MAX_TOKENS",
        "ZHIPU_API_KEY",
    ):
        monkeypatch.delenv(f"PROJECT_COPILOT_{suffix}", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{"deepseek_api_key": SecretStr(FAKE_KEY), **overrides})


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "fake-completion", "object": "chat.completion", "created": 0,
        "model": "deepseek-flash-2026-alias",
        "choices": [{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": '  {"answer":"已发布"}\n  '},
        }],
        "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
        **overrides,
    }


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_sdk_request_is_explicit_non_thinking_json_and_keeps_original_messages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert request.extensions["timeout"]["read"] == 30.0
        assert json.loads(request.content) == {
            "model": "deepseek-flash", "messages": MESSAGES,
            "temperature": 0.0, "max_tokens": 1024, "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        return httpx.Response(200, json=_payload())

    with _http_client(handler) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            assert isinstance(chat, DeepSeekChat)
            assert chat.model == "deepseek-flash"
            assert requests == []
            result = chat.generate_json(MESSAGES)
        assert http_client.is_closed

    assert len(requests) == 1
    assert result == ChatGeneration(
        content='  {"answer":"已发布"}\n  ', model="deepseek-flash-2026-alias",
        usage={"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
    )
    with pytest.raises(FrozenInstanceError):
        result.content = "changed"  # type: ignore[misc]


def test_custom_parameters_and_pro_model_reach_sdk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-pro"
        assert body["temperature"] == 0.25
        assert body["max_tokens"] == 4096
        assert body["thinking"] == {"type": "disabled"}
        assert request.extensions["timeout"]["read"] == 7.0
        return httpx.Response(200, json=_payload(model="deepseek-v4-pro-alias"))

    settings = _settings(deepseek_model="deepseek-v4-pro", deepseek_temperature=0.25,
                         deepseek_max_tokens=4096, deepseek_timeout_seconds=7)
    with _http_client(handler) as http_client:
        with create_chat_client(settings, http_client=http_client) as chat:
            assert chat.generate_json(MESSAGES).model == "deepseek-v4-pro-alias"


def test_missing_usage_is_unknown_and_content_json_is_left_to_answering_layer() -> None:
    payload = _payload()
    del payload["usage"]
    payload["choices"][0]["message"]["content"] = "raw non-json text"
    with _http_client(lambda _request: httpx.Response(200, json=payload)) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            result = chat.generate_json(MESSAGES)
    assert result.usage is None
    assert result.content == "raw non-json text"


def test_usage_retains_only_checked_counters_and_discards_provider_metadata() -> None:
    usage = {
        "prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
        "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 4,
        "completion_tokens_details": {"reasoning_tokens": 2, "private": PRIVATE_BODY},
        "private": {"key": FAKE_KEY},
    }
    with _http_client(lambda _request: httpx.Response(200, json=_payload(usage=usage))) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            result = chat.generate_json(MESSAGES)
    assert result.usage == {
        "prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
        "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 4, "reasoning_tokens": 2,
    }
    assert PRIVATE_BODY not in repr(result)
    assert FAKE_KEY not in repr(result)


@pytest.mark.parametrize("empty_fields", [
    {"tool_calls": None}, {"tool_calls": []}, {"function_call": None},
    {"tool_calls": [], "function_call": None, "refusal": None},
])
def test_stop_with_empty_tool_fields_is_a_normal_answer(empty_fields: dict[str, Any]) -> None:
    payload = _payload()
    payload["choices"][0]["message"].update(empty_fields)
    with _http_client(lambda _request: httpx.Response(200, json=payload)) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            result = chat.generate_json(MESSAGES)
    assert result.content == payload["choices"][0]["message"]["content"]
    assert result.model == payload["model"]


def _invalid_protocol_payloads() -> list[Any]:
    payloads: list[Any] = [[], None, _payload(model=""), _payload(model=" \n "),
                           _payload(model=123), _payload(choices=[]), _payload(choices=None)]
    duplicate = _payload()
    duplicate["choices"].append(deepcopy(duplicate["choices"][0]))
    payloads.extend([duplicate, _payload(choices=[None])])
    for reason in ("length", "tool_calls", "content_filter", None):
        payload = _payload()
        payload["choices"][0]["finish_reason"] = reason
        payloads.append(payload)
    for message in (None, {"role": "user", "content": "valid"},
                    {"role": "assistant"}, {"role": "assistant", "content": "  "},
                    {"role": "assistant", "content": None},
                    {"role": "assistant", "content": ["valid"]}):
        payload = _payload()
        payload["choices"][0]["message"] = message
        payloads.append(payload)
    for field, value in (("tool_calls", [{"id": "fake-call", "type": "function",
                                          "function": {"name": "fake", "arguments": "{}"}}]),
                         ("tool_calls", {}), ("tool_calls", ""), ("tool_calls", False),
                         ("tool_calls", 0), ("function_call", {"name": "fake"}),
                         ("function_call", {}), ("function_call", []),
                         ("function_call", ""), ("function_call", False),
                         ("tools", []), ("tools", None),
                         ("refusal", PRIVATE_BODY), ("refusal", "")):
        payload = _payload()
        payload["choices"][0]["message"][field] = value
        payloads.append(payload)
    return payloads


@pytest.mark.parametrize("payload", _invalid_protocol_payloads())
def test_invalid_raw_chat_protocol_is_rejected_without_retry(payload: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    with _http_client(handler) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ChatResponseError) as captured:
                chat.generate_json(MESSAGES)
    assert len(requests) == 1
    assert PRIVATE_BODY not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize("body", [b"{not-json", b""])
def test_unparseable_response_is_sanitized(body: bytes) -> None:
    with _http_client(lambda _request: httpx.Response(200, content=body)) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ChatResponseError) as captured:
                chat.generate_json(MESSAGES)
    assert captured.value.__suppress_context__


def _invalid_usage_values() -> list[Any]:
    valid = {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}
    values: list[Any] = [None, [], {}, {**valid, "total_tokens": 13}]
    for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens"):
        for count in (True, "3", 3.0, -1, None):
            values.append({**valid, field: count})
    values.extend([
        {"completion_tokens": 3, "total_tokens": 12},
        {**valid, "completion_tokens_details": None},
        {**valid, "completion_tokens_details": {"reasoning_tokens": True}},
        {**valid, "completion_tokens_details": {"reasoning_tokens": -1}},
        {**valid, "reasoning_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 2}},
    ])
    return values


@pytest.mark.parametrize("usage", _invalid_usage_values())
def test_usage_is_validated_before_sdk_numeric_coercion(usage: Any) -> None:
    with _http_client(lambda _request: httpx.Response(200, json=_payload(usage=usage))) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ChatResponseError):
                chat.generate_json(MESSAGES)


@pytest.mark.parametrize("messages", [
    None, "a string", [], [MESSAGES[0]], MESSAGES + [MESSAGES[1]],
    list(reversed(MESSAGES)), [{"role": "system", "content": " "}, MESSAGES[1]],
    [MESSAGES[0], {"role": "tool", "content": "valid"}],
    [MESSAGES[0], {"role": "user", "content": 1}],
    [MESSAGES[0], {"role": "user", "content": "valid", "tools": []}],
    [None, MESSAGES[1]],
])
def test_invalid_messages_fail_before_http(messages: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload())

    with _http_client(handler) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ValueError):
                chat.generate_json(messages)
    assert requests == []


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_http_errors_have_status_and_no_retry_or_sensitive_exception_context(status: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": f"{PRIVATE_BODY} {FAKE_KEY}"}})

    with _http_client(handler) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ChatRequestError) as captured:
                chat.generate_json(MESSAGES)
    assert len(requests) == 1
    assert captured.value.status_code == status
    assert captured.value.__cause__ is None
    assert captured.value.__suppress_context__
    displayed = "".join(traceback.format_exception(captured.value))
    assert PRIVATE_BODY not in displayed
    assert FAKE_KEY not in displayed


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_failure_is_sanitized_and_not_retried(exception_type: type[httpx.RequestError]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exception_type(f"{PRIVATE_BODY} {FAKE_KEY}", request=request)

    with _http_client(handler) as http_client:
        with create_chat_client(_settings(), http_client=http_client) as chat:
            with pytest.raises(ChatRequestError) as captured:
                chat.generate_json(MESSAGES)
    assert len(requests) == 1
    assert captured.value.status_code is None
    assert captured.value.__suppress_context__
    displayed = "".join(traceback.format_exception(captured.value))
    assert PRIVATE_BODY not in displayed
    assert FAKE_KEY not in displayed


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr(" \t "),
                               SecretStr("bad key"), SecretStr("非法key")])
def test_independent_deepseek_key_never_borrows_openai_or_zhipu(key: SecretStr | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fake-openai-key")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload())

    with _http_client(handler) as http_client:
        with pytest.raises(ChatConfigError) as captured:
            create_chat_client(_settings(deepseek_api_key=key, zhipu_api_key=SecretStr("fake-zhipu")),
                               http_client=http_client)
    assert "PROJECT_COPILOT_DEEPSEEK_API_KEY" in str(captured.value)
    assert requests == []


def test_default_settings_need_no_model_credentials_and_secrets_stay_redacted() -> None:
    settings = Settings(_env_file=None)
    assert settings.deepseek_api_key is None
    assert settings.zhipu_api_key is None
    assert str(settings.deepseek_base_url) == "https://api.deepseek.com/"
    assert settings.deepseek_model == "deepseek-flash"
    assert settings.deepseek_timeout_seconds == 30.0
    assert settings.deepseek_temperature == 0.0
    assert settings.deepseek_max_tokens == 1024
    settings = _settings()
    assert isinstance(settings.deepseek_api_key, SecretStr)
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in settings.model_dump_json()


@pytest.mark.parametrize(("field", "value"), [
    ("deepseek_base_url", "invalid-url"), ("deepseek_base_url", "http://api.deepseek.com"),
    ("deepseek_base_url", "https://user:password@api.deepseek.com"),
    ("deepseek_base_url", "https://api.deepseek.com?key=fake"),
    ("deepseek_base_url", "https://api.deepseek.com#fragment"),
    ("deepseek_model", "deepseek-chat"), ("deepseek_model", "deepseek-reasoner"),
    ("deepseek_model", "other-model"), ("deepseek_timeout_seconds", 0),
    ("deepseek_timeout_seconds", 121), ("deepseek_timeout_seconds", float("nan")),
    ("deepseek_timeout_seconds", float("inf")), ("deepseek_temperature", -0.01),
    ("deepseek_temperature", 2.01), ("deepseek_temperature", float("nan")),
    ("deepseek_temperature", float("inf")), ("deepseek_max_tokens", 0),
    ("deepseek_max_tokens", 4097), ("deepseek_max_tokens", 1.5),
])
def test_chat_configuration_rejects_unsupported_or_unsafe_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_close_is_idempotent_and_context_closes_on_exception() -> None:
    with _http_client(lambda _request: httpx.Response(200, json=_payload())) as http_client:
        chat = create_chat_client(_settings(), http_client=http_client)
        with pytest.raises(RuntimeError, match="controlled-context-error"):
            with chat as active:
                assert active is chat
                raise RuntimeError("controlled-context-error")
        assert http_client.is_closed
        chat.close()
