"""PostgreSQL-trigger policy and evidence-preserving maintenance; no providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from project_copilot.db.jobs import AiJob, AiRequestAttempt
from project_copilot.db.models import AgentContext, AiRequestReservation, LoginSession, PendingAction, Ticket
from project_copilot.db.operations import JobContentTombstone, JobRecoveryAudit, MaintenanceAudit
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ApprovalService, ContextRejected, PublicOperationLimit
from project_copilot.services.public_maintenance import MaintenanceRejected, cleanup, inspect_job, resolve_no_retry
from project_copilot.services.quotas import QuotaService
from project_copilot.services.worker_stop import sign_inventory
from project_copilot.config import Settings
from test_public_agent_approvals import app_for, public_headers, seed


def service(session, principal):
    return ApprovalService(session, principal=principal, enforce_ownership=True)


def test_context_admission_serializes_three_active_contexts(ticket_database):
    principals, _ = seed(ticket_database)
    alice = principals["alice"]

    def attempt(_):
        try:
            with ticket_database.transaction() as session:
                context = service(session, alice).create_context()
                assert 86390 < (context.expires_at - session.scalar(select(func.clock_timestamp()))).total_seconds() <= 86400
            return True
        except PublicOperationLimit:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 3
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AgentContext)) == 3


def test_context_hourly_limit_is_enforced_by_database(ticket_database):
    principals, _ = seed(ticket_database)
    alice = principals["alice"]
    for _ in range(5):
        with ticket_database.transaction() as session:
            service(session, alice).create_context()
            # Simulate passage of 24h on this synthetic fixture only. The public
            # API cannot update immutable contexts; its hourly history remains.
            session.execute(text("ALTER TABLE agent_contexts DISABLE TRIGGER context_guard"))
            session.execute(text("UPDATE agent_contexts SET expires_at=clock_timestamp()-interval '1 second'"))
            session.execute(text("ALTER TABLE agent_contexts ENABLE TRIGGER context_guard"))
    with pytest.raises(PublicOperationLimit):
        with ticket_database.transaction() as session:
            service(session, alice).create_context()


def test_proposal_pending_daily_and_ttl_are_database_enforced(ticket_database):
    principals, _ = seed(ticket_database)
    alice = principals["alice"]
    with ticket_database.transaction() as session:
        credentials = service(session, alice).create_context()
    ctx_id = credentials.context_id

    def attempt(_):
        try:
            with ticket_database.transaction() as session:
                svc = service(session, alice)
                context = svc.authenticate(ctx_id, credentials.context_token)
                result = svc.preview(context, TicketCreate(title="synthetic", description="isolated only"))
                assert 890 < (result.expires_at - result.created_at).total_seconds() <= 900
            return True
        except PublicOperationLimit:
            return False
    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(attempt, range(6))) == 3
    # Cancelling does not erase history or the daily count.
    with ticket_database.transaction() as session:
        session.execute(text("UPDATE pending_actions SET status='cancelled'"))
    for _ in range(7):
        assert attempt(0)
        with ticket_database.transaction() as session:
            session.execute(text("UPDATE pending_actions SET status='cancelled' WHERE status='pending'"))
    assert not attempt(0)
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 10
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_context_limit_api_returns_429_without_database_details(ticket_database):
    _, tokens = seed(ticket_database)
    token = tokens["alice"]
    with TestClient(app_for(ticket_database)) as client:
        for _ in range(3):
            assert client.post("/agent/contexts", cookies={"__Host-pc_session": token}, headers=public_headers(token)).status_code == 201
        denied = client.post("/agent/contexts", cookies={"__Host-pc_session": token}, headers=public_headers(token))
        assert denied.status_code == 429 and "public_operation_limit" not in denied.text


def test_service_does_not_trust_a_foreign_preloaded_context(ticket_database):
    principals, _ = seed(ticket_database)
    with ticket_database.transaction() as session:
        alice = service(session, principals["alice"])
        credential = alice.create_context()
        context = alice.authenticate(credential.context_id, credential.context_token)
        proposal = alice.preview(context, TicketCreate(title="private", description="owner only"))
        bob = service(session, principals["bob"])
        with pytest.raises(ContextRejected):
            bob.preview(context, TicketCreate(title="not allowed", description="not allowed"))
        with pytest.raises(ContextRejected):
            bob.decide(context, proposal.action_id, proposal.payload_fingerprint, approve=False)


def job(database, principal, *, state="succeeded", age_days=8, context_id=None):
    with database.transaction() as session:
        login = session.scalar(select(LoginSession).where(LoginSession.user_id == principal.id))
        request, _ = QuotaService(session, user_daily_limit=100, global_daily_limit=100, user_concurrency_limit=100).reserve(
            principal.id, f"synthetic-{state}-{age_days}-{context_id or 'no-context'}", "a" * 64, "rag")
        session.add(AiJob(id=request.id, login_session_id=login.id, context_id=context_id,
            payload={"question": "PRIVATE_QUESTION"}, result={"answer": "PRIVATE_RESULT"} if state == "succeeded" else None))
        session.flush()
        request.state = "running"
        request.provider_attempts = 1
        session.flush()
        request.state = state
        request.created_at = session.scalar(select(func.clock_timestamp())) - timedelta(days=age_days)
        request.finished_at = request.created_at if state in ("succeeded", "failed") else None
        session.add(AiRequestAttempt(user_id=principal.id, reservation_id=request.id, kind="rag", outcome="accepted",
                                   created_at=request.created_at))
        return request.id


def stopped(job_id):
    now = datetime.now(timezone.utc)
    proof = sign_inventory({"version": 1, "job_id": str(job_id), "resource_id": "public-demo-v2",
        "captured_at": now.isoformat(), "postgres_volume": "pc-public-v2-postgres",
        "review_sha256": "e" * 64,
        "workers": [{"container_id": "f" * 64, "status": "exited", "pid": 0, "running": False,
                     "restarting": False, "finished_at": (now - timedelta(seconds=1)).isoformat()}]}, "a" * 32)
    settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32,
        public_resource_id="public-demo-v2", auth_public_origin="https://demo.example")
    return {"worker_stop_proof": proof, "settings": settings}


def test_cleanup_preserves_unknown_evidence_and_idempotency(ticket_database):
    principals, _ = seed(ticket_database)
    alice, admin = principals["alice"], principals["admin"]
    done = job(ticket_database, alice)
    unknown = job(ticket_database, alice, state="unknown")
    with ticket_database.transaction() as session:
        counts = cleanup(session, admin.id)
        assert counts["terminal_content"] == 1
    with ticket_database.transaction() as session:
        assert session.get(AiJob, done).payload == {} and session.get(AiJob, done).result is None
        assert session.get(JobContentTombstone, done) is not None
        from project_copilot.api.jobs import _response
        status = _response(session.get(AiJob, done), session.get(AiRequestReservation, done))
        assert status["status"] == "succeeded" and status["result"] is None and status["result_expired"] is True
        assert session.get(AiJob, unknown).payload == {"question": "PRIVATE_QUESTION"}
        assert session.get(AiRequestReservation, unknown).state == "unknown"
        assert session.scalar(select(func.count()).select_from(AiRequestAttempt).where(AiRequestAttempt.reservation_id == unknown)) == 1
        assert session.scalar(select(func.count()).select_from(MaintenanceAudit)) == 1
        reused, replay = QuotaService(session).reserve(alice.id, "synthetic-succeeded-8-no-context", "a" * 64, "rag")
        assert replay and reused.id == done and reused.state == "succeeded"
        assert cleanup(session, admin.id)["terminal_content"] == 0


def test_recovery_requires_exclusive_execution_and_reconciled_evidence(ticket_database):
    principals, _ = seed(ticket_database)
    alice, admin = principals["alice"], principals["admin"]
    unknown = job(ticket_database, alice, state="unknown")
    with ticket_database.transaction() as session:
        inspection = inspect_job(session, admin.id, unknown)
        assert "PRIVATE_QUESTION" not in str(inspection)
    evidence = {"version": 1, "job_id": str(unknown), "inspection_sha256": inspection["inspection_sha256"],
        "original_execution_terminated": True, "business_verified": True,
        "provider_outcome": "completed_and_reconciled", "evidence_reference_sha256": "e" * 64,
        "decision": "terminal_no_retry"}
    with pytest.raises(MaintenanceRejected):
        with ticket_database.transaction() as session:
            resolve_no_retry(session, admin.id, unknown, evidence)
    with ticket_database.engine.connect() as execution:
        execution.execute(text("SELECT pg_advisory_lock_shared(604116220002)"))
        execution.commit()
        try:
            with pytest.raises(MaintenanceRejected):
                with ticket_database.transaction() as session:
                    resolve_no_retry(session, admin.id, unknown, evidence, **stopped(unknown))
        finally:
            execution.execute(text("SELECT pg_advisory_unlock_shared(604116220002)"))
            execution.commit()
    with ticket_database.transaction() as session:
        result = resolve_no_retry(session, admin.id, unknown, evidence, **stopped(unknown))
        assert result["state"] == "failed" and result["provider_result"] == "not_inferred"
    with ticket_database.transaction() as session:
        row = session.get(AiRequestReservation, unknown)
        assert row.state == "failed" and row.provider_attempts == 1
        assert session.scalar(select(JobRecoveryAudit).where(JobRecoveryAudit.job_id == unknown)).previous_state == "unknown"
        assert session.get(AiJob, unknown).payload == {"question": "PRIVATE_QUESTION"}
        assert cleanup(session, admin.id)["terminal_content"] == 0
        replay, reused = QuotaService(session).reserve(alice.id, "synthetic-unknown-8-no-context", "a" * 64, "rag")
        assert reused and replay.id == unknown and replay.state == "failed"
        assert not QuotaService(session).claim(alice.id, unknown)


def test_audit_activity_never_removes_uncertain_investigation_records(ticket_database):
    from project_copilot.services.ai_audit import record_attempt
    principals, _ = seed(ticket_database)
    unknown = job(ticket_database, principals["alice"], state="unknown")
    with ticket_database.transaction() as session:
        record_attempt(session, principals["admin"].id, "rag", "failed")
    with ticket_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestAttempt).where(AiRequestAttempt.reservation_id == unknown)) == 1


def test_context_cleanup_preserves_foreign_keys_and_all_proposals(ticket_database):
    principals, _ = seed(ticket_database)
    with ticket_database.transaction() as session:
        svc = service(session, principals["alice"])
        removable = svc.create_context()
        retained = svc.create_context()
        context = svc.authenticate(retained.context_id, retained.context_token)
        action = svc.preview(context, TicketCreate(title="preserve proposal", description="immutable history"))
        session.execute(text("ALTER TABLE agent_contexts DISABLE TRIGGER context_guard"))
        session.execute(text("UPDATE agent_contexts SET expires_at=clock_timestamp()-interval '9 days'"))
        session.execute(text("ALTER TABLE agent_contexts ENABLE TRIGGER context_guard"))
    with ticket_database.transaction() as session:
        counts = cleanup(session, principals["admin"].id)
        assert counts["unreferenced_contexts"] == 1
    with ticket_database.transaction() as session:
        assert session.get(AgentContext, removable.context_id) is None
        assert session.get(AgentContext, retained.context_id) is not None
        assert session.get(PendingAction, action.action_id) is not None


def test_cannot_modify_or_delete_recovery_audit(ticket_database):
    principals, _ = seed(ticket_database)
    unknown = job(ticket_database, principals["alice"], state="unknown")
    with ticket_database.transaction() as session:
        inspection = inspect_job(session, principals["admin"].id, unknown)
        evidence = {"version": 1, "job_id": str(unknown), "inspection_sha256": inspection["inspection_sha256"],
            "original_execution_terminated": True, "business_verified": True,
            "provider_outcome": "completed_and_reconciled", "evidence_reference_sha256": "e" * 64, "decision": "terminal_no_retry"}
        resolve_no_retry(session, principals["admin"].id, unknown, evidence, **stopped(unknown))
    with pytest.raises(DBAPIError):
        with ticket_database.transaction() as session:
            session.execute(text("DELETE FROM job_recovery_audits"))
