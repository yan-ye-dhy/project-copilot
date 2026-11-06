"""Final state is decided after physical work and provider/business reconciliation."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from project_copilot.api.jobs import _response
from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.models import AiRequestReservation, PendingAction
from project_copilot.infrastructure.execution_tracking import provider_request
from project_copilot.services import ai_worker
from project_copilot.services.job_events import append_event
from test_execution_slots_postgres import slots, state, USAGE


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
