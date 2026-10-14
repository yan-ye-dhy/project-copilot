"""D5问答HTTP契约、配置隔离与资源收尾；不创建真实外部客户端。"""

import asyncio
from contextlib import contextmanager
from uuid import UUID

import httpx
import pytest

from project_copilot.api import rag
from project_copilot.config import Settings
from project_copilot.infrastructure import models
from project_copilot.infrastructure.models import (
    ChatConfigError, ChatRequestError, ChatResponseError,
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from project_copilot.infrastructure.qdrant import QdrantConfigError, QdrantRequestError, QdrantResponseError
from project_copilot.main import create_app
from project_copilot.rag.answering import AnswerResponseError, prompt_info
from project_copilot.rag.ingestion import ManifestError
from project_copilot.schemas.documents import ChunkMetadata
from project_copilot.schemas.rag import Citation, GenerationInfo, RagAnswer


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None, environment="test")


def response_answer(question: str) -> RagAnswer:
    return RagAnswer(
        question=question, status="answered", answer="样本文档介绍问答功能。", evidence_count=1,
        citations=(Citation(
            number=1, text="真实原文：问答功能。",
            metadata=ChunkMetadata(
                document_id=UUID(int=100), content_hash="a" * 64, source_name="sample.pdf",
                page=0, chunk_id=UUID(int=1), chunk_index=0, index_version="split-v1-test",
            ),
        ),),
        prompt=prompt_info(), index_version="dense-v1-test",
        generation=GenerationInfo(
            requested_model="requested-alias", actual_model="actual-model", temperature=0,
            max_tokens=1024, usage={"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16},
        ),
    )


def request(application, method: str, path: str, **kwargs) -> httpx.Response:
    async def run() -> httpx.Response:
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://testserver"
            ) as client:
                return await client.request(method, path, **kwargs)
    return asyncio.run(run())


def test_valid_post_serializes_answer_schema_and_closes_fake_service(monkeypatch, settings) -> None:
    events = []
    expected = response_answer("样本文档的功能？")

    class FakeService:
        def answer(self, question):
            events.append(("answer", question))
            return expected

    @contextmanager
    def fake_factory(active_settings):
        assert active_settings is settings
        events.append("entered")
        try:
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(rag, "create_answer_service", fake_factory)
    application = create_app(settings)
    response = request(application, "POST", "/rag/ask", json={"question": "  样本文档的功能？  "})

    assert response.status_code == 200
    assert response.json() == expected.model_dump(mode="json")
    assert RagAnswer.model_validate(response.json()) == expected
    assert events == ["entered", ("answer", expected.question), "closed"]


@pytest.mark.parametrize("payload", [
    {}, {"question": ""}, {"question": "  \t\n "}, {"question": 123}, {"question": True},
    {"question": "问" * 2001}, {"question": "问题", "model": "user-override"}, [],
])
def test_invalid_question_is_422_before_creating_any_service(monkeypatch, settings, payload) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法请求不应创建任何外部客户端或问答服务")
    monkeypatch.setattr(rag, "create_answer_service", forbidden)
    response = request(create_app(settings), "POST", "/rag/ask", json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("error_type,status,phase", [
    (ChatConfigError, 503, "enter"), (EmbeddingConfigError, 503, "enter"),
    (QdrantConfigError, 503, "enter"), (ManifestError, 503, "enter"),
    (ChatRequestError, 502, "answer"), (EmbeddingRequestError, 502, "answer"),
    (QdrantRequestError, 502, "answer"), (ChatResponseError, 502, "answer"),
    (EmbeddingResponseError, 502, "answer"), (QdrantResponseError, 502, "answer"),
    (AnswerResponseError, 502, "answer"),
])
def test_known_service_errors_are_sanitized_and_resources_are_closed(
    monkeypatch, settings, error_type, status, phase,
) -> None:
    events = []
    private_error = error_type("private-key-and-raw-provider-output")

    class FakeService:
        def answer(self, question):
            events.append(("answer", question))
            raise private_error

    @contextmanager
    def fake_factory(active_settings):
        assert active_settings is settings
        events.append("entered")
        try:
            if phase == "enter":
                raise private_error
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(rag, "create_answer_service", fake_factory)
    response = request(create_app(settings), "POST", "/rag/ask", json={"question": "问题"})

    assert response.status_code == status
    assert set(response.json()) == {"detail"}
    assert response.json()["detail"].strip()
    assert "private-key-and-raw-provider-output" not in response.text
    assert "Traceback" not in response.text
    expected = ["entered", "closed"] if phase == "enter" else ["entered", ("answer", "问题"), "closed"]
    assert events == expected


def test_health_still_starts_without_model_keys_or_client_creation(monkeypatch, settings) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("健康请求不应创建模型客户端或问答服务")
    monkeypatch.setattr(models, "OpenAI", forbidden)
    monkeypatch.setattr(rag, "create_answer_service", forbidden)
    assert settings.zhipu_api_key is None and settings.deepseek_api_key is None
    response = request(create_app(settings), "GET", "/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": settings.app_name, "environment": "test"}


def test_openapi_exposes_question_answer_and_generation_contracts_without_clients(monkeypatch, settings) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("OpenAPI不应创建问答服务")
    monkeypatch.setattr(rag, "create_answer_service", forbidden)
    response = request(create_app(settings), "GET", "/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    operation = schema["paths"]["/rag/ask"]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RagQuestion"
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/RagAnswer"
    }
    schemas = schema["components"]["schemas"]
    assert schemas["RagQuestion"]["additionalProperties"] is False
    assert schemas["RagQuestion"]["properties"]["question"]["maxLength"] == 2000
    assert schemas["Citation"]["properties"]["number"]["minimum"] == 1
    assert {"question", "status", "answer", "citations", "evidence_count", "prompt",
            "index_version", "generation"} == set(schemas["RagAnswer"]["properties"])
    assert {"requested_model", "actual_model", "temperature", "max_tokens", "thinking", "usage"} == set(
        schemas["GenerationInfo"]["properties"]
    )
