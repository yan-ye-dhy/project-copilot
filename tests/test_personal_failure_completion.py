"""Final state is decided after physical work and provider/business reconciliation."""
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import select

from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.service import graph_limits
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.api.jobs import _response
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.models import AiRequestReservation, PendingAction
from project_copilot.infrastructure.agent_model import create_agent_model
from project_copilot.infrastructure.execution_tracking import provider_request
from project_copilot.services import ai_worker
from project_copilot.services.job_events import append_event
from project_copilot.services.quotas import QuotaExceeded
from test_agent_graph import Backend, TICKET_ID
from test_execution_slots_postgres import slots, state, USAGE


def _install_sdk_graph(monkeypatch, slots, handler):
    """Keep Worker/auth/Graph/SDK real; replace only provider transport and tools."""
    observed = SimpleNamespace(requests=[], clients=[], graphs=[], backend=Backend())

    def transport(request):
        observed.requests.append(request)
        return handler(request)

    @asynccontextmanager
    async def service(settings, database, *, context, auth_session_id):
        assert database is slots.database and context is None and auth_session_id == slots.login_id
        model_settings = settings.model_copy(update={
            "deepseek_api_key": SecretStr("synthetic-worker-sdk-key-canary"),
            "deepseek_base_url": "https://provider.invalid/v1",
        })
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False)
        observed.clients.append(client)
        async with create_agent_model(model_settings, http_client=client) as model:
            graph = AgentRunner(model, build_readonly_registry(observed.backend), graph_limits(settings))
            observed.graphs.append(graph)
            yield graph

    monkeypatch.setattr(ai_worker, "create_agent_service", service)
    return observed


@pytest.mark.parametrize("scenario,category,settled", [
    ("401", "model_auth_error", True),
    ("429", "model_rate_limit", True),
    ("invalid_response", "model_response_invalid", True),
    ("invalid_tool_id", "model_tool_protocol", True),
    ("connection", "model_connection_error", False),
    ("timeout", "model_timeout", False),
])
def test_sdk_graph_failure_keeps_actual_worker_completion_boundary(slots, monkeypatch, caplog,
                                                                scenario, category, settled):
    canary = "synthetic-worker-private-provider-body-canary"

    def handler(request):
        if scenario in {"401", "429"}:
            return httpx.Response(int(scenario), json={"error": {"message": canary}})
        if scenario in {"connection", "timeout"}:
            error_type = httpx.ConnectError if scenario == "connection" else httpx.ReadTimeout
            raise error_type(canary, request=request)
        # HTTP and JSON are complete, while the assistant/tool protocol is invalid.
        message = {"role": "assistant", "content": canary}
        reason = "stop"
        if scenario == "invalid_response":
            message["role"] = "user"
        else:
            reason = "tool_calls"
            message["tool_calls"] = [{"type": "function", "id": None,
                "function": {"name": "get_ticket", "arguments": canary}}]
        return httpx.Response(200, json={"model": "synthetic-model", "choices": [
            {"index": 0, "finish_reason": reason, "message": message}],
        })

    observed = _install_sdk_graph(monkeypatch, slots, handler)
    identity, key = slots.reserve(kind="agent")
    # Use the real default execute path, including authentication and compiled Graph.
    assert ai_worker.process_one(slots.database, slots.settings)
    snapshot = state(slots, identity)
    expected_state = "failed" if settled else "unknown"
    assert snapshot.state == expected_state
    assert snapshot.attempts == len(observed.requests) == 1
    assert snapshot.usage is None and snapshot.cost == "unknown"
    assert snapshot.provider_open == 0 and snapshot.provider_uncertain is (not settled)
    assert (snapshot.released is not None) is settled
    assert (snapshot.finished is not None) is settled
    assert snapshot.reason == ("drained" if settled else None)
    assert len(observed.graphs) == len(observed.clients) == 1
    assert type(observed.graphs[0].graph).__name__ == "CompiledStateGraph"
    assert observed.clients[0].is_closed and observed.backend.calls == []
    with slots.database.transaction() as session:
        job, reservation = session.get(AiJob, identity), session.get(AiRequestReservation, identity)
        execution = session.get(AiJobExecution, identity)
        response = _response(job, reservation)
        events = session.scalars(select(AiJobEvent).where(AiJobEvent.job_id == identity)).all()
        assert response["status"] == expected_state and response["failure_type"] == category
        assert response["result"] is None and job.failure_type == category
        assert execution.local_done_at is not None
        assert (execution.fenced_at is not None) is settled
        assert execution.action_id is None and session.query(PendingAction).count() == 0
        assert [event.data for event in events if event.kind == "diagnostic"] == [{"failure_type": category}]
        assert [event.data for event in events if event.kind == "terminal"] == [{"status": expected_state}]
        public_evidence = json.dumps(response, default=str) + json.dumps([event.data for event in events])
        assert canary not in public_evidence + caplog.text
        assert "synthetic-worker-sdk-key-canary" not in public_evidence + caplog.text
    # Reusing the original key reads the same terminal/uncertain job, never another request.
    assert slots.reserve(kind="agent", key=key)[0] == identity
    assert not ai_worker.process_one(slots.database, slots.settings, runner=lambda *_: pytest.fail("replayed"))
    assert len(observed.requests) == 1
    if not settled:
        assert slots.reap() == 0
        assert state(slots, identity) == snapshot
        with pytest.raises(QuotaExceeded) as caught:
            slots.reserve(kind="agent")
        assert caught.value.reason == "user_concurrency"


@pytest.mark.parametrize("usage", [None, USAGE])
def test_sdk_graph_provider_budget_rejects_next_call_before_transport(slots, monkeypatch, usage):
    # Lower only this test's Worker runtime allowance; release/auth/SQL accounting
    # still use the fixture's valid personal grant and real database transactions.
    runtime_settings = ai_worker.personal_runtime_settings
    monkeypatch.setattr(ai_worker, "personal_runtime_settings", lambda settings:
        runtime_settings(settings).model_copy(update={"max_provider_attempts_per_job": 1}))

    def handler(_request):
        assert len(observed.requests) == 1, "exhausted provider budget reached transport"
        body = {"model": "synthetic-model", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": "", "tool_calls": [
                {"type": "function", "id": "synthetic-call", "function": {
                    "name": "get_ticket", "arguments": json.dumps({"ticket_id": TICKET_ID})}},
            ]}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    observed = _install_sdk_graph(monkeypatch, slots, handler)
    identity, key = slots.reserve(kind="agent")
    assert ai_worker.process_one(slots.database, slots.settings)
    snapshot = state(slots, identity)
    assert snapshot.state == "failed" and snapshot.reason == "drained" and snapshot.released is not None
    assert snapshot.attempts == len(observed.requests) == 1 and snapshot.usage == usage
    assert snapshot.provider_open == 0 and not snapshot.provider_uncertain
    assert observed.backend.calls == [("get_ticket", TICKET_ID)]
    assert observed.clients[0].is_closed
    with slots.database.transaction() as session:
        job, reservation = session.get(AiJob, identity), session.get(AiRequestReservation, identity)
        assert job.failure_type == "provider_budget" and job.result is None
        assert _response(job, reservation)["failure_type"] == "provider_budget"
        assert session.query(PendingAction).count() == 0
    assert slots.reserve(kind="agent", key=key)[0] == identity
    assert not ai_worker.process_one(slots.database, slots.settings, runner=lambda *_: pytest.fail("replayed"))


@pytest.mark.parametrize("reason", ["tool_budget", "model_budget", "total_timeout", "repeated_tool_loop", "tool_no_progress"])
def test_known_agent_failure_is_failed_only_after_settled_completion(slots, reason):
    identity, key = slots.reserve(kind="agent")
    calls = []
    def runner(settings, *_):
        assert settings.agent_max_model_calls == 24 and settings.agent_max_tool_calls == 80
        assert settings.agent_total_timeout_seconds == 900 and settings.deepseek_max_tokens == 4096
        with provider_request() as attempt:
            calls.append("synthetic-provider")
            attempt.completed()
        return SimpleNamespace(status="budget_exhausted", failure_type=reason, events=[], model_events=[], usage=USAGE,
            model_dump=lambda **_: {"status": "budget_exhausted"})
    assert ai_worker.process_one(slots.database, slots.settings, runner=runner)
    snapshot = state(slots, identity)
    assert snapshot.state == "failed" and snapshot.released is not None and snapshot.usage == USAGE
    assert snapshot.finished is not None and snapshot.attempts == 1 and calls == ["synthetic-provider"]
    with slots.database.transaction() as session:
        job, reservation = session.get(AiJob, identity), session.get(AiRequestReservation, identity)
        response = _response(job, reservation)
        assert response["failure_type"] == reason and response["result"] is None and response["status"] == "failed"
        assert session.scalar(select(AiJobEvent.data).where(AiJobEvent.job_id == identity,
            AiJobEvent.kind == "terminal")) == {"status": "failed"}
        assert session.query(PendingAction).count() == 0
    assert slots.reserve(kind="agent", key=key)[0] == identity
    assert not ai_worker.process_one(slots.database, slots.settings, runner=lambda *_: pytest.fail("replayed"))


def test_budget_failure_with_unverified_provider_remains_unknown_and_held(slots):
    identity, _ = slots.reserve(kind="agent")
    def runner(*_):
        with provider_request():  # Response was not proven complete.
            pass
        return SimpleNamespace(status="budget_exhausted", failure_type="tool_budget", events=[], model_events=[],
            usage=None, model_dump=lambda **_: {"status": "budget_exhausted"})
    assert ai_worker.process_one(slots.database, slots.settings, runner=runner)
    snapshot = state(slots, identity)
    assert snapshot.state == "unknown" and snapshot.released is None and snapshot.provider_uncertain
    assert snapshot.usage is None and snapshot.finished is None
    with slots.database.transaction() as session:
        assert _response(session.get(AiJob, identity), session.get(AiRequestReservation, identity))["failure_type"] == "tool_budget"


def test_unclassified_failure_is_not_declared_certain(slots):
    identity, _ = slots.reserve(kind="agent")
    def runner(*_):
        return SimpleNamespace(status="failed", failure_type=None, events=[], model_events=[], usage=None,
            model_dump=lambda **_: {"status": "failed"})
    assert ai_worker.process_one(slots.database, slots.settings, runner=runner)
    assert state(slots, identity).state == "unknown"


def test_validated_answer_after_ended_tool_timeout_is_not_automatically_discarded(slots):
    identity, _ = slots.reserve(kind="agent")
    def runner(*_):
        return SimpleNamespace(status="answered", failure_type=None,
            events=[SimpleNamespace(failure_type="tool_timeout")], model_events=[], usage=USAGE,
            model_dump=lambda **_: {"status": "answered", "answer": "validated controlled result"})
    assert ai_worker.process_one(slots.database, slots.settings, runner=runner)
    assert state(slots, identity).state == "succeeded"


def test_old_unknown_reads_existing_safe_diagnostic_without_rewriting_history(slots):
    from project_copilot.services.quotas import QuotaService
    identity, _ = slots.reserve(kind="agent")
    with slots.database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(slots.user_id, identity)
        quota.finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
        append_event(session, identity, "diagnostic", {"failure_type": "tool_budget"})
    before = state(slots, identity)
    with slots.database.transaction() as session:
        job, reservation = session.get(AiJob, identity), session.get(AiRequestReservation, identity)
        assert job.failure_type is None
        assert _response(job, reservation)["failure_type"] == "tool_budget"
        assert job.failure_type is None and reservation.state == "unknown"
    assert state(slots, identity) == before
