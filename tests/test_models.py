"""D2离线契约：真实OpenAI SDK序列化，经MockTransport收发假数据。"""

import json
import traceback
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from project_copilot.config import Settings
from project_copilot.infrastructure.models import (
    EmbeddingConfigError,
    EmbeddingRequestError,
    EmbeddingResponseError,
    ZhipuEmbeddings,
    create_embedding_client,
)


FAKE_KEY = "d2-fake-key-canary-do-not-log"
PRIVATE_BODY = "d2-private-response-canary-do-not-log"
MODEL_ENVIRONMENT_VARIABLES = (
    "PROJECT_COPILOT_APP_NAME",
    "PROJECT_COPILOT_ENVIRONMENT",
    "PROJECT_COPILOT_ZHIPU_API_KEY",
    "PROJECT_COPILOT_ZHIPU_BASE_URL",
    "PROJECT_COPILOT_EMBEDDING_MODEL",
    "PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS",
    "PROJECT_COPILOT_EMBEDDING_BATCH_SIZE",
    "PROJECT_COPILOT_ZHIPU_TIMEOUT_SECONDS",
)


@pytest.fixture(autouse=True)
def isolate_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """不读取dotenv，配置和凭据由测试显式提供。"""
    for variable in MODEL_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "zhipu_api_key": SecretStr(FAKE_KEY),
        "embedding_expected_dimensions": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _payload(
    vectors: list[list[Any]],
    *,
    indices: list[Any] | None = None,
    model: str = "embedding-3",
) -> dict[str, Any]:
    if indices is None:
        indices = list(range(len(vectors)))
    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in zip(indices, vectors, strict=True)
        ],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_document_requests_preserve_chinese_and_split_at_64() -> None:
    texts = [f"研发知识第{index}条：原文不切分。" for index in range(65)]
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://open.bigmodel.cn/api/paas/v4/embeddings"
        assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
        assert request.extensions["timeout"]["read"] == 20.0
        body = json.loads(request.content)
        assert set(body) == {"model", "input", "encoding_format"}
        assert body["model"] == "embedding-3"
        assert body["encoding_format"] == "float"
        assert all(isinstance(text, str) for text in body["input"])
        requests.append(body)
        vectors = [[texts.index(text), 0.25, -0.5] for text in body["input"]]
        return httpx.Response(200, json=_payload(vectors))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            assert isinstance(embeddings, ZhipuEmbeddings)
            result = embeddings.embed_documents(texts)

    assert [body["input"] for body in requests] == [texts[:64], texts[64:]]
    assert result == [[float(index), 0.25, -0.5] for index in range(65)]
    assert all(type(value) is float for vector in result for value in vector)


def test_embed_query_sends_one_raw_string_and_returns_one_vector() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_payload([[1, -2, 0]]))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            vector = embeddings.embed_query("  谁负责排障？\n保留原文空格。  ")

    assert requests == [
        {
            "model": "embedding-3",
            "input": ["  谁负责排障？\n保留原文空格。  "],
            "encoding_format": "float",
        }
    ]
    assert vector == [1.0, -2.0, 0.0]
    assert all(type(value) is float for value in vector)


def test_empty_document_sequence_has_no_requests() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([]))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            assert embeddings.embed_documents([]) == []
            assert embeddings.embed_documents(()) == []
    assert requests == []


@pytest.mark.parametrize("text", ["", " \t\n ", None, 123, True, ["中文"]])
def test_invalid_query_is_rejected_before_http(text: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([[1, 2, 3]]))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(ValueError):
                embeddings.embed_query(text)
    assert requests == []


@pytest.mark.parametrize("texts", [["有效", ""], ["有效", "  "], ["有效", 123], [None], "整段字符串", None])
def test_invalid_document_input_is_rejected_before_http(texts: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([[1, 2, 3]]))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(ValueError):
                embeddings.embed_documents(texts)
    assert requests == []


def test_full_input_is_validated_before_first_batch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([[1, 2, 3]] * 64))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(ValueError):
                embeddings.embed_documents(["有效输入"] * 64 + ["  "])
    assert requests == []


def test_response_indices_restore_input_order_for_a_tuple() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["input"] == ["第一段", "第二段"]
        return httpx.Response(200, json=_payload([[4, 5, 6], [1, 2, 3]], indices=[1, 0]))

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            assert embeddings.embed_documents(("第一段", "第二段")) == [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]


@pytest.mark.parametrize(
    "response_payload",
    [
        _payload([[1, 2, 3]], model="another-model"),
        _payload([[1, 2, 3]]),
        _payload([]),
        _payload([[1, 2, 3]] * 3),
        _payload([[1, 2, 3], [4, 5, 6]], indices=[0, 0]),
        _payload([[1, 2, 3], [4, 5, 6]], indices=[-1, 1]),
        _payload([[1, 2, 3], [4, 5, 6]], indices=[0, 2]),
        _payload([[1, 2, 3], [4, 5, 6]], indices=[False, 1]),
        _payload([[1, 2, 3], [4, 5, 6]], indices=["0", 1]),
        _payload([[1, 2], [4, 5, 6]]),
        _payload([[1, 2, 3], [4, 5, 6, 7]]),
        _payload([[True, 2, 3], [4, 5, 6]]),
        _payload([["1", 2, 3], [4, 5, 6]]),
        _payload([[None, 2, 3], [4, 5, 6]]),
        {"object": "list", "model": "embedding-3", "data": None},
        {"object": "list", "model": "embedding-3"},
    ],
    ids=[
        "wrong-model", "too-few", "empty-data", "too-many", "duplicate-index", "negative-index",
        "index-out-of-range", "boolean-index", "string-index", "short-vector", "long-vector",
        "boolean-coordinate", "string-coordinate", "null-coordinate", "null-data", "missing-data",
    ],
)
def test_invalid_response_contract_is_rejected(response_payload: dict[str, Any]) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_payload)

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(EmbeddingResponseError):
                embeddings.embed_documents(["第一段", "第二段"])


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")], ids=["nan", "infinity", "negative-infinity"])
def test_nonfinite_coordinate_is_rejected(nonfinite: float) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        # httpx的json=编码拒绝非有限数，因此用原始JSON构造异常响应。
        body = json.dumps(_payload([[nonfinite, 0.0, 1.0]]), allow_nan=True)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(EmbeddingResponseError):
                embeddings.embed_query("中文")


@pytest.mark.parametrize("body", [b"{not-json", b"[]", b"null"])
def test_malformed_or_non_object_json_is_rejected(body: bytes) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(EmbeddingResponseError) as captured:
                embeddings.embed_query("中文")
        assert http_client.is_closed
    assert FAKE_KEY not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize("status_code", [401, 429, 500])
def test_http_errors_have_status_without_retries_or_private_body(status_code: int) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            json={"error": {"message": f"{PRIVATE_BODY} {FAKE_KEY}", "code": "fake-provider-error"}},
        )

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(EmbeddingRequestError) as captured:
                embeddings.embed_query("受控输入")

    assert len(requests) == 1
    assert captured.value.status_code == status_code
    assert FAKE_KEY not in str(captured.value)
    assert PRIVATE_BODY not in str(captured.value)
    assert FAKE_KEY not in repr(captured.value)
    assert PRIVATE_BODY not in repr(captured.value)
    displayed_traceback = "".join(traceback.format_exception(captured.value))
    assert FAKE_KEY not in displayed_traceback
    assert PRIVATE_BODY not in displayed_traceback


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.ConnectError], ids=["timeout", "connection"])
def test_transport_failures_are_sanitized_and_not_retried(exception_type: type[httpx.RequestError]) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exception_type(f"{PRIVATE_BODY} {FAKE_KEY}", request=request)

    with _http_client(handler) as http_client:
        with create_embedding_client(_settings(), http_client=http_client) as embeddings:
            with pytest.raises(EmbeddingRequestError) as captured:
                embeddings.embed_query("受控输入")

    assert len(requests) == 1
    assert captured.value.status_code is None
    assert FAKE_KEY not in str(captured.value)
    assert PRIVATE_BODY not in str(captured.value)
    displayed_traceback = "".join(traceback.format_exception(captured.value))
    assert FAKE_KEY not in displayed_traceback
    assert PRIVATE_BODY not in displayed_traceback


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr(" \t ")], ids=["missing", "empty", "whitespace"])
def test_missing_zhipu_key_does_not_borrow_openai_key(key: SecretStr | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fake-openai-key")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload([[1, 2, 3]]))

    with _http_client(handler) as http_client:
        with pytest.raises(EmbeddingConfigError) as captured:
            create_embedding_client(_settings(zhipu_api_key=key), http_client=http_client)

    assert "PROJECT_COPILOT_ZHIPU_API_KEY" in str(captured.value)
    assert "unrelated-fake-openai-key" not in str(captured.value)
    assert requests == []


def test_health_settings_do_not_require_embedding_credentials() -> None:
    settings = Settings(_env_file=None)
    assert settings.zhipu_api_key is None
    assert settings.app_name == "Project Copilot"
    assert settings.embedding_model == "embedding-3"
    assert settings.embedding_expected_dimensions == 2048
    assert settings.embedding_batch_size == 64
    assert settings.zhipu_timeout_seconds == 20


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("zhipu_base_url", "invalid-url"),
        ("embedding_model", "unsupported-model"),
        ("embedding_expected_dimensions", 0),
        ("embedding_expected_dimensions", -1),
        ("embedding_expected_dimensions", 1.5),
        ("embedding_batch_size", 0),
        ("embedding_batch_size", 65),
        ("embedding_batch_size", -1),
        ("zhipu_timeout_seconds", 0),
        ("zhipu_timeout_seconds", -1),
        ("zhipu_timeout_seconds", 121),
        ("zhipu_timeout_seconds", float("nan")),
        ("zhipu_timeout_seconds", float("inf")),
    ],
)
def test_invalid_model_configuration_is_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_secret_configuration_repr_and_json_are_redacted() -> None:
    settings = _settings()
    assert isinstance(settings.zhipu_api_key, SecretStr)
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in settings.model_dump_json()


def test_custom_batch_size_and_timeout_are_used() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] == 7.0
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=_payload([[1, 2, 3]] * len(body["input"])))

    settings = _settings(embedding_batch_size=2, zhipu_timeout_seconds=7)
    with _http_client(handler) as http_client:
        with create_embedding_client(settings, http_client=http_client) as embeddings:
            assert len(embeddings.embed_documents(["一", "二", "三"])) == 3
    assert [body["input"] for body in requests] == [["一", "二"], ["三"]]


def test_close_releases_http_client_and_can_be_called_twice() -> None:
    with _http_client(lambda _request: httpx.Response(200, json=_payload([[1, 2, 3]]))) as http_client:
        embeddings = create_embedding_client(_settings(), http_client=http_client)
        assert not http_client.is_closed
        embeddings.close()
        assert http_client.is_closed
        embeddings.close()


def test_context_manager_releases_client_on_an_exception() -> None:
    with _http_client(lambda _request: httpx.Response(200, json=_payload([[1, 2, 3]]))) as http_client:
        embeddings = create_embedding_client(_settings(), http_client=http_client)
        with pytest.raises(RuntimeError, match="controlled-context-error"):
            with embeddings as active_client:
                assert active_client is embeddings
                raise RuntimeError("controlled-context-error")
        assert http_client.is_closed
