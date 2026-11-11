"""Automatic checkpoint eligibility in owned PostgreSQL; no provider/model HTTP."""
import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import func, select

from project_copilot.agent.proposals import ProposalBackend
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.models import AiRequestReservation, PendingAction
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState, AiJobStep
from project_copilot.infrastructure.call_accounting import account_job, record_provider_attempt
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, track_execution
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.auth import AuthService
from project_copilot.services.job_recovery import RecoveryBudgetExceeded, recover_job, recovery_budget
from project_copilot.services.job_steps import StepJournal, StepUncertain, fingerprint, normalize_model_message
from test_job_recovery_postgres import recovery_setup


def _manifest(settings):
    spec = DenseIndexSpec(collection_name=settings.qdrant_collection_name,
        embedding_model=settings.embedding_model, dimensions=settings.embedding_expected_dimensions,
        splitter_version=SplitConfig().index_version)
    store = ManifestStore(settings.manifest_dir, spec)
    if not store.path.exists():
        store._publish(store.load())
    return store


@pytest.fixture
def cached(recovery_setup):
    from project_copilot.infrastructure.recovery_runtime import checkpoint_bindings
    setup = recovery_setup
    identity = setup.reserve()
    job = setup.claim(identity)
    manifest = _manifest(setup.settings)
    config, index = checkpoint_bindings(setup.settings)
    journal = StepJournal(setup.database, setup.settings, identity, job.execution_lease.token,
        config_sha256=config, index_fingerprint=index, revalidate=lambda *_: True)
    answer = {"status": "insufficient_evidence", "answer": "saved controlled answer", "source_ids": []}
    result = normalize_model_message(AIMessage(content=json.dumps(answer), response_metadata={
        "model": "synthetic-model", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}))
    assert journal.begin("agent.model.1", kind="model", input_sha256=fingerprint({"question": "controlled recovery"})).decision == "execute"
    journal.complete("agent.model.1", result)
    with setup.database.transaction() as session:
        # A synthetic fully observed provider boundary; no HTTP was issued.
        session.get(AiRequestReservation, identity).provider_attempts = 1
    return SimpleNamespace(setup=setup, identity=identity, job=job, manifest=manifest,
        journal=journal, config=config, index=index, result=result)


def _assert_no_repeat(value, expected="model_repeat_required"):
    value.job.execution_lease.close()
    decision = recover_job(value.setup.database, value.setup.settings, value.identity)
    assert decision.decision == "blocked" and decision.reason == expected
    with value.setup.database.transaction() as session:
        assert session.get(AiJobRecoveryState, value.identity).current_attempt_no == 1
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 1
        assert session.get(AiRequestReservation, value.identity).state == "running"
        assert session.scalar(select(func.count()).select_from(AiJobExecutionAttempt)) == 0


def test_complete_matching_real_step_receipt_automatically_admits_checkpoint_recovery(cached):
    value = cached
    value.job.execution_lease.close()
    decision = recover_job(value.setup.database, value.setup.settings, value.identity)
    assert decision.decision == "requeued" and decision.reason == "checkpoint"
    with value.setup.database.transaction() as session:
        proof = session.get(AiJobExecutionAttempt, (value.identity, 1))
        assert proof.reason == "checkpoint" and proof.provider_attempts == 1
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 1
        assert session.get(AiJobStep, (value.identity, "agent.model.1")).result == value.result


@pytest.mark.parametrize("kind,status", [("model", "started"), ("tool", "started"), ("node", "started"),
                                         ("model", "unknown"), ("tool", "unknown")])
def test_any_unfinished_later_intent_prevents_automatic_paid_replay(cached, kind, status):
    value = cached
    value.journal.begin("later.intent", kind=kind, input_sha256=fingerprint({"later": True}))
    if status == "unknown":
        value.journal.mark_unknown("later.intent")
    _assert_no_repeat(value)


@pytest.mark.parametrize("changed", ["config", "index", "payload", "writer"])
def test_changed_binding_or_corrupt_receipt_never_authorizes_paid_automatic_replay(cached, changed):
    value = cached
    if changed == "index":
        value.manifest.record(value.manifest.load(), CompletedDocument(document_id=uuid4(),
            content_hash="a" * 64, source_name="synthetic-changed-index.txt", chunk_ids=(uuid4(),)))
    else:
        with value.setup.database.transaction() as session:
            row = session.get(AiJobStep, (value.identity, "agent.model.1"))
            if changed == "config":
                row.config_sha256 = "f" * 64 if row.config_sha256 != "f" * 64 else "e" * 64
            elif changed == "payload":
                row.result = {**row.result, "content": "tampered cached content"}
            else:
                row.execution_token = uuid4()
    _assert_no_repeat(value)


def test_unavailable_manifest_cannot_supply_checkpoint_recovery_proof(cached):
    value = cached
    value.manifest.path.write_text("synthetic incomplete manifest", encoding="utf-8")
    _assert_no_repeat(value)


def test_expired_checkpoint_can_recover_locally_but_cannot_initiate_another_provider(cached):
    value = cached
    with value.setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, value.identity)
        state.budget_started_at -= timedelta(hours=1)
        state.deadline_at -= timedelta(hours=1)
        deadline = state.deadline_at
    value.job.execution_lease.close()
    decision = recover_job(value.setup.database, value.setup.settings, value.identity)
    assert decision.reason == "checkpoint" and decision.decision == "requeued"
    second = value.setup.claim(value.identity)
    with value.setup.database.transaction() as session:
        assert recovery_budget(session, value.identity, second.execution_lease.token, allow_expired=True).deadline_at == deadline
    tracker = ExecutionTracker(value.setup.database, value.identity, second.execution_lease.token)
    with track_execution(tracker), account_job(value.setup.database, value.setup.user_id, value.identity,
        settings=value.setup.settings), pytest.raises(RecoveryBudgetExceeded):
        record_provider_attempt()
    tracker.drain()
    with value.setup.database.transaction() as session:
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 1
        assert session.get(AiJobRecoveryState, value.identity).model_calls == 1


@pytest.mark.parametrize("uncertainty", ["provider_open", "provider_uncertain"])
def test_complete_receipts_do_not_override_uncertain_provider_proof(cached, uncertainty):
    value = cached
    with value.setup.database.transaction() as session:
        row = session.get(AiJobExecution, value.identity)
        if uncertainty == "provider_open":
            row.provider_open = 1
        else:
            row.provider_uncertain = True
    _assert_no_repeat(value, uncertainty)


def test_stable_proposal_proves_original_preview_even_when_tool_receipt_was_lost(recovery_setup):
    setup = recovery_setup
    with setup.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(setup.token)
        context = ApprovalService(session, principal=principal, enforce_ownership=True).create_context()
    identity = setup.reserve("agent", context_id=context.context_id)
    job = setup.claim(identity)
    backend = ProposalBackend(setup.settings, setup.database, context.context_id, None,
                              auth_session_id=setup.login_id)
    tracker = ExecutionTracker(setup.database, identity, job.execution_lease.token)
    with track_execution(tracker):
        result = asyncio.run(backend.propose_ticket("original preview", "one stable business operation"))
    tracker.drain()
    with setup.database.transaction() as session:
        session.get(AiRequestReservation, identity).provider_attempts = 1
        session.add(AiJobStep(job_id=identity, step_key="agent.tool.propose_ticket", kind="tool",
            status="started", input_sha256="a" * 64, config_sha256="b" * 64, attempt_no=1,
            execution_token=job.execution_lease.token))
    job.execution_lease.close()
    decision = recover_job(setup.database, setup.settings, identity)
    assert decision.decision == "requeued" and decision.reason == "checkpoint"
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
        assert session.get(PendingAction, UUID(result["data"]["action_id"])).status == "pending"
        assert session.get(AiRequestReservation, identity).provider_attempts == 1


def test_explicit_repeat_policy_can_restart_only_readonly_paid_tool_intent(recovery_setup):
    from project_copilot.infrastructure.recovery_runtime import checkpoint_bindings
    setup = recovery_setup
    identity = setup.reserve()
    job = setup.claim(identity)
    _manifest(setup.settings)
    config, index = checkpoint_bindings(setup.settings)
    journal = StepJournal(setup.database, setup.settings, identity, job.execution_lease.token,
        config_sha256=config, index_fingerprint=index, revalidate=lambda *_: True)
    request_hash = fingerprint({"query": "synthetic embedding retrieval"})
    journal.begin("rag.retrieve", kind="tool", input_sha256=request_hash)
    with setup.database.transaction() as session:
        session.get(AiRequestReservation, identity).provider_attempts = 1
    job.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "model_repeat_required"
    assert recover_job(setup.database, setup.settings, identity, allow_model_repeat=True).reason == "model_repeat"
    second = setup.claim(identity)
    replay = StepJournal(setup.database, setup.settings, identity, second.execution_lease.token,
        config_sha256=config, index_fingerprint=index, revalidate=lambda *_: True)
    with pytest.raises(StepUncertain):
        replay.begin("rag.retrieve", kind="tool", input_sha256=request_hash)
    assert replay.begin("rag.retrieve", kind="tool", input_sha256=request_hash,
                        retry_policy="readonly_tool").decision == "execute"
    with setup.database.transaction() as session:
        assert session.get(AiJobRecoveryState, identity).tool_calls == 2
        assert session.get(AiJobStep, (identity, "rag.retrieve")).prior_intent["execution_token"] == str(job.execution_lease.token)
        assert session.get(AiRequestReservation, identity).provider_attempts == 1


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_new_step_count_limit_reports_its_precise_budget_without_committing_intent(cached, kind):
    value = cached
    with value.setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, value.identity)
        maximum = getattr(state, "max_" + kind + "_calls")
        setattr(state, kind + "_calls", maximum)
    key = "new." + kind + ".at_limit"
    with pytest.raises(RecoveryBudgetExceeded) as error:
        value.journal.begin(key, kind=kind, input_sha256=fingerprint({"kind": kind}))
    assert error.value.reason == kind + "_budget"
    with value.setup.database.transaction() as session:
        state = session.get(AiJobRecoveryState, value.identity)
        assert getattr(state, kind + "_calls") == maximum
        assert session.get(AiJobStep, (value.identity, key)) is None
