"""Real compiled graph plus owned PG receipts; providers are counted substitutes."""
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import func, select

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.recovery import AgentCheckpointError, message_inputs
from project_copilot.agent.state import GraphLimits
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.db.models import AiRequestReservation, LoginSession, Ticket
from project_copilot.db.recovery import AiJobRecoveryState, AiJobStep
from project_copilot.db.release import ReleaseControl
from project_copilot.infrastructure import recovery_runtime
from project_copilot.infrastructure.execution_tracking import tracked_to_thread
from project_copilot.infrastructure.job_progress import observe
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.services import release_control
from project_copilot.services.quotas import QuotaService
from project_copilot.services.auth import AuthenticationRejected
from project_copilot.services.execution_slots import ExecutionLease, register_execution
from project_copilot.services.job_recovery import recover_job, start_recovery_budget
from project_copilot.services.job_recovery import RecoveryBudgetExceeded
from test_job_steps_postgres import journal
from test_execution_slots_postgres import slots

USAGE = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


class Crash(BaseException):
    """Process interruption after a committed receipt, before graph state update."""


class FaultJournal:
    def __init__(self, actual, key, *, sql_failure=False, before=False):
        self.actual, self.key, self.sql_failure, self.before = actual, key, sql_failure, before
    def __getattr__(self, name):
        return getattr(self.actual, name)
    def complete(self, key, result):
        if key == self.key and self.before:
            raise Crash()
        value = self.actual.complete(key, result)
        if key == self.key:
            if self.sql_failure:
                raise RuntimeError("private-database-canary")
            raise Crash()
        return value


class Model:
    def __init__(self, setup, messages):
        self.setup, self.messages, self.calls = setup, list(messages), 0
    async def ainvoke(self, messages, schemas):
        self.calls += 1
        assert self.messages, "a saved model response was requested again"
        def charge():
            with self.setup.database.transaction() as session:
                QuotaService(session).mark_provider_attempt(self.setup.user_id, self.setup.identity)
        await tracked_to_thread(charge)
        return self.messages.pop(0)


class Backend(ReadOnlyBackend):
    def __init__(self, setup):
        super().__init__(setup.settings, setup.database, auth_session_id=setup.login_id)
        self.calls = []
    async def get_ticket(self, ticket_id):
        self.calls.append(str(ticket_id))
        return await super().get_ticket(ticket_id)


@pytest.fixture
def graph_setup(journal, monkeypatch):
    # Fix only the synthetic deployment fingerprint; login/ownership/release
    # checks, manifest bindings and runtime source revalidation remain real.
    monkeypatch.setattr(release_control, "config_fingerprint", lambda _: "a" * 64)
    with journal.database.transaction() as session:
        release = session.get(ReleaseControl, 1)
        release.evidence = {**release.evidence, "configuration_fingerprint": "a" * 64}
        tickets = [Ticket(owner_user_id=journal.user_id, title=f"Synthetic checkpoint {index}",
                          description="Owned graph evidence") for index in range(2)]
        session.add_all(tickets)
        session.flush()
        identities = [ticket.id for ticket in tickets]
    journal.tickets = identities
    journal.backend = Backend(journal)
    return journal


def tool_message(setup, *, batch=1, invalid=False):
    calls = [{"id": f"call_{index + 1}", "name": "get_ticket",
              "args": {"ticket_id": "invalid" if invalid else str(setup.tickets[index])}}
             for index in range(batch)]
    return AIMessage(content="", tool_calls=calls, response_metadata={"model": "synthetic", "usage": USAGE})


def final_message(setup, *, batch=1, insufficient=False):
    return AIMessage(content=json.dumps({"status": "insufficient_evidence" if insufficient else "answered",
        "answer": "Synthetic grounded response", "source_ids": [] if insufficient else [
            "ticket:" + str(value) for value in setup.tickets[:batch]]}),
        response_metadata={"model": "synthetic", "usage": USAGE})


def run(setup, model, *, fault=None, sql_failure=False, fault_before=False, limits=None, prepare=None):
    job = SimpleNamespace(id=setup.identity, kind="agent", execution_lease=setup.lease,
                          attempt_no=getattr(setup, "attempt_no", 1))
    with recovery_runtime.recovery_scope(setup.database, setup.settings, setup.settings, job) as runtime:
        if fault:
            runtime.journal = FaultJournal(runtime.journal, fault, sql_failure=sql_failure, before=fault_before)
        if prepare:
            prepare(runtime)
        return asyncio.run(AgentRunner(model, build_readonly_registry(setup.backend), limits).run(
            AgentQuestion(question="Read my synthetic tickets"), "synthetic-checkpoint-run"))


def counts(setup):
    with setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, setup.identity)
        reservation = session.get(AiRequestReservation, setup.identity)
        return state.model_calls, state.tool_calls, reservation.provider_attempts


def test_actual_model_receipt_survives_crash_before_model_node_update(graph_setup):
    setup = graph_setup
    first = Model(setup, [tool_message(setup)])
    with pytest.raises(Crash):
        run(setup, first, fault="agent:model:1")
    resumed = Model(setup, [final_message(setup)])
    result = run(setup, resumed)
    assert result.status == "answered" and result.model_calls == 2 and result.tool_calls == 1
    assert first.calls == resumed.calls == 1 and setup.backend.calls == [str(setup.tickets[0])]
    assert counts(setup) == (2, 1, 2)
    with setup.database.transaction() as session:
        rows = session.scalars(select(AiJobStep).where(AiJobStep.job_id == setup.identity)).all()
        assert len(rows) == 3 and all(row.status == "completed" for row in rows)
        assert all("你是Project Copilot" not in str(row.result) for row in rows)


def test_first_tool_receipt_replays_before_second_tool_without_false_used_id_error(graph_setup):
    setup = graph_setup
    with pytest.raises(Crash):
        run(setup, Model(setup, [tool_message(setup, batch=2)]), fault="agent:tool:1:1")
    assert setup.backend.calls == [str(setup.tickets[0])]
    resumed = Model(setup, [final_message(setup, batch=2)])
    result = run(setup, resumed)
    assert result.status == "answered" and result.failure_type is None
    assert result.tool_calls == result.tool_executions == 2 and result.tool_failures == 0
    assert [event.tool_call_id for event in result.events] == ["call_1", "call_2"]
    assert setup.backend.calls == [str(value) for value in setup.tickets]
    assert counts(setup) == (2, 2, 2)


def test_new_physical_attempt_replays_partial_batch_and_fences_old_graph_owner(graph_setup):
    setup = graph_setup
    with pytest.raises(Crash):
        run(setup, Model(setup, [tool_message(setup, batch=2)]), fault="agent:tool:1:1")
    old = setup.lease
    old.close()
    assert recover_job(setup.database, setup.settings, setup.identity, allow_model_repeat=True).decision == "requeued"
    fresh = ExecutionLease.create(setup.settings.manifest_dir, setup.identity)
    setup.slots.leases.append(fresh)
    with setup.database.transaction() as session:
        assert QuotaService(session).claim(setup.user_id, setup.identity)
        start_recovery_budget(session, setup.identity, setup.settings)
        register_execution(session, session.get(AiRequestReservation, setup.identity), fresh)
    with pytest.raises(AgentCheckpointError):
        run(setup, Model(setup, []))  # Old token cannot even read cached content.
    setup.lease, setup.attempt_no = fresh, 2
    result = run(setup, Model(setup, [final_message(setup, batch=2)]))
    assert result.status == "answered" and result.tool_failures == 0
    assert setup.backend.calls == [str(value) for value in setup.tickets]
    assert counts(setup) == (2, 2, 2)


def test_unfinished_readonly_tool_retries_only_after_archived_authorized_attempt(graph_setup):
    setup = graph_setup
    with pytest.raises(Crash):
        run(setup, Model(setup, [tool_message(setup)]), fault="agent:tool:1:1", fault_before=True)
    assert counts(setup) == (1, 1, 1) and len(setup.backend.calls) == 1
    setup.lease.close()
    assert recover_job(setup.database, setup.settings, setup.identity, allow_model_repeat=True).decision == "requeued"
    fresh = ExecutionLease.create(setup.settings.manifest_dir, setup.identity)
    setup.slots.leases.append(fresh)
    with setup.database.transaction() as session:
        assert QuotaService(session).claim(setup.user_id, setup.identity)
        start_recovery_budget(session, setup.identity, setup.settings)
        register_execution(session, session.get(AiRequestReservation, setup.identity), fresh)
    setup.lease, setup.attempt_no = fresh, 2
    result = run(setup, Model(setup, [final_message(setup)]))
    assert result.status == "answered" and result.tool_calls == result.tool_executions == 1
    assert len(setup.backend.calls) == 2 and counts(setup) == (2, 2, 2)
    with setup.database.transaction() as session:
        row = session.get(AiJobStep, (setup.identity, "agent:tool:1:1"))
        assert row.attempt_no == 2 and row.prior_intent["status"] == "started"


def test_lost_final_job_commit_revalidates_and_reconstructs_result_without_external_calls(graph_setup):
    setup = graph_setup
    original = run(setup, Model(setup, [tool_message(setup), final_message(setup)]))
    before = counts(setup)
    replay = run(setup, Model(setup, []))
    assert replay.status == original.status == "answered"
    assert replay.answer == original.answer and replay.sources == original.sources
    assert replay.usage == original.usage and replay.model_calls == original.model_calls
    assert [event.elapsed_ms for event in replay.events] == [event.elapsed_ms for event in original.events]
    assert counts(setup) == before == (2, 1, 2)
    assert len(setup.backend.calls) == 1


def test_saved_operations_emit_checkpoint_progress_without_new_execution_stages(graph_setup):
    setup = graph_setup
    fresh, resumed = [], []
    def collect(target):
        return lambda kind, data: target.append(data["stage"]) if kind == "progress" else None
    with observe(collect(fresh)):
        run(setup, Model(setup, [tool_message(setup), final_message(setup)]))
    before = counts(setup)
    with observe(collect(resumed)):
        result = run(setup, Model(setup, []))
    assert fresh.count("model") == 2 and fresh.count("get_ticket") == 1
    assert resumed.count("checkpoint") == 3
    assert not {"model", "get_ticket", "tool_done"} & set(resumed)
    assert "validate" in resumed and result.status == "answered"
    assert counts(setup) == before == (2, 1, 2) and len(setup.backend.calls) == 1


def test_invalid_tool_attempt_receipt_replays_without_recharge_or_backend_call(graph_setup):
    setup = graph_setup
    result = run(setup, Model(setup, [tool_message(setup, invalid=True), final_message(setup, insufficient=True)]))
    replay = run(setup, Model(setup, []))
    assert result.status == replay.status == "insufficient_evidence"
    assert result.events[0].failure_type == replay.events[0].failure_type == "arguments_invalid"
    assert setup.backend.calls == [] and counts(setup) == (2, 1, 2)


@pytest.mark.parametrize("change", ["source", "session", "generation"])
def test_saved_tool_content_does_not_bypass_current_authority_or_generation(graph_setup, change):
    setup = graph_setup
    run(setup, Model(setup, [tool_message(setup), final_message(setup)]))
    before = counts(setup)
    with setup.database.transaction() as session:
        if change == "source":
            session.get(Ticket, setup.tickets[0]).title = "Current resource changed after receipt"
        elif change == "session":
            session.get(LoginSession, setup.login_id).revoked_at = session.scalar(select(func.clock_timestamp()))
    def invalidate(runtime):
        if change == "generation":
            def changed():
                raise ValueError("private-index-canary")
            runtime.validate_generation = changed
    with pytest.raises((AgentCheckpointError, AuthenticationRejected)):
        run(setup, Model(setup, []), prepare=invalidate)
    assert counts(setup) == before


def test_checkpoint_failure_escapes_generic_model_error_and_redacts_cause(graph_setup):
    setup = graph_setup
    with pytest.raises(AgentCheckpointError) as error:
        run(setup, Model(setup, [tool_message(setup)]), fault="agent:model:1", sql_failure=True)
    assert "private-database-canary" not in str(error.value) and error.value.__suppress_context__
    assert counts(setup) == (1, 0, 1)


def test_completed_replay_after_deadline_uses_no_new_model_or_tool_budget(graph_setup):
    setup = graph_setup
    run(setup, Model(setup, [tool_message(setup), final_message(setup)]))
    with setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, setup.identity)
        state.deadline_at = state.budget_started_at + timedelta(seconds=0.001)
    result = run(setup, Model(setup, []))
    assert result.status == "answered" and counts(setup) == (2, 1, 2)


@pytest.mark.parametrize("exhausted", ["deadline", "model_budget"])
def test_known_budget_failure_escapes_checkpoint_and_generic_model_handlers(graph_setup, exhausted):
    setup = graph_setup
    with pytest.raises(Crash):
        run(setup, Model(setup, [tool_message(setup)]), fault="agent:tool:1:1")
    with setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, setup.identity)
        if exhausted == "deadline":
            state.deadline_at = state.budget_started_at + timedelta(seconds=0.001)
        else:
            state.max_model_calls = state.model_calls
    model = Model(setup, [])
    with pytest.raises(RecoveryBudgetExceeded) as error:
        run(setup, model)
    assert error.value.reason == exhausted
    assert model.calls == 0 and counts(setup) == (1, 1, 1)


def test_replay_trace_does_not_report_new_tokens_or_tool_execution(graph_setup, monkeypatch):
    from contextlib import contextmanager
    from project_copilot.agent import graph
    setup = graph_setup
    run(setup, Model(setup, [tool_message(setup), final_message(setup)]))
    spans = []
    class Span:
        def __init__(self, name, attributes):
            self.name, self.attributes = name, dict(attributes or {})
        def set_attribute(self, name, value):
            self.attributes[name] = value
        def set_status(self, *_):
            pass
    @contextmanager
    def traced(name, attributes=None):
        span = Span(name, attributes)
        spans.append(span)
        yield span
    monkeypatch.setattr(graph, "trace_span", traced)
    replay = run(setup, Model(setup, []))
    models = [span for span in spans if span.name == "agent.model"]
    assert len(models) == 2 and all(span.attributes["checkpoint.replayed"] for span in models)
    assert all(not any(key.startswith(("llm.token_count", "usage.")) for key in span.attributes) for span in models)
    tools = [span for span in spans if span.name == "agent.tool.result"]
    assert len(tools) == 1 and tools[0].attributes["checkpoint.replayed"]
    assert tools[0].attributes["tool.executed"] is False
    assert replay.tool_executions == 1 and all(event["replayed"] for event in replay.model_events)


def test_tool_logical_budget_rejection_does_not_create_or_charge_an_extra_receipt(graph_setup):
    setup = graph_setup
    result = run(setup, Model(setup, [tool_message(setup, batch=2)]), limits=GraphLimits(max_tool_calls=1))
    assert result.status == "budget_exhausted" and result.failure_type == "tool_budget"
    assert counts(setup) == (1, 1, 1) and len(setup.backend.calls) == 1
    with setup.database.transaction() as session:
        assert session.get(AiJobStep, (setup.identity, "agent:tool:1:2")) is None


def test_input_projection_drops_generated_message_identity_but_keeps_tool_call_identity():
    first = AIMessage(content="", id="generated-A", tool_calls=[
        {"name": "get_ticket", "args": {"ticket_id": "synthetic"}, "id": "call_1"}])
    second = first.model_copy(update={"id": "generated-B"})
    assert message_inputs([first]) == message_inputs([second])
    changed = second.model_copy(update={"tool_calls": [{**second.tool_calls[0], "id": "call_2"}]})
    assert message_inputs([first]) != message_inputs([changed])
