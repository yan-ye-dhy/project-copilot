"""Application wiring, real PostgreSQL reservations, no model calls."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure.redis_runtime import RedisUnavailable
from project_copilot.services.redis_admission import RateLimited


def test_redis_configuration_is_opt_in_and_redacted():
    settings = Settings(_env_file=None)
    assert not settings.redis_enabled and not settings.redis_admission_enabled
    for overrides in ({"redis_enabled": True}, {"redis_admission_enabled": True},
                      {"redis_url": "redis://user:PRIVATE@host:6379/0?socket_timeout=999"},
                      {"redis_admission_user_refill_per_second": 0.0000001}):
        with pytest.raises(ValidationError) as caught:
            Settings(_env_file=None, **overrides)
        assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("error,code", [(RateLimited(1.2), 429), (RedisUnavailable(), 503)])
def test_http_rate_refusal_preserves_retry_after_and_safe_body(monkeypatch, error, code):
    import project_copilot.api.jobs as module
    user = uuid4()
    @contextmanager
    def transaction():
        yield object()
    def reserve(*_):
        raise error
    monkeypatch.setattr(module, "public_identity", lambda *_, **__: SimpleNamespace(id=user))
    monkeypatch.setattr(module, "AuthService", lambda _: SimpleNamespace(authenticate=lambda *_, **__: (None, SimpleNamespace(id=uuid4()))))
    monkeypatch.setattr(module, "_token_from_cookie", lambda _: "synthetic-only")
    monkeypatch.setattr(module, "_quota", lambda *_: SimpleNamespace(reserve=reserve))
    monkeypatch.setattr(module, "_record_rejected", lambda *_: None)
    app = FastAPI(); app.include_router(module.router)
    app.state.settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a"*32,
                                  auth_public_origin="https://demo.example")
    app.state.database = SimpleNamespace(transaction=transaction)
    with TestClient(app) as client:
        response = client.post("/jobs", headers={"Idempotency-Key": "week1-key-123"},
                               json={"kind": "rag", "payload": {"question": "synthetic"}})
    assert response.status_code == code
    assert response.headers.get("Retry-After") == ("2" if code == 429 else None)
    assert "PRIVATE" not in response.text and "redis://" not in response.text


def test_pg_replay_does_not_consume_another_rate_token(ticket_database, monkeypatch):
    from project_copilot.schemas.auth import RegisterInput
    from project_copilot.services.auth import AuthService
    from project_copilot.services.quotas import QuotaService
    from project_copilot.services import redis_admission
    calls = []
    def admit(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise RedisUnavailable()
    monkeypatch.setattr(redis_admission, "admit", admit)
    settings = Settings(_env_file=None, environment="test")
    with ticket_database.transaction() as session:
        user = AuthService(session).register(RegisterInput(username="week1user", password="synthetic-password-123"), open_registration=True)
        user_id = user.id
    with ticket_database.transaction() as session:
        row, reused = QuotaService(session, settings=settings).reserve(user_id, "week1-key-123", "a"*64, "rag")
        expected = row.id
        assert not reused
    with ticket_database.transaction() as session:
        row, reused = QuotaService(session, settings=settings).reserve(user_id, "week1-key-123", "a"*64, "rag")
        assert reused and row.id == expected
    assert calls == [1]
    with pytest.raises(RedisUnavailable):
        with ticket_database.transaction() as session:
            QuotaService(session, settings=settings).reserve(user_id, "week1-other-123", "b"*64, "rag")
    from sqlalchemy import select, func
    from project_copilot.db.models import AiRequestReservation
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1


@pytest.mark.parametrize("error,expected", [(RateLimited(1), "admission_rate_limited"),
                                          (RedisUnavailable(), "admission_unavailable")])
def test_actual_graph_keeps_admission_distinct_without_retry(error, expected):
    import asyncio
    from project_copilot.agent.graph import AgentRunner
    from project_copilot.agent.tools import ToolRegistry
    from project_copilot.schemas.agent import AgentQuestion
    class Model:
        calls = 0
        async def ainvoke(self, *_):
            self.calls += 1
            raise error
    model = Model()
    result = asyncio.run(AgentRunner(model, ToolRegistry()).run(AgentQuestion(question="合成准入测试"), "week1-admission"))
    assert result.status == "failed" and result.failure_type == expected
    assert result.model_events[0]["failure_type"] == expected
    assert model.calls == 1 and result.tool_executions == 0 and result.usage is None


@pytest.mark.parametrize("error,expected", [(RateLimited(1), "admission_rate_limited"),
                                          (RedisUnavailable(), "admission_unavailable")])
def test_tool_provider_admission_stops_graph_without_another_decision(error, expected):
    from test_agent_graph import Backend, call, run
    backend = Backend()
    async def denied(ticket_id):
        raise error
    backend.get_ticket = denied
    result, _, model, _ = run([call()], backend=backend)
    assert result.status == "failed" and result.failure_type == expected
    assert result.tool_calls == result.tool_executions == result.tool_failures == 1
    assert result.events[0].failure_type == expected
    assert result.model_calls == 1
