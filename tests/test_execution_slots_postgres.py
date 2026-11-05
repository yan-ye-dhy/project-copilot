"""Independent PG/OS execution evidence; every provider and workload is controlled.

Each test owns a fresh DB in the dedicated test cluster and private lease files.
These are not live demo recovery operations, billing receipts or model calls.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import select as os_select
import subprocess
import sys
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from uuid import UUID, uuid4

from alembic import command
import pytest
from sqlalchemy import func, select, text

from project_copilot.config import Settings
from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob
from project_copilot.db.migrations import check_model_drift, migration_config, upgrade
from project_copilot.db.models import AiRequestReservation, PendingAction, Ticket
from project_copilot.infrastructure.call_accounting import account_job
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, provider_request, track_execution, tracked_submit, tracked_to_thread
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import ai_worker, release_control as release
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.auth import AuthService
from project_copilot.services.execution_slots import (
    ExecutionFenced, ExecutionLease, guard_execution, mark_local_done_and_release,
    provider_finished, provider_started, reclaim_ended, register_execution,
)
from project_copilot.services.quotas import QuotaExceeded, QuotaService

USAGE = {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}
PASSWORD = "synthetic-slot-password-123"


@pytest.fixture
def slots(ticket_database, tmp_path, monkeypatch):
    database = ticket_database
    settings = Settings(_env_file=None, public_demo=True, environment="production",
        public_proxy_secret="synthetic-slot-secret-" * 3,
        auth_public_origin="https://personal.example",
        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
        manifest_dir=tmp_path / "manifests", tracing_enabled=False)
    # The release/account/session/quota SQL is real. Only deployment fingerprints
    # and dependency health are controlled; no public evidence is fabricated.
    monkeypatch.setattr(release, "code_fingerprint", lambda: "synthetic-slot-code")
    monkeypatch.setattr(release, "config_fingerprint", lambda _: "synthetic-slot-settings")
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    with database.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=PASSWORD))
        user = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        issued = auth.login(LoginInput(username="alice", password=PASSWORD))
        _, login = auth.authenticate(issued.token)
        admin_id, user_id, login_id = admin.id, user.id, login.id
        release.change_state(session, settings, actor_id=admin_id, operation="PERSONAL_DEMO",
            personal_user_id=user_id, duration_minutes=90, reason="synthetic execution slot test")
        release.change_state(session, settings, actor_id=admin_id, operation="worker-enable",
            reason="synthetic worker enable")
    leases = []
    def reserve(kind="rag", key=None, context_id=None):
        key = key or "slot-test-" + uuid4().hex
        with database.transaction() as session:
            row, reused = QuotaService(session, settings=settings).reserve(user_id, key, "f" * 64, kind)
            if not reused:
                session.add(AiJob(id=row.id, login_session_id=login_id, context_id=context_id,
                                  payload={"question": "controlled"}))
            return row.id, key
    def enroll(kind="rag", context_id=None):
        identity, key = reserve(kind, context_id=context_id)
        lease = ExecutionLease.create(settings.manifest_dir, identity)
        leases.append(lease)
        with database.transaction() as session:
            assert QuotaService(session).claim(user_id, identity)
            row = session.get(AiRequestReservation, identity)
            register_execution(session, row, lease)
        return identity, key, lease
    def reap():
        with database.transaction() as session:
            return reclaim_ended(session, settings, user_id=user_id)
    yield SimpleNamespace(database=database, settings=settings, admin_id=admin_id, user_id=user_id,
        login_id=login_id, token=issued.token, reserve=reserve, enroll=enroll, reap=reap, leases=leases)
    for lease in leases:
        lease.close()


def state(setup, identity):
    with setup.database.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        execution = session.get(AiJobExecution, identity)
        return SimpleNamespace(state=row.state, released=row.slot_released_at, attempts=row.provider_attempts,
            usage=row.usage, cost=row.cost_status, key=row.idempotency_key, grant=row.personal_demo_grant_id,
            day=row.utc_day, finished=row.finished_at, token=execution.token if execution else None,
            reason=execution.release_reason if execution else None,
            provider_open=execution.provider_open if execution else None,
            provider_uncertain=execution.provider_uncertain if execution else None)


def test_empty_migration_and_orm_match_execution_evidence(ticket_database):
    check_model_drift(ticket_database)
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0014_execution_slots"


def test_upgrade_releases_existing_terminal_only_and_never_invents_unknown_proof(postgres_database, tmp_path):
    db = postgres_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0013_job_events")
    with db.transaction() as session:
        user = AuthService(session).register(RegisterInput(username="olduser", password=PASSWORD), open_registration=True)
        user_id = user.id
        identities = {}
        for status in ("reserved", "running", "unknown", "succeeded", "failed"):
            identity = uuid4(); identities[status] = identity
            session.execute(text("""INSERT INTO ai_request_reservations
                (id,user_id,idempotency_key,payload_fingerprint,kind,state,utc_day,finished_at,provider_attempts,usage)
                VALUES (:id,:user,:key,:fingerprint,'rag',:status,CURRENT_DATE,:finished,3,CAST(:usage AS jsonb))"""),
                {"id": identity, "user": user_id, "key": "legacy-" + status, "fingerprint": "a" * 64,
                 "status": status, "finished": datetime.now(timezone.utc) if status in {"succeeded", "failed"} else None,
                 "usage": json.dumps(USAGE)})
    upgrade(db)
    check_model_drift(db)
    with db.transaction() as session:
        for status, identity in identities.items():
            row = session.get(AiRequestReservation, identity)
            assert row.state == status and row.provider_attempts == 3 and row.usage == USAGE
            assert (row.slot_released_at is not None) == (status in {"succeeded", "failed"})
            assert session.get(AiJobExecution, identity) is None
        assert reclaim_ended(session, SimpleNamespace(manifest_dir=tmp_path), user_id=user_id) == 0


@pytest.mark.parametrize("outcome", ["succeeded", "unknown"])
def test_proven_drain_releases_slot_without_changing_delivery_usage_or_idempotency(slots, outcome):
    identity, key, lease = slots.enroll()
    with slots.database.transaction() as session:
        QuotaService(session).finish(slots.user_id, identity, outcome=outcome, usage=USAGE)
    before = state(slots, identity)
    assert before.released is None
    assert mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    after = state(slots, identity)
    assert after.released is not None and after.reason == "drained"
    assert (after.state, after.usage, after.attempts, after.cost, after.key, after.day, after.grant, after.finished) == (
        before.state, before.usage, before.attempts, before.cost, before.key, before.day, before.grant, before.finished)
    with slots.database.transaction() as session:
        row, reused = QuotaService(session, settings=slots.settings).reserve(slots.user_id, key, "f" * 64, "rag")
        assert reused and row.id == identity and not QuotaService(session).claim(slots.user_id, identity)
    assert slots.reserve()[0] != identity


def test_completed_provider_receipt_allows_unknown_release_after_provider_has_ended(slots):
    identity, _, lease = slots.enroll()
    with slots.database.transaction() as session:
        provider_started(session, identity, lease.token)
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    assert state(slots, identity).released is None
    assert slots.reap() == 0
    assert provider_finished(slots.database, identity, lease.token, completed=True)
    assert mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    assert state(slots, identity).state == "unknown"


def test_premature_local_done_cannot_release_an_open_provider(slots):
    identity, _, lease = slots.enroll()
    with slots.database.transaction() as session:
        provider_started(session, identity, lease.token)
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    assert not mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    lease.close()
    assert slots.reap() == 0
    snapshot = state(slots, identity)
    assert snapshot.released is None and snapshot.provider_open == 1


def test_lost_provider_response_retains_slot_even_after_physical_lease_closes(slots):
    identity, _, lease = slots.enroll()
    with slots.database.transaction() as session:
        provider_started(session, identity, lease.token)
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    assert provider_finished(slots.database, identity, lease.token, completed=False)
    assert not mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    lease.close()
    assert slots.reap() == 0
    snapshot = state(slots, identity)
    assert snapshot.released is None and snapshot.provider_uncertain and snapshot.provider_open == 0
    with pytest.raises(QuotaExceeded) as error:
        slots.reserve()
    assert error.value.reason == "user_concurrency"


@pytest.mark.parametrize("bad_final", [False, True])
def test_worker_returns_real_completion_evidence_even_when_agent_final_is_unknown(slots, bad_final):
    identity, key = slots.reserve(kind="agent")
    calls = []
    def runner(*_):
        with provider_request() as attempt:
            calls.append("controlled-provider")
            attempt.completed()
        return SimpleNamespace(status="failed" if bad_final else "insufficient_evidence",
            failure_type="invalid_final" if bad_final else None, events=[], model_events=[], usage=USAGE,
            model_dump=lambda **_: {"status": "insufficient_evidence", "answer": "controlled"})
    assert ai_worker.process_one(slots.database, slots.settings, runner=runner)
    assert not ai_worker.process_one(slots.database, slots.settings, runner=runner)
    snapshot = state(slots, identity)
    assert snapshot.state == ("unknown" if bad_final else "succeeded")
    assert snapshot.released is not None and snapshot.attempts == 1 and snapshot.usage == USAGE
    assert calls == ["controlled-provider"]
    assert slots.reserve(kind="agent", key=key)[0] == identity


def test_worker_does_not_release_while_real_background_future_is_still_running(slots):
    identity, _ = slots.reserve()
    started, finish, runner_returned = Event(), Event(), Event()
    pool = ThreadPoolExecutor(max_workers=1)
    errors = []
    def actual_work():
        started.set()
        assert finish.wait(15)
    def runner(*_):
        tracked_submit(pool, actual_work)
        assert started.wait(5)
        runner_returned.set()
        raise ValueError("controlled final failure while background is alive")
    def work():
        try:
            ai_worker.process_one(slots.database, slots.settings, runner=runner)
        except BaseException as exc:
            errors.append(type(exc).__name__)
    thread = Thread(target=work)
    thread.start()
    try:
        assert runner_returned.wait(8)
        assert state(slots, identity).released is None
        assert slots.reap() == 0
        with pytest.raises(QuotaExceeded):
            slots.reserve()
        finish.set()
        thread.join(timeout=10)
        assert not thread.is_alive() and not errors
        assert state(slots, identity).state == "unknown"
        assert state(slots, identity).released is not None
    finally:
        finish.set(); thread.join(timeout=10); pool.shutdown(wait=True)


_CHILD = r'''
import json, sys
from uuid import UUID
from sqlalchemy import text
from project_copilot.db.session import Database
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.execution_slots import ExecutionLease, register_execution, provider_started
from project_copilot.services.quotas import QuotaService
payload=json.loads(sys.stdin.readline())
database=Database(payload['url'])
identity=UUID(payload['job'])
lease=ExecutionLease.create(payload['manifest'], identity)
with database.transaction() as session:
    row=session.get(AiRequestReservation, identity)
    assert QuotaService(session).claim(row.user_id, identity)
    register_execution(session,row,lease)
    if payload.get('provider_open'):
        provider_started(session,identity,lease.token)
# Simulate loss of every SQL connection without ending the physical execution.
database.close()
print('READY',flush=True)
sys.stdin.readline()
lease.close()
'''


def launch_execution(setup, *, kind="rag", provider_open=False):
    identity, _ = setup.reserve(kind=kind)
    process = subprocess.Popen([sys.executable, "-u", "-c", _CHILD], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    process.stdin.write(json.dumps({"url": setup.database.engine.url.render_as_string(hide_password=False),
        "job": str(identity), "manifest": str(setup.settings.manifest_dir), "provider_open": provider_open}) + "\n")
    process.stdin.flush()
    ready, _, _ = os_select.select([process.stdout], [], [], 10)
    if not ready or process.stdout.readline().strip() != "READY":
        process.kill(); process.wait(timeout=5)
        raise AssertionError("isolated execution child did not become ready")
    return identity, process


@pytest.mark.parametrize("kind,reason", [("rag", "dead_readonly"), ("agent", "dead_agent_fenced")])
def test_real_process_lease_not_age_or_sql_disconnect_proves_crash_end(slots, kind, reason):
    identity, process = launch_execution(slots, kind=kind)
    try:
        with slots.database.transaction() as session:
            session.get(AiRequestReservation, identity).created_at -= timedelta(days=7)
            session.get(AiJobExecution, identity).created_at -= timedelta(days=7)
        assert slots.reap() == 0  # old timestamps + no SQL connection still do not prove exit
        assert state(slots, identity).released is None
        process.kill(); process.wait(timeout=5)
        assert slots.reap() == 1
        snapshot = state(slots, identity)
        assert snapshot.state == "unknown" and snapshot.released is not None and snapshot.reason == reason
        assert slots.reap() == 0
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stdin.close(); process.stdout.close(); process.stderr.close()


def test_process_death_during_unacknowledged_provider_call_is_not_auto_reclaimed(slots):
    identity, process = launch_execution(slots, provider_open=True)
    try:
        process.kill(); process.wait(timeout=5)
        assert slots.reap() == 0
        snapshot = state(slots, identity)
        assert snapshot.released is None and snapshot.provider_open == 1
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=5)
        process.stdin.close(); process.stdout.close(); process.stderr.close()


@pytest.mark.parametrize("damage", ["missing", "bad_header", "replacement", "symlink"])
def test_missing_or_changed_lease_never_fabricates_historical_stop_evidence(slots, damage):
    identity, _, lease = slots.enroll()
    lease.close()
    original = lease.path.read_bytes()
    if damage == "missing":
        lease.path.unlink()
    elif damage == "bad_header":
        lease.path.write_bytes(b"not valid execution evidence")
    else:
        old = lease.path.with_suffix(".old")
        lease.path.rename(old)
        if damage == "replacement":
            lease.path.write_bytes(original)
        else:
            lease.path.symlink_to(old)
    assert slots.reap() == 0
    assert state(slots, identity).released is None


def test_legacy_unknown_without_execution_record_is_kept_across_new_grants(slots):
    identity, _ = slots.reserve()
    with slots.database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(slots.user_id, identity)
        quota.finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
        release.change_state(session, slots.settings, actor_id=slots.admin_id, operation="CLOSED",
            reason="synthetic grant close")
    with slots.database.transaction() as session:
        release.change_state(session, slots.settings, actor_id=slots.admin_id, operation="PERSONAL_DEMO",
            personal_user_id=slots.user_id, duration_minutes=90, reason="synthetic next grant")
    assert slots.reap() == 0
    assert state(slots, identity).released is None and state(slots, identity).token is None
    with pytest.raises(QuotaExceeded) as error:
        slots.reserve()
    assert error.value.reason == "user_concurrency"


def test_reaper_fences_old_token_before_new_submission_and_never_replays(slots):
    identity, _, lease = slots.enroll()
    token = lease.token
    lease.close()
    assert slots.reap() == 1
    with pytest.raises(ExecutionFenced), slots.database.transaction() as session:
        guard_execution(session, identity, token)
    with pytest.raises(ExecutionFenced), slots.database.transaction() as session:
        provider_started(session, identity, token)
    new_identity, _ = slots.reserve()
    assert new_identity != identity
    assert state(slots, identity).attempts == 0 and state(slots, identity).state == "unknown"


def test_concurrent_reapers_release_once_and_new_reservations_remain_single_user(slots):
    identity, _, lease = slots.enroll()
    lease.close()
    barrier = Barrier(5)
    def reaper(_):
        barrier.wait(timeout=10)
        return slots.reap()
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(reaper, range(5))) == 1
    barrier = Barrier(4)
    def submit(_):
        barrier.wait(timeout=10)
        try:
            return slots.reserve()[0]
        except QuotaExceeded:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        accepted = list(pool.map(submit, range(4)))
    assert len([item for item in accepted if item is not None]) == 1
    assert state(slots, identity).released is not None


def test_agent_proposal_record_and_execution_fence_share_the_same_real_transaction(slots):
    from project_copilot.agent.proposals import ProposalBackend
    with slots.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(slots.token)
        context = ApprovalService(session, principal=principal, enforce_ownership=True).create_context()
    identity, _, lease = slots.enroll(kind="agent", context_id=context.context_id)
    tracker = ExecutionTracker(slots.database, identity, lease.token)
    backend = ProposalBackend(slots.settings, slots.database, context.context_id, None, auth_session_id=slots.login_id)
    with track_execution(tracker):
        result = asyncio.run(backend.propose_ticket("controlled proposal", "no automatic ticket confirmation"))
    tracker.drain()
    action_id = UUID(result["data"]["action_id"])
    with slots.database.transaction() as session:
        execution = session.get(AiJobExecution, identity)
        assert execution.action_id == action_id
        action = session.get(PendingAction, action_id)
        assert action.owner_user_id == slots.user_id and action.status == "pending" and action.ticket_id is None
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    lease.close()
    assert slots.reap() == 1
    with track_execution(ExecutionTracker(slots.database, identity, lease.token)), pytest.raises(ExecutionFenced):
        asyncio.run(backend.propose_ticket("stale retry", "must not create a duplicate"))
    with slots.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        assert session.get(PendingAction, action_id).status == "pending"


def test_agent_reclaim_keeps_confirmed_ticket_and_repeat_confirmation_idempotent(slots):
    from project_copilot.agent.proposals import ProposalBackend
    with slots.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(slots.token)
        context = ApprovalService(session, principal=principal, enforce_ownership=True).create_context()
    identity, _, lease = slots.enroll(kind="agent", context_id=context.context_id)
    tracker = ExecutionTracker(slots.database, identity, lease.token)
    backend = ProposalBackend(slots.settings, slots.database, context.context_id, None, auth_session_id=slots.login_id)
    with track_execution(tracker):
        result = asyncio.run(backend.propose_ticket("confirmed controlled proposal", "synthetic business proof"))
    tracker.drain()
    action_id = UUID(result["data"]["action_id"])
    with slots.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(slots.token)
        service = ApprovalService(session, principal=principal, enforce_ownership=True)
        checked = service.authenticate(context.context_id, context.context_token)
        action = session.get(PendingAction, action_id)
        confirmed = service.decide(checked, action_id, action.payload_fingerprint, approve=True)
        ticket_id, fingerprint = confirmed.ticket_id, confirmed.payload_fingerprint
        assert confirmed.status == "completed"
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    lease.close()
    assert slots.reap() == 1
    assert state(slots, identity).state == "unknown"
    assert state(slots, identity).reason == "dead_agent_fenced"
    with slots.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(slots.token)
        service = ApprovalService(session, principal=principal, enforce_ownership=True)
        checked = service.authenticate(context.context_id, context.context_token)
        repeated = service.decide(checked, action_id, fingerprint, approve=True)
        assert repeated.ticket_id == ticket_id and repeated.status == "completed"
        ticket = session.get(Ticket, ticket_id)
        assert ticket.creation_key == action_id and ticket.owner_user_id == slots.user_id
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1


def test_asyncio_timeout_does_not_release_physical_to_thread_early(slots):
    identity, _ = slots.reserve(kind="agent")
    started, finish, returned = Event(), Event(), Event()
    errors = []
    def physical_tool():
        started.set()
        assert finish.wait(15)
    async def invoke():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(tracked_to_thread(physical_tool), timeout=0.05)
        returned.set()
    def runner(*_):
        asyncio.run(invoke())
        return SimpleNamespace(status="failed", failure_type="tool_timeout", events=[], model_events=[],
            usage=None, model_dump=lambda **_: {"status": "failed"})
    def work():
        try:
            ai_worker.process_one(slots.database, slots.settings, runner=runner)
        except BaseException as exc:
            errors.append(type(exc).__name__)
    thread = Thread(target=work)
    thread.start()
    try:
        assert started.wait(8) and returned.wait(8)
        assert state(slots, identity).released is None
        assert slots.reap() == 0
        with pytest.raises(QuotaExceeded):
            slots.reserve()
        finish.set(); thread.join(timeout=10)
        assert not thread.is_alive() and not errors
        snapshot = state(slots, identity)
        assert snapshot.state == "unknown" and snapshot.released is not None
    finally:
        finish.set(); thread.join(timeout=10)


def test_stale_execution_is_fenced_before_provider_counter_or_outbound_call(slots):
    identity, _, lease = slots.enroll()
    token = lease.token
    lease.close()
    assert slots.reap() == 1
    tracker = ExecutionTracker(slots.database, identity, token)
    with track_execution(tracker), account_job(slots.database, slots.user_id, identity, settings=slots.settings):
        with pytest.raises(ExecutionFenced):
            with provider_request():
                pytest.fail("stale execution must not reach an outbound provider")
    assert state(slots, identity).attempts == 0


def test_three_task_lifetime_budget_is_not_refunded_when_unknown_slots_release(slots):
    identities = []
    for _ in range(3):
        identity, _, lease = slots.enroll()
        identities.append(identity)
        with slots.database.transaction() as session:
            QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
        assert mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    with pytest.raises(QuotaExceeded) as error:
        slots.reserve()
    assert error.value.reason == "personal_demo_limit"
    with slots.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 3
        assert all(session.get(AiRequestReservation, identity).state == "unknown" for identity in identities)


def test_never_claimed_queue_expiry_releases_slot_without_execution_proof(slots):
    identity, _ = slots.reserve()
    with slots.database.transaction() as session:
        session.get(AiRequestReservation, identity).created_at -= timedelta(days=2)
    assert ai_worker.expire_unclaimed(slots.database, ttl_seconds=60) == 1
    snapshot = state(slots, identity)
    assert snapshot.state == "failed" and snapshot.released is not None and snapshot.attempts == 0
    assert snapshot.token is None
    assert slots.reserve()[0] != identity


def test_agent_business_mismatch_stays_manual_even_after_execution_ends(slots):
    from project_copilot.schemas.tickets import TicketCreate
    identity, _, lease = slots.enroll(kind="agent")
    with slots.database.transaction() as session:
        other = AuthService(session).register(RegisterInput(username="other", password=PASSWORD), open_registration=True)
        service = ApprovalService(session, principal=other, enforce_ownership=True)
        context = service.create_context()
        checked = service.authenticate(context.context_id, context.context_token)
        action = service.preview(checked, TicketCreate(title="other user's action", description="isolated corruption probe"))
        session.get(AiJobExecution, identity).action_id = action.action_id
        QuotaService(session).finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
    assert not mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    lease.close()
    assert slots.reap() == 0
    assert state(slots, identity).released is None


def test_upload_process_crash_keeps_unreconciled_index_side_effects_for_manual_review(slots):
    identity, _, lease = slots.enroll(kind="upload")
    lease.close()
    assert slots.reap() == 0
    assert state(slots, identity).released is None


def test_existing_manual_fallback_keeps_unknown_usage_and_idempotency_when_explicitly_reconciled(slots):
    from project_copilot.services.public_maintenance import inspect_job, resolve_no_retry
    from project_copilot.services.worker_stop import sign_inventory
    identity, key = slots.reserve()
    with slots.database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(slots.user_id, identity)
        quota.finish(slots.user_id, identity, outcome="unknown", usage=USAGE)
        release.change_state(session, slots.settings, actor_id=slots.admin_id, operation="CLOSED",
            reason="synthetic manual recovery window")
        inspection = inspect_job(session, slots.admin_id, identity)
    now = datetime.now(timezone.utc)
    proof = sign_inventory({"version": 1, "job_id": str(identity), "resource_id": "public-demo-v2",
        "captured_at": now.isoformat(), "postgres_volume": "pc-public-v2-postgres", "review_sha256": "e" * 64,
        "workers": [{"container_id": "a" * 64, "status": "exited", "pid": 0, "running": False,
            "restarting": False, "finished_at": now.isoformat()}]}, slots.settings.public_proxy_secret.get_secret_value())
    evidence = {"version": 1, "job_id": str(identity), "inspection_sha256": inspection["inspection_sha256"],
        "original_execution_terminated": True, "business_verified": True,
        "provider_outcome": "completed_and_reconciled", "evidence_reference_sha256": "e" * 64,
        "decision": "terminal_no_retry"}
    with slots.database.transaction() as session:
        result = resolve_no_retry(session, slots.admin_id, identity, evidence, worker_stop_proof=proof, settings=slots.settings)
        assert result["state"] == "unknown" and result["provider_result"] == "not_inferred"
    snapshot = state(slots, identity)
    assert snapshot.state == "unknown" and snapshot.released is not None and snapshot.finished is None
    assert snapshot.usage == USAGE and snapshot.key == key and snapshot.cost == "unknown"
    with slots.database.transaction() as session:
        row, reused = QuotaService(session).reserve(slots.user_id, key, "f" * 64, "rag")
        assert reused and row.id == identity and not QuotaService(session).claim(slots.user_id, identity)


def recovery_inputs(setup, identity):
    """Synthetic administrator review and stopped-worker proof, never live proof."""
    from project_copilot.services.public_maintenance import inspect_job
    from project_copilot.services.worker_stop import sign_inventory
    with setup.database.transaction() as session:
        release.change_state(session, setup.settings, actor_id=setup.admin_id, operation="CLOSED",
            reason="synthetic acknowledgement failure reconciliation")
        inspection = inspect_job(session, setup.admin_id, identity)
    now = datetime.now(timezone.utc)
    proof = sign_inventory({"version": 1, "job_id": str(identity), "resource_id": "public-demo-v2",
        "captured_at": now.isoformat(), "postgres_volume": "pc-public-v2-postgres", "review_sha256": "d" * 64,
        "workers": [{"container_id": "b" * 64, "status": "exited", "pid": 0, "running": False,
            "restarting": False, "finished_at": now.isoformat()}]}, setup.settings.public_proxy_secret.get_secret_value())
    evidence = {"version": 1, "job_id": str(identity), "inspection_sha256": inspection["inspection_sha256"],
        "original_execution_terminated": True, "business_verified": True,
        "provider_outcome": "completed_and_reconciled", "evidence_reference_sha256": "d" * 64,
        "decision": "terminal_no_retry"}
    return evidence, proof


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_manual_recovery_of_enrolled_terminal_ack_failure_preserves_original_outcome(slots, monkeypatch, outcome):
    from project_copilot.db.operations import JobRecoveryAudit
    from project_copilot.services import execution_slots
    from project_copilot.services.public_maintenance import MaintenanceRejected, resolve_no_retry
    identity, key, lease = slots.enroll()
    tracker = ExecutionTracker(slots.database, identity, lease.token)
    acknowledgements = []
    def rejected_acknowledgement(*_, **__):
        acknowledgements.append("controlled-acknowledgement")
        # A real isolated PostgreSQL transaction fails and rolls back. The
        # provider substitute has ended, but no durable completion proof exists.
        with slots.database.transaction() as session:
            session.execute(text("SELECT 1 / 0"))
    with monkeypatch.context() as patch:
        patch.setattr(execution_slots, "provider_finished", rejected_acknowledgement)
        with track_execution(tracker), account_job(slots.database, slots.user_id, identity, settings=slots.settings):
            with provider_request() as attempt:
                attempt.completed()
    tracker.drain()
    original_result = {"status": outcome, "answer": "synthetic durable final result", "usage": USAGE}
    with slots.database.transaction() as session:
        session.get(AiJob, identity).result = original_result
        QuotaService(session).finish(slots.user_id, identity, outcome=outcome, usage=USAGE)
    assert not mark_local_done_and_release(slots.database, slots.settings, identity, lease.token, lease)
    lease.close()
    assert slots.reap() == 0
    before = state(slots, identity)
    assert before.released is None and before.provider_open == 1 and before.attempts == 1
    assert before.finished is not None and acknowledgements == ["controlled-acknowledgement"]
    evidence, proof = recovery_inputs(slots, identity)
    with slots.database.transaction() as session:
        result = resolve_no_retry(session, slots.admin_id, identity, evidence,
            worker_stop_proof=proof, settings=slots.settings)
        assert result["state"] == outcome and result["provider_result"] == "not_inferred"
        assert result["slot_released"] and not result["replay_permitted"]
    after = state(slots, identity)
    assert after.released is not None and after.reason == "manual"
    assert (after.state, after.finished, after.usage, after.attempts, after.cost, after.key, after.day, after.grant,
            after.provider_open, after.provider_uncertain) == (
        before.state, before.finished, before.usage, before.attempts, before.cost, before.key, before.day, before.grant,
        before.provider_open, before.provider_uncertain)
    with slots.database.transaction() as session:
        assert session.get(AiJob, identity).result == original_result
        audit = session.scalar(select(JobRecoveryAudit).where(JobRecoveryAudit.job_id == identity))
        assert audit.previous_state == outcome and audit.provider_attempts == 1 and audit.prior_usage == USAGE
        assert audit.resolution == "terminal_no_retry" and audit.inspection_sha256 == evidence["inspection_sha256"]
        row, reused = QuotaService(session).reserve(slots.user_id, key, "f" * 64, "rag")
        assert reused and row.id == identity and not QuotaService(session).claim(slots.user_id, identity)
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
    with pytest.raises(MaintenanceRejected), slots.database.transaction() as session:
        resolve_no_retry(session, slots.admin_id, identity, evidence, worker_stop_proof=proof, settings=slots.settings)
    with slots.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobRecoveryAudit)) == 1


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_manual_terminal_recovery_rejects_missing_execution_enrollment(slots, outcome):
    from project_copilot.db.operations import JobRecoveryAudit
    from project_copilot.services.public_maintenance import MaintenanceRejected, resolve_no_retry
    identity, _ = slots.reserve()
    with slots.database.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(slots.user_id, identity)
        quota.finish(slots.user_id, identity, outcome=outcome, usage=USAGE)
        # An anomalous imported legacy row is created only in this owned test DB.
        # It must not gain terminal-recovery eligibility without enrollment.
        session.get(AiRequestReservation, identity).slot_released_at = None
    before = state(slots, identity)
    evidence, proof = recovery_inputs(slots, identity)
    with pytest.raises(MaintenanceRejected), slots.database.transaction() as session:
        resolve_no_retry(session, slots.admin_id, identity, evidence, worker_stop_proof=proof, settings=slots.settings)
    after = state(slots, identity)
    assert after.state == outcome and after.released is None and after.token is None
    assert after.finished == before.finished and after.usage == USAGE
    with slots.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(JobRecoveryAudit)) == 0
