"""Real owned PG queue races; release/health/providers are explicitly controlled.

No live release, models, Redis service, business database or personal death
proof is substituted by these assertions. Existing execution-proof tests remain.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as PoolTimeout

from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.migrations import check_model_drift, migration_config
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.session import Database
from project_copilot.infrastructure.redis_runtime import RedisUnavailable
from project_copilot.services import ai_worker, redis_admission
from project_copilot.services.queue_control import inspect_failures
from project_copilot.services.quotas import QuotaExceeded
from scripts.week1_queue_lab import (_settings, controlled_boundaries, crash_after_claim,
                                     enqueue, run_queue, seed_identity, synthetic_result, worker_pair)
from test_execution_slots_postgres import launch_execution, slots, state as personal_state


@pytest.fixture
def queue(ticket_database):
    settings = _settings()
    user_id, login_id = seed_identity(ticket_database)
    with controlled_boundaries():
        yield ticket_database, settings, user_id, login_id


def states(database):
    with database.transaction() as session:
        return {row.id: (row.state, row.slot_released_at, row.provider_attempts,
                        session.get(AiJob, row.id).result)
                for row in session.scalars(select(AiRequestReservation)).all()}


def test_queue_partial_indexes_and_orm_match_real_head(ticket_database):
    check_model_drift(ticket_database)
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(migration_config()).get_current_head()
        indexes = dict(session.execute(text("SELECT indexname,indexdef FROM pg_indexes "
            "WHERE schemaname='public' AND tablename='ai_request_reservations'")).all())
        assert "WHERE" in indexes["ix_ai_reserved_claim"] and "reserved" in indexes["ix_ai_reserved_claim"]
        assert "created_at, id" in indexes["ix_ai_reserved_claim"]
        assert "slot_released_at IS NULL" in indexes["ix_ai_held_execution_slots"]


def test_queue_cap_is_atomic_across_accounts_and_rollbacks(queue):
    database, settings, _, _ = queue
    identities = [seed_identity(database) for _ in range(8)]
    barrier = Barrier(len(identities))
    def submit(identity):
        barrier.wait(timeout=10)
        try:
            enqueue(database, settings, *identity)
            return "accepted"
        except QuotaExceeded as error:
            return error.reason
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(submit, identities))
    assert result.count("accepted") == 3 and result.count("queue_backlog") == 5
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 3
        assert session.scalar(select(func.count()).select_from(AiJob)) == 3
    assert database.engine.pool.checkedout() == 0


def test_saturated_idempotent_replay_precedes_queue_and_redis_checks(queue, monkeypatch):
    database, settings, user_id, login_id = queue
    first = enqueue(database, settings, user_id, login_id)
    for _ in range(2):
        enqueue(database, settings, user_id, login_id)
    with pytest.raises(QuotaExceeded) as error:
        enqueue(database, settings, user_id, login_id)
    assert error.value.reason == "queue_backlog"
    monkeypatch.setattr(redis_admission, "admit", lambda *_args, **_kwargs: (_ for _ in ()).throw(RedisUnavailable()))
    replay = enqueue(database, settings, user_id, login_id, key=first[1])
    assert replay == (first[0], first[1], True)
    assert len(states(database)) == 3


def test_execution_capacity_is_atomic_before_candidate_row_claim(queue):
    database, settings, user_id, login_id = queue
    settings.worker_max_active_jobs = 1
    for _ in range(3):
        enqueue(database, settings, user_id, login_id)
    barrier = Barrier(4)
    def claim(_):
        barrier.wait(timeout=10)
        return ai_worker.claim_one(database, settings=settings)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(claim, range(4)))
    assert sum(job is not None for job in claimed) == 1
    assert sorted(item[0] for item in states(database).values()) == ["reserved", "reserved", "running"]


def test_two_independent_cpu_processes_commit_each_job_once(queue):
    database, settings, user_id, login_id = queue
    jobs = [enqueue(database, settings, user_id, login_id)[0] for _ in range(3)]
    url = database.engine.url.render_as_string(hide_password=False)
    workers = worker_pair(url)
    executed = [identity for worker in workers for identity in worker["executed"]]
    assert len({worker["pid"] for worker in workers}) == 2
    assert all(worker["executed"] for worker in workers)
    assert len(executed) == len(set(executed)) == 3
    assert set(executed) == {str(identity) for identity in jobs}
    with database.transaction() as session:
        for identity in jobs:
            reservation, job = session.get(AiRequestReservation, identity), session.get(AiJob, identity)
            assert reservation.state == "succeeded" and reservation.slot_released_at is not None
            assert job.result["job_id"] == str(identity) and job.result["worker_pid"] in {row["pid"] for row in workers}
            assert reservation.provider_attempts == 0
            assert session.scalar(select(func.count()).select_from(AiJobEvent).where(
                AiJobEvent.job_id == identity, AiJobEvent.kind == "terminal")) == 1
    assert not ai_worker.process_one(database, settings, runner=lambda *_: pytest.fail("completed job replayed"))


def test_held_unknown_blocks_claim_and_redis_failure_never_replays(queue, caplog):
    database, settings, user_id, login_id = queue
    settings.worker_max_active_jobs = 1
    failed_id = enqueue(database, settings, user_id, login_id)[0]
    pending_id = enqueue(database, settings, user_id, login_id)[0]
    called = []
    def unavailable(*_):
        called.append("once")
        raise RedisUnavailable()
    assert ai_worker.process_one(database, settings, runner=unavailable)
    assert not ai_worker.process_one(database, settings, runner=lambda *_: pytest.fail("held unknown did not block claim"))
    assert called == ["once"]
    current = states(database)
    assert current[failed_id] == ("unknown", None, 0, None)
    assert current[pending_id] == ("reserved", None, 0, None)
    with database.transaction() as session:
        view = inspect_failures(session)
    assert view[0]["failure_type"] == "admission_unavailable"
    assert view[0]["slot_held"] and not view[0]["automatic_replay_allowed"]
    assert "synthetic queue experiment" not in caplog.text


def test_real_process_exit_after_claim_stays_running_and_held(queue):
    database, settings, user_id, login_id = queue
    settings.worker_max_active_jobs = 1
    crashed_id = enqueue(database, settings, user_id, login_id)[0]
    crash = crash_after_claim(database.engine.url.render_as_string(hide_password=False))
    assert crash == {"job_id": str(crashed_id), "exit_code": 73}
    pending_id = enqueue(database, settings, user_id, login_id)[0]
    assert not ai_worker.process_one(database, settings, runner=lambda *_: pytest.fail("crashed work replayed or freed capacity"))
    current = states(database)
    assert current[crashed_id] == ("running", None, 0, None)
    assert current[pending_id] == ("reserved", None, 0, None)


def test_failure_inspection_is_read_only_and_never_exports_payload_or_raw_failure(queue):
    database, settings, user_id, login_id = queue
    identity = enqueue(database, settings, user_id, login_id,
                       payload={"question": "private-prompt-canary"})[0]
    def failed(*_):
        raise RuntimeError("private-provider-canary")
    assert ai_worker.process_one(database, settings, runner=failed)
    with database.transaction() as session:
        session.get(AiJob, identity).failure_type = "private-failure-canary"
    before = states(database)
    with database.transaction() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        inspected = inspect_failures(session, limit=1)
    assert len(inspected) == 1 and inspected[0]["job_id"] == str(identity)
    assert inspected[0]["failure_type"] == "execution_error"
    assert inspected[0]["slot_held"] and not inspected[0]["automatic_replay_allowed"]
    assert "private-" not in json.dumps(inspected)
    assert states(database) == before


def test_small_real_pool_times_out_without_claim_result_or_success(queue):
    database, settings, user_id, login_id = queue
    identity = enqueue(database, settings, user_id, login_id)[0]
    small = Database(database.engine.url.render_as_string(hide_password=False),
                     pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        with pytest.raises(PoolTimeout):
            ai_worker.process_one(small, settings, runner=lambda *_: pytest.fail("pool timeout executed a model"))
        assert small.engine.pool.checkedout() == 0
    finally:
        small.close()
    assert states(database)[identity] == ("reserved", None, 0, None)


def test_own_pg_connection_termination_rolls_back_terminal_success_without_replay(queue, monkeypatch):
    database, settings, user_id, login_id = queue
    identity = enqueue(database, settings, user_id, login_id)[0]
    executed = []
    def runner(_settings, _database, job):
        executed.append(job.id)
        return synthetic_result(job)
    original = ai_worker.append_event
    def disconnect(session, job_id, kind, data):
        if kind == "terminal":
            assert session.scalar(text("SELECT current_database()")) == database.engine.url.database
            backend = session.scalar(text("SELECT pg_backend_pid()"))
            # This PID belongs to the current transaction in this test's new
            # random database. No cluster restart or unrelated backend is used.
            with database.engine.begin() as control:
                assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend})
            session.execute(text("SELECT 1"))
        return original(session, job_id, kind, data)
    monkeypatch.setattr(ai_worker, "append_event", disconnect)
    with pytest.raises(SQLAlchemyError):
        ai_worker.process_one(database, settings, runner=runner)
    monkeypatch.setattr(ai_worker, "append_event", original)
    assert executed == [identity]
    assert states(database)[identity] == ("running", None, 0, None)
    assert not ai_worker.process_one(database, settings, runner=lambda *_: pytest.fail("PG disconnect replayed work"))
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobEvent).where(
            AiJobEvent.job_id == identity, AiJobEvent.kind == "terminal")) == 0
    assert database.engine.pool.checkedout() == 0


def test_queue_lab_plan_does_not_connect(monkeypatch):
    from types import SimpleNamespace
    from scripts import week1_queue_lab as lab
    monkeypatch.setattr(lab, "Database", lambda *_args, **_kwargs: pytest.fail("dry run connected"))
    result = run_queue(SimpleNamespace(execute=False))
    assert not result["network_accessed"]
    assert result["plan"]["workers"] == 2 and result["plan"]["external_provider_calls"] == 0


def test_queue_lab_settings_do_not_inherit_production_secrets_or_admission(monkeypatch):
    for name, value in {"PUBLIC_DEMO": "true", "DATABASE_URL": "postgresql+psycopg://private:secret@production:5432/private",
                        "DEEPSEEK_API_KEY": "private-key-canary", "ZHIPU_API_KEY": "private-key-canary",
                        "REDIS_ENABLED": "true", "REDIS_URL": "redis://private:secret@production:6379/0",
                        "REDIS_ADMISSION_ENABLED": "true", "REDIS_CACHE_ENABLED": "true",
                        "REDIS_CACHE_HMAC_KEY": "private-cache-key-canary-" * 2}.items():
        monkeypatch.setenv("PROJECT_COPILOT_" + name, value)
    active = _settings()
    assert not active.public_demo and not active.redis_enabled and not active.redis_admission_enabled
    assert not active.redis_cache_enabled and active.redis_url is None and active.redis_cache_hmac_key is None
    assert active.database_url is active.zhipu_api_key is active.deepseek_api_key is None


def test_personal_capacity_reclaims_only_after_real_os_owner_exits_before_claim(slots):
    slots.settings.personal_max_concurrent_requests_per_user = 3
    slots.settings.worker_max_active_jobs = 1
    # Refresh the synthetic operator grant through the actual service after
    # changing its limits; never bypass the existing evidence/limits check.
    from project_copilot.services.release_control import change_state
    for operation in ("CLOSED", "PERSONAL_DEMO", "worker-enable"):
        with slots.database.transaction() as session:
            change_state(session, slots.settings, actor_id=slots.admin_id, operation=operation,
                personal_user_id=slots.user_id if operation == "PERSONAL_DEMO" else None,
                duration_minutes=90, reason="synthetic capacity liveness grant")
    # Reuse the established PG/OS proof fixture. This child closes every SQL
    # connection while retaining its real flock, which must still block reclaim.
    previous, process = launch_execution(slots)
    try:
        pending, _ = slots.reserve()
        assert ai_worker.claim_one(slots.database, settings=slots.settings) is None
        assert process.poll() is None
        assert personal_state(slots, previous).released is None
        process.kill()
        process.wait(timeout=5)
        claimed = ai_worker.claim_one(slots.database, settings=slots.settings)
        assert claimed is not None and claimed.id == pending and claimed.id != previous
        assert claimed.execution_lease is not None
        slots.leases.append(claimed.execution_lease)
        released = personal_state(slots, previous)
        assert released.released is not None and released.state == "unknown"
        assert released.reason == "dead_readonly" and released.attempts == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
