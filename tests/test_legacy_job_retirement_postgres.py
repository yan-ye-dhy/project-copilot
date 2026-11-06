"""Exact historical exception on owned, random PostgreSQL databases only.

The UUID/timestamp are the approved target, but accounts, payloads, stopped-worker
inventories and provider reviews here are synthetic. No live recovery is run.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from project_copilot.config import Settings
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob, AiRequestAttempt
from project_copilot.db.models import AgentContext, AiRequestReservation, LoginSession, PendingAction, Ticket
from project_copilot.db.operations import JobRecoveryAudit, MaintenanceAudit
from project_copilot.db.release import ReleaseControl
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import public_maintenance as maintenance
from project_copilot.services import release_control as release
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.auth import AuthService
from project_copilot.services.execution_slots import reclaim_ended
from project_copilot.services.quotas import QuotaExceeded, QuotaService
from project_copilot.services.worker_stop import sign_inventory


TARGET = UUID("3f72446c-6aed-470f-85da-774004b97555")
CREATED = datetime(2026, 11, 5, 9, 38, 17, 911814, tzinfo=timezone.utc)
USAGE = {"prompt_tokens": 1389, "completion_tokens": 102, "total_tokens": 1491,
         "prompt_cache_hit_tokens": 896, "prompt_cache_miss_tokens": 493}
RISK = "ACCEPT UNVERIFIED PROVIDER RISK 3f72446c-6aed-470f-85da-774004b97555"
PASSWORD = "synthetic-legacy-retirement-password-583"
ARTIFACT = "e" * 64


@pytest.fixture
def legacy(ticket_database, tmp_path):
    db = ticket_database
    settings = Settings(_env_file=None, public_demo=True, environment="production",
        public_proxy_secret="synthetic_legacy_secret_" * 3, public_resource_id="public-demo-v2",
        auth_public_origin="https://legacy.example", manifest_dir=tmp_path / "manifests",
        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
        tracing_enabled=False)
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=PASSWORD))
        alice = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        bob = auth.register(RegisterInput(username="bob", password=PASSWORD), open_registration=True)
        token = auth.login(LoginInput(username="alice", password=PASSWORD)).token
        _, login = auth.authenticate(token)
        context = ApprovalService(session, principal=alice, enforce_ownership=True).create_context()
        admin_id, user_id, other_id, login_id = admin.id, alice.id, bob.id, login.id
        context_id = context.context_id
        # Set historical times only in this synthetic fixture. Public context
        # fields remain immutable; the service never performs this UPDATE.
        session.execute(text("ALTER TABLE agent_contexts DISABLE TRIGGER context_guard"))
        session.execute(text("UPDATE agent_contexts SET created_at=:created,expires_at=:expires WHERE id=:identity"),
            {"created": CREATED - timedelta(minutes=5), "expires": CREATED + timedelta(hours=24), "identity": context_id})
        session.execute(text("ALTER TABLE agent_contexts ENABLE TRIGGER context_guard"))
        grant_id = uuid4()
        row = AiRequestReservation(id=TARGET, user_id=user_id, personal_demo_grant_id=grant_id,
            idempotency_key="synthetic-legacy-known-request", payload_fingerprint="a" * 64,
            kind="agent", utc_day=CREATED.date(), created_at=CREATED, state="unknown",
            provider_attempts=3, usage=deepcopy(USAGE), cost_status="unknown")
        session.add(row)
        session.flush()
        session.add(AiJob(id=TARGET, login_session_id=login_id, context_id=context_id,
            payload={"question": "synthetic legacy question, never replay"}, result=None))
        session.add(AiRequestAttempt(user_id=user_id, reservation_id=TARGET, kind="agent", outcome="accepted", created_at=CREATED))
    return SimpleNamespace(db=db, settings=settings, admin=admin_id, user=user_id, other=other_id,
        login=login_id, context=context_id, grant=grant_id)


def receipt(setup):
    with setup.db.transaction() as session:
        inspected = maintenance.inspect_job(session, setup.admin, TARGET)
    return {"version": 1, "job_id": str(TARGET), "expected_owner_user_id": str(setup.user),
        "expected_context_id": str(setup.context), "created_at": CREATED.isoformat(),
        "provider_attempts": 3, "inspection_sha256": inspected["inspection_sha256"],
        "original_owner_verified": True, "original_execution_terminated": True, "business_verified": True,
        "provider_outcome": "unverified", "evidence_reference_sha256": ARTIFACT,
        "decision": "retire_legacy_slot_no_retry_v1"}


def stop_proof(setup, *, change=None):
    now = datetime.now(timezone.utc)
    inventory = {"version": 1, "job_id": str(TARGET), "resource_id": "public-demo-v2",
        "captured_at": now.isoformat(), "postgres_volume": "pc-public-v2-postgres", "review_sha256": ARTIFACT,
        "workers": [{"container_id": "f" * 64, "status": "exited", "pid": 0, "running": False,
            "restarting": False, "finished_at": (now - timedelta(seconds=1)).isoformat()}]}
    if change:
        change(inventory)
    return sign_inventory(inventory, setup.settings.public_proxy_secret.get_secret_value())


def retire(setup, evidence=None, *, actor=None, risk=RISK, proof="fresh", identity=TARGET):
    evidence = receipt(setup) if evidence is None else evidence
    proof = stop_proof(setup) if proof == "fresh" else proof
    with setup.db.transaction() as session:
        return maintenance.retire_legacy_unknown(session, actor or setup.admin, identity, evidence,
            risk_acceptance=risk, worker_stop_proof=proof, settings=setup.settings)


def snapshot(setup):
    with setup.db.transaction() as session:
        row = session.get(AiRequestReservation, TARGET)
        values = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
        job = session.get(AiJob, TARGET)
        return {"reservation": values, "payload": deepcopy(job.payload), "result": deepcopy(job.result),
            "attempts": session.scalar(select(func.count()).select_from(AiRequestAttempt)),
            "requests": session.scalar(select(func.count()).select_from(AiRequestReservation)),
            "recoveries": session.scalar(select(func.count()).select_from(JobRecoveryAudit)),
            "maintenance": session.scalar(select(func.count()).select_from(MaintenanceAudit))}


def assert_held_without_audit(setup):
    current = snapshot(setup)
    assert current["reservation"]["slot_released_at"] is None
    assert current["recoveries"] == current["maintenance"] == 0


def test_legacy_unknown_stays_held_after_new_grant_and_automatic_reaper(legacy, monkeypatch):
    with legacy.db.transaction() as session:
        assert reclaim_ended(session, legacy.settings, user_id=legacy.user) == 0
    monkeypatch.setattr(release, "code_fingerprint", lambda: "synthetic-legacy-code")
    monkeypatch.setattr(release, "config_fingerprint", lambda _: "synthetic-legacy-configuration")
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    with legacy.db.transaction() as session:
        release.change_state(session, legacy.settings, actor_id=legacy.admin, operation="PERSONAL_DEMO",
            personal_user_id=legacy.user, duration_minutes=90, reason="controlled new grant")
    # The old unresolved execution still consumes one of the new three slots.
    # A new grant must not silently drop that slot from concurrency accounting.
    with legacy.db.transaction() as session:
        for n in range(2):
            QuotaService(session, settings=legacy.settings).reserve(legacy.user,
                f"new-grant-available-{n}", "b" * 64, "rag")
    with pytest.raises(QuotaExceeded) as error, legacy.db.transaction() as session:
        QuotaService(session, settings=legacy.settings).reserve(legacy.user, "synthetic-new-request", "b" * 64, "rag")
    assert error.value.reason == "user_concurrency"
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("risk", [None, "", "terminal_no_retry", "ACCEPT UNVERIFIED PROVIDER RISK wrong-job"])
def test_exact_interactive_risk_acceptance_is_required(legacy, risk):
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, risk=risk)
    assert_held_without_audit(legacy)


def test_non_admin_cannot_accept_provider_risk(legacy):
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, actor=legacy.user)
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(captured_at=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()),
    lambda value: value["workers"][0].update(running=True, status="running", pid=4321),
    lambda value: value["workers"][0].update(finished_at=(CREATED - timedelta(seconds=1)).isoformat()),
    lambda value: value.update(postgres_volume="wrong-volume"),
    lambda value: value.update(review_sha256="d" * 64),
])
def test_fresh_resource_bound_worker_stop_proof_is_required(legacy, mutation):
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, proof=stop_proof(legacy, change=mutation))
    assert_held_without_audit(legacy)


def test_missing_stop_proof_is_rejected(legacy):
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, proof=None)
    assert_held_without_audit(legacy)


def test_real_mock_worker_thread_holding_execution_lease_blocks_retirement(legacy):
    started, finish = Event(), Event()
    def local_work():
        with release.execution_lease(legacy.db):
            started.set()
            assert finish.wait(10)
    thread = Thread(target=local_work)
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(maintenance.MaintenanceRejected):
            retire(legacy)
        assert_held_without_audit(legacy)
    finally:
        finish.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_release_must_be_closed_even_with_stopped_inventory(legacy):
    with legacy.db.transaction() as session:
        control = session.get(ReleaseControl, 1)
        control.state, control.worker_enabled = "PERSONAL_DEMO", True
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy)
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("key,value", [
    ("job_id", "00000000-0000-0000-0000-000000000001"),
    ("expected_owner_user_id", "00000000-0000-0000-0000-000000000002"),
    ("expected_context_id", "00000000-0000-0000-0000-000000000003"),
    ("created_at", "2026-11-05T09:38:17.911815+00:00"),
    ("provider_attempts", 2), ("inspection_sha256", "0" * 64),
    ("original_owner_verified", False), ("original_execution_terminated", False),
    ("business_verified", False), ("provider_outcome", "completed_and_reconciled"),
])
def test_exact_receipt_and_current_inspection_are_required(legacy, key, value):
    evidence = receipt(legacy)
    evidence[key] = value
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, evidence)
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("field,value", [
    ("kind", "rag"), ("provider_attempts", 4), ("personal_demo_grant_id", None),
    ("usage", {**USAGE, "total_tokens": 1492}),
])
def test_current_target_must_match_original_identity_and_counters(legacy, field, value):
    with legacy.db.transaction() as session:
        setattr(session.get(AiRequestReservation, TARGET), field, value)
    # Even a fresh receipt cannot endorse a changed immutable baseline.
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy)
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("change", ["result", "session_owner", "context_owner", "execution"])
def test_result_owner_and_missing_execution_record_are_checked(legacy, change):
    with legacy.db.transaction() as session:
        if change == "result":
            session.get(AiJob, TARGET).result = {"answer": "unexpected durable result"}
        elif change == "session_owner":
            session.get(LoginSession, legacy.login).user_id = legacy.other
        elif change == "context_owner":
            session.execute(text("ALTER TABLE agent_contexts DISABLE TRIGGER context_guard"))
            session.execute(text("UPDATE agent_contexts SET owner_user_id=:other WHERE id=:identity"),
                {"other": legacy.other, "identity": legacy.context})
            session.execute(text("ALTER TABLE agent_contexts ENABLE TRIGGER context_guard"))
        else:
            session.add(AiJobExecution(job_id=TARGET, token=uuid4(), lease_inode=123))
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy)
    assert_held_without_audit(legacy)


@pytest.mark.parametrize("side_effect", ["pending_action", "orphan_ticket"])
def test_potential_business_side_effects_prevent_retirement(legacy, side_effect):
    with legacy.db.transaction() as session:
        if side_effect == "pending_action":
            session.add(PendingAction(context_id=legacy.context, owner_user_id=legacy.user,
                title="synthetic unresolved proposal", description="must not be discarded",
                payload_fingerprint="b" * 64, status="pending", expires_at=CREATED + timedelta(hours=12)))
        else:
            session.add(Ticket(owner_user_id=legacy.user, creation_key=uuid4(),
                title="synthetic orphan business result", description="unreconciled creation key"))
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy)
    assert_held_without_audit(legacy)


def test_only_the_exact_approved_job_id_can_use_the_exception(legacy):
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, identity=uuid4())
    assert_held_without_audit(legacy)


def test_exact_retirement_only_releases_slot_and_preserves_all_other_data(legacy):
    before = snapshot(legacy)
    result = retire(legacy)
    assert result["state"] == "unknown" and result["slot_released"] is True
    assert result["provider_outcome"] == "unverified" and result["replay_permitted"] is False
    assert result["already_retired"] is False and result["quota_before"] == result["quota_after"]
    after = snapshot(legacy)
    released = after["reservation"].pop("slot_released_at")
    assert released is not None
    before["reservation"].pop("slot_released_at")
    assert after["reservation"] == before["reservation"]
    assert (after["payload"], after["result"], after["attempts"], after["requests"]) == (
        before["payload"], before["result"], before["attempts"], before["requests"])
    assert after["recoveries"] == after["maintenance"] == 1
    with legacy.db.transaction() as session:
        audit = session.scalar(select(JobRecoveryAudit).where(JobRecoveryAudit.job_id == TARGET))
        metadata = session.scalar(select(MaintenanceAudit)).counts
        assert audit.previous_state == "unknown" and audit.prior_usage == USAGE and audit.provider_attempts == 3
        assert metadata["operation"] == "legacy_unknown_slot_retirement_v1"
        assert metadata["provider_outcome"] == "unverified" and metadata["recovery_audit_id"] == str(audit.id)
        assert metadata["risk_acceptance"]["actor_user_id"] == str(legacy.admin)
        assert metadata["risk_acceptance"]["accepted_at"]
        assert metadata["preserved"] and "password" not in str(metadata).lower()
        row, reused = QuotaService(session).reserve(legacy.user, "synthetic-legacy-known-request", "a" * 64, "agent")
        assert reused and row.id == TARGET and not QuotaService(session).claim(legacy.user, TARGET)
        assert reclaim_ended(session, legacy.settings, user_id=legacy.user) == 0
        assert session.get(AiJobExecution, TARGET) is None
        admitted, reused = QuotaService(session).reserve(legacy.user, "synthetic-after-retirement", "c" * 64, "rag")
        assert not reused and admitted.id != TARGET


def test_repeat_with_same_receipt_is_audited_once_and_keeps_original_timestamp(legacy):
    evidence = receipt(legacy)
    first = retire(legacy, evidence)
    before = snapshot(legacy)
    repeated = retire(legacy, evidence)
    assert repeated["already_retired"] is True and repeated["audit_id"] == first["audit_id"]
    assert snapshot(legacy) == before


def test_repeat_does_not_hide_external_changes_after_retirement(legacy):
    evidence = receipt(legacy)
    retire(legacy, evidence)
    with legacy.db.transaction() as session:
        session.get(AiRequestReservation, TARGET).provider_attempts = 4
    with pytest.raises(maintenance.MaintenanceRejected):
        retire(legacy, evidence)
    current = snapshot(legacy)
    assert current["recoveries"] == current["maintenance"] == 1


def test_audit_commit_failure_rolls_back_slot_release_and_both_audits(legacy):
    evidence = receipt(legacy)
    before = snapshot(legacy)
    with pytest.raises(DBAPIError):
        with legacy.db.transaction() as session:
            def fail_commit(active):
                active.execute(text("SELECT 1 / 0"))
            event.listen(session, "before_commit", fail_commit, once=True)
            maintenance.retire_legacy_unknown(session, legacy.admin, TARGET, evidence,
                risk_acceptance=RISK, worker_stop_proof=stop_proof(legacy), settings=legacy.settings)
    assert snapshot(legacy) == before


def test_concurrent_duplicate_operators_never_write_two_recoveries(legacy):
    evidence = receipt(legacy)
    start = Barrier(2)
    def operate(_):
        start.wait(timeout=5)
        try:
            return retire(legacy, evidence)
        except maintenance.MaintenanceRejected:
            return None  # Nonblocking maintenance lease may reject the competitor.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(operate, range(2)))
    assert any(result and result["slot_released"] for result in results)
    current = snapshot(legacy)
    assert current["recoveries"] == current["maintenance"] == 1
    assert current["reservation"]["state"] == "unknown"


def test_generic_recovery_does_not_gain_unverified_provider_exception(legacy):
    evidence = receipt(legacy)
    for field in ("expected_owner_user_id", "expected_context_id", "created_at", "provider_attempts", "original_owner_verified"):
        evidence.pop(field)
    evidence["decision"] = "terminal_no_retry"
    with pytest.raises(maintenance.MaintenanceRejected):
        with legacy.db.transaction() as session:
            maintenance.resolve_no_retry(session, legacy.admin, TARGET, evidence,
                worker_stop_proof=stop_proof(legacy), settings=legacy.settings)
    assert_held_without_audit(legacy)
