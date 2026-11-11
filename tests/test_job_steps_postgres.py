"""Owned PostgreSQL step receipts; every model, resource and retry is controlled."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from project_copilot.db.jobs import AiJob
from project_copilot.db.migrations import check_model_drift
from project_copilot.db.models import AiRequestReservation, LoginSession
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState, AiJobStep
from project_copilot.services.execution_slots import ExecutionFenced, ExecutionLease, register_execution
from project_copilot.services.job_recovery import (RecoveryBudgetExceeded, initialize_recovery, recover_job,
                                                  start_recovery_budget)
from project_copilot.services.job_steps import (MAX_JOB_RESULT_BYTES, MAX_JOB_STEPS, MAX_RESULT_BYTES,
    StepJournal, StepRejected, StepUncertain,
    bounded_json, fingerprint, normalize_model_message, normalize_tool_result, restore_model_message)
from project_copilot.services.quotas import QuotaService
from test_execution_slots_postgres import slots

CONFIG = "a" * 64
INDEX = "b" * 64
INPUT = fingerprint({"question": "synthetic journal input"})
MODEL_RESULT = normalize_model_message(AIMessage(content="synthetic complete response",
    response_metadata={"model": "synthetic-model", "usage": {
        "prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}))


@pytest.fixture
def journal(slots):
    settings = slots.settings.model_copy(update={"job_recovery_enabled": True, "message_queue_enabled": True,
        "rabbitmq_url": SecretStr("amqp://synthetic:synthetic@127.0.0.1:15672/synthetic")})
    identity, _ = slots.reserve()
    lease = ExecutionLease.create(settings.manifest_dir, identity)
    slots.leases.append(lease)
    with slots.database.transaction() as session:
        assert initialize_recovery(session, identity, settings) is not None
        assert QuotaService(session).claim(slots.user_id, identity)
        start_recovery_budget(session, identity, settings)
        register_execution(session, session.get(AiRequestReservation, identity), lease)
    def make(token=None, *, config=CONFIG, index=INDEX, revalidate=None):
        return StepJournal(slots.database, settings, identity, token or lease.token,
            config_sha256=config, index_fingerprint=index,
            revalidate=revalidate or (lambda *_: True))
    return SimpleNamespace(database=slots.database, settings=settings, user_id=slots.user_id,
        login_id=slots.login_id, identity=identity, lease=lease, make=make, slots=slots)


def retry_attempt(journal, *, paid=False, repeat=False, checkpoint=False):
    journal.lease.close()
    if paid:
        with journal.database.transaction() as session:
            session.get(AiRequestReservation, journal.identity).provider_attempts = 1
    decision = recover_job(journal.database, journal.settings, journal.identity, allow_model_repeat=repeat,
                           checkpoint_available=checkpoint)
    assert decision.decision == "requeued"
    lease = ExecutionLease.create(journal.settings.manifest_dir, journal.identity)
    journal.slots.leases.append(lease)
    with journal.database.transaction() as session:
        assert QuotaService(session).claim(journal.user_id, journal.identity)
        start_recovery_budget(session, journal.identity, journal.settings)
        register_execution(session, session.get(AiRequestReservation, journal.identity), lease)
    return journal.make(lease.token), lease


def test_step_schema_matches_migration(journal):
    check_model_drift(journal.database)


def test_model_normalization_keeps_protocol_and_discards_raw_transport_metadata():
    message = AIMessage(content="必要的模型回答", tool_calls=[
        {"id": "call_1", "name": "search_tickets", "args": {"q": "E42"}, "type": "tool_call"}],
        additional_kwargs={"headers": {"Authorization": "private-canary"}},
        response_metadata={"model": "synthetic", "headers": {"x-api-key": "private-canary"},
                           "request": {"system_prompt": "private-canary"}, "usage": None})
    normalized = normalize_model_message(message)
    assert "private-canary" not in str(normalized)
    restored = restore_model_message(normalized)
    assert restored.content == message.content and restored.tool_calls == message.tool_calls
    assert restored.additional_kwargs == {} and set(restored.response_metadata) == {"model", "usage"}


@pytest.mark.parametrize("value", [
    {"headers": {"authorization": "private"}}, {"data": {"context_token": "private"}},
    {"system_prompt": "private"}, {"messages": [{"role": "system", "content": "private"}]},
    {"data": "x" * MAX_RESULT_BYTES}, {"data": float("nan")},
])
def test_journal_rejects_raw_capabilities_histories_and_unbounded_results(value):
    with pytest.raises(StepRejected, match="result_contract"):
        bounded_json(value)


def test_tool_normalization_rejects_transport_extensions_but_retains_trusted_sources():
    value = {"data": {"count": 0}, "sources": []}
    assert normalize_tool_result(value) == value
    with pytest.raises(StepRejected):
        normalize_tool_result({**value, "headers": {"Authorization": "private"}})


def test_intent_precedes_call_and_completed_result_reuses_without_new_charge(journal):
    writer = journal.make()
    assert writer.begin("model:1", kind="model", input_sha256=INPUT).decision == "execute"
    with journal.database.transaction() as session:
        intent = session.get(AiJobStep, (journal.identity, "model:1"))
        assert intent.status == "started" and intent.result is None
        assert intent.execution_token == journal.lease.token and intent.attempt_no == 1
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1
    with pytest.raises(StepUncertain):
        writer.begin("model:1", kind="model", input_sha256=INPUT)
    assert writer.complete("model:1", MODEL_RESULT) == MODEL_RESULT
    decision = writer.begin("model:1", kind="model", input_sha256=INPUT)
    assert decision.decision == "replay" and decision.result == MODEL_RESULT
    with journal.database.transaction() as session:
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1
        assert session.get(AiRequestReservation, journal.identity).provider_attempts == 0
        assert session.scalar(select(func.count()).select_from(AiJobStep)) == 1


def test_model_response_survives_crash_before_node_state_write(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.complete("model:1", MODEL_RESULT)
    # A fresh journal has no Python response object or graph node state.
    restarted = journal.make()
    assert restarted.read("model:1", kind="model", input_sha256=INPUT) == MODEL_RESULT
    node = {"model_calls": 1, "status": "running", "next_node": "tools"}
    assert restarted.save_node_state("node:model:1", node, input_sha256=INPUT) == node
    assert restarted.read("node:model:1", kind="node", input_sha256=INPUT) == node


def test_recovered_attempt_replays_completed_response_without_another_model_charge(journal):
    original = journal.make()
    original.begin("model:1", kind="model", input_sha256=INPUT)
    original.complete("model:1", MODEL_RESULT)
    restarted, _ = retry_attempt(journal, paid=True, checkpoint=True)
    decision = restarted.begin("model:1", kind="model", input_sha256=INPUT)
    assert decision.decision == "replay" and decision.result == MODEL_RESULT
    with journal.database.transaction() as session:
        step = session.get(AiJobStep, (journal.identity, "model:1"))
        assert step.attempt_no == 1 and step.execution_token == journal.lease.token
        assert step.prior_intent is None
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1
        assert session.get(AiRequestReservation, journal.identity).provider_attempts == 1
        assert session.get(AiJobExecutionAttempt, (journal.identity, 1)).reason == "checkpoint"
    with pytest.raises(ExecutionFenced):
        original.read("model:1", kind="model", input_sha256=INPUT)


@pytest.mark.parametrize("changed", ["input", "config", "index", "resource"])
def test_cached_result_requires_current_input_configuration_index_and_permissions(journal, changed):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.complete("model:1", MODEL_RESULT)
    reader = journal.make(config="c" * 64 if changed == "config" else CONFIG,
        index="d" * 64 if changed == "index" else INDEX,
        revalidate=(lambda *_: False) if changed == "resource" else None)
    with pytest.raises(StepRejected):
        reader.read("model:1", kind="model", input_sha256="e" * 64 if changed == "input" else INPUT)


def test_cached_result_requires_live_original_session_and_owner(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.complete("model:1", MODEL_RESULT)
    with journal.database.transaction() as session:
        session.get(LoginSession, journal.login_id).revoked_at = session.scalar(select(func.clock_timestamp()))
    from project_copilot.services.auth import AuthenticationRejected
    with pytest.raises(AuthenticationRejected):
        writer.read("model:1", kind="model", input_sha256=INPUT)


def test_completed_receipt_is_immutable_and_unknown_never_implicitly_reexecutes(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.complete("model:1", MODEL_RESULT)
    assert writer.complete("model:1", MODEL_RESULT) == MODEL_RESULT
    with pytest.raises(StepRejected, match="immutable_result"):
        writer.complete("model:1", {**MODEL_RESULT, "content": "another response"})
    writer.begin("model:2", kind="model", input_sha256=INPUT)
    assert writer.mark_unknown("model:2")
    with pytest.raises(StepUncertain):
        writer.begin("model:2", kind="model", input_sha256=INPUT)
    with pytest.raises(StepUncertain):
        writer.complete("model:2", MODEL_RESULT)


def test_concurrent_same_step_admits_one_physical_intent(journal):
    barrier = Barrier(2)
    def begin(_):
        barrier.wait(timeout=10)
        try:
            return journal.make().begin("model:1", kind="model", input_sha256=INPUT).decision
        except StepUncertain:
            return "uncertain"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(begin, range(2))) == ["execute", "uncertain"]
    with journal.database.transaction() as session:
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1
        assert session.scalar(select(func.count()).select_from(AiJobStep)) == 1


def test_pre_call_intent_retries_only_after_proven_zero_provider_recovery(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    restarted, lease = retry_attempt(journal)
    assert restarted.begin("model:1", kind="model", input_sha256=INPUT).decision == "execute"
    restarted.complete("model:1", MODEL_RESULT)
    with journal.database.transaction() as session:
        step = session.get(AiJobStep, (journal.identity, "model:1"))
        assert step.attempt_no == 2 and step.execution_token == lease.token
        assert step.prior_intent["execution_token"] == str(journal.lease.token)
        assert step.prior_intent["status"] == "started"
        assert session.get(AiJobExecutionAttempt, (journal.identity, 1)).reason == "no_provider"
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 2
        assert session.scalar(select(func.count()).select_from(AiJobStep)) == 1
    with pytest.raises(ExecutionFenced):
        writer.complete("model:1", MODEL_RESULT)


def test_settled_provider_without_saved_result_requires_explicit_repeat_policy(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.mark_unknown("model:1")
    journal.lease.close()
    with journal.database.transaction() as session:
        session.get(AiRequestReservation, journal.identity).provider_attempts = 1
    decision = recover_job(journal.database, journal.settings, journal.identity)
    assert decision.decision == "blocked" and decision.reason == "model_repeat_required"
    restarted, _ = retry_attempt(journal, repeat=True)
    assert restarted.begin("model:1", kind="model", input_sha256=INPUT).decision == "execute"
    with journal.database.transaction() as session:
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 2
        assert session.get(AiRequestReservation, journal.identity).provider_attempts == 1
        assert session.get(AiJobStep, (journal.identity, "model:1")).prior_intent["status"] == "unknown"


def test_uncertain_provider_response_cannot_be_repeated_even_with_fee_policy(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    writer.mark_unknown("model:1")
    journal.lease.close()
    with journal.database.transaction() as session:
        session.get(AiRequestReservation, journal.identity).provider_attempts = 1
        session.get(AiJobExecution, journal.identity).provider_uncertain = True
    decision = recover_job(journal.database, journal.settings, journal.identity, allow_model_repeat=True)
    assert decision.decision == "blocked" and decision.reason == "provider_uncertain"
    with pytest.raises(StepUncertain):
        writer.begin("model:1", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        assert session.get(AiJobRecoveryState, journal.identity).current_attempt_no == 1
        assert session.get(AiJobStep, (journal.identity, "model:1")).prior_intent is None


def test_uncertain_tool_is_not_retried_without_explicit_readonly_policy(journal):
    writer = journal.make()
    writer.begin("tool:call_1", kind="tool", input_sha256=INPUT)
    restarted, _ = retry_attempt(journal)
    with pytest.raises(StepUncertain):
        restarted.begin("tool:call_1", kind="tool", input_sha256=INPUT)
    assert restarted.begin("tool:call_1", kind="tool", input_sha256=INPUT,
                           retry_policy="readonly_tool").decision == "execute"


def test_expired_budget_keeps_receipts_and_cached_finalization_but_denies_new_calls(journal):
    writer = journal.make()
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        state = session.get(AiJobRecoveryState, journal.identity)
        state.budget_started_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=60)
        state.deadline_at = state.budget_started_at + timedelta(seconds=1)
    assert writer.complete("model:1", MODEL_RESULT) == MODEL_RESULT
    assert writer.begin("model:1", kind="model", input_sha256=INPUT).decision == "replay"
    with pytest.raises(RecoveryBudgetExceeded, match="budget exhausted"):
        writer.begin("model:2", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        assert session.get(AiJobStep, (journal.identity, "model:2")) is None
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1


def test_budget_failure_rolls_back_intent_and_new_unknown_job_cannot_start_calls(journal):
    writer = journal.make()
    with journal.database.transaction() as session:
        session.get(AiJobRecoveryState, journal.identity).max_model_calls = 1
    writer.begin("model:1", kind="model", input_sha256=INPUT)
    with pytest.raises(RecoveryBudgetExceeded):
        writer.begin("model:2", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        assert session.get(AiJobStep, (journal.identity, "model:2")) is None
        session.get(AiRequestReservation, journal.identity).state = "unknown"
    with pytest.raises(StepRejected, match="not_running"):
        writer.begin("tool:1", kind="tool", input_sha256=INPUT)


def seed_steps(journal, count, *, result=None):
    """Bulk synthetic baseline only; actual contending writes use the journal."""
    with journal.database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        for index in range(count):
            completed = ({"result": result, "result_sha256": fingerprint(result), "completed_at": now}
                         if result is not None else {})
            session.add(AiJobStep(job_id=journal.identity, step_key=f"seed:{index}", kind="node",
                input_sha256=INPUT, config_sha256=CONFIG, index_fingerprint=INDEX,
                status="completed" if result is not None else "started", attempt_no=1,
                execution_token=journal.lease.token, **completed))


def test_last_job_step_slot_is_atomic_across_distinct_keys(journal):
    seed_steps(journal, MAX_JOB_STEPS - 1)
    barrier = Barrier(2)
    def begin(index):
        barrier.wait(timeout=10)
        try:
            return journal.make().begin(f"node:contender:{index}", kind="node", input_sha256=INPUT).decision
        except StepRejected as error:
            return error.reason
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(begin, range(2))) == ["execute", "row_limit"]
    with journal.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobStep)) == MAX_JOB_STEPS
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 0


def test_aggregate_result_cap_is_atomic_and_failed_completion_keeps_its_intent(journal):
    writer = journal.make()
    for key in ("node:a", "node:b"):
        writer.begin(key, kind="node", input_sha256=INPUT)
    # JSONB's exact representation is 262144 bytes, with the shared 4MiB cap
    # leaving one such row of capacity. Two valid 150KiB results cannot both fit.
    full = {"data": "x" * (MAX_RESULT_BYTES - 12)}
    seed_steps(journal, MAX_JOB_RESULT_BYTES // MAX_RESULT_BYTES - 1, result=full)
    result = {"data": "y" * 150000}
    barrier = Barrier(2)
    def finish(key):
        barrier.wait(timeout=10)
        try:
            journal.make().complete(key, result)
            return "completed"
        except StepRejected as error:
            return error.reason
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(finish, ("node:a", "node:b"))) == ["completed", "job_size_limit"]
    with journal.database.transaction() as session:
        rows = session.scalars(select(AiJobStep).where(AiJobStep.step_key.in_(("node:a", "node:b")))).all()
        assert sorted(row.status for row in rows) == ["completed", "started"]
        pending = next(row for row in rows if row.status == "started")
        assert pending.result is None and pending.result_sha256 is None and pending.completed_at is None


def test_full_aggregate_rejects_new_intent_before_charging_model(journal):
    full = {"data": "x" * (MAX_RESULT_BYTES - 12)}
    seed_steps(journal, MAX_JOB_RESULT_BYTES // MAX_RESULT_BYTES, result=full)
    with pytest.raises(StepRejected, match="job_size_limit"):
        journal.make().begin("model:overflow", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        assert session.get(AiJobStep, (journal.identity, "model:overflow")) is None
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 0


def test_retry_prior_intent_receipt_counts_toward_aggregate_before_recharge(journal):
    journal.make().begin("model:retry", kind="model", input_sha256=INPUT)
    full = {"data": "x" * (MAX_RESULT_BYTES - 12)}
    seed_steps(journal, MAX_JOB_RESULT_BYTES // MAX_RESULT_BYTES - 1, result=full)
    tail = {"data": "y" * (MAX_RESULT_BYTES - 112)}  # Exactly 100 bytes remain.
    with journal.database.transaction() as session:
        session.add(AiJobStep(job_id=journal.identity, step_key="node:tail", kind="node",
            input_sha256=INPUT, config_sha256=CONFIG, index_fingerprint=INDEX, status="completed",
            attempt_no=1, execution_token=journal.lease.token, result=tail, result_sha256=fingerprint(tail),
            completed_at=session.scalar(select(func.clock_timestamp()))))
    restarted, _ = retry_attempt(journal)
    with pytest.raises(StepRejected, match="job_size_limit"):
        restarted.begin("model:retry", kind="model", input_sha256=INPUT)
    with journal.database.transaction() as session:
        row = session.get(AiJobStep, (journal.identity, "model:retry"))
        assert row.prior_intent is None and row.attempt_no == 1
        assert session.get(AiJobRecoveryState, journal.identity).model_calls == 1


def test_configured_per_result_limit_rolls_back_content_without_losing_intent(journal):
    limited = StepJournal(journal.database,
        journal.settings.model_copy(update={"job_recovery_checkpoint_max_bytes": 4096}),
        journal.identity, journal.lease.token, config_sha256=CONFIG, index_fingerprint=INDEX,
        revalidate=lambda *_: True)
    limited.begin("node:bounded", kind="node", input_sha256=INPUT)
    with pytest.raises(StepRejected, match="result_size_limit"):
        limited.complete("node:bounded", {"data": "x" * 4096})
    with journal.database.transaction() as session:
        row = session.get(AiJobStep, (journal.identity, "node:bounded"))
        assert row.status == "started" and row.result is None
