"""Owned-test-cluster queue experiment; deployment and providers are controlled.

Only run_queue(execute=True) creates a new, random database. Child processes
accept only that freshly owned database identity and never load a dotenv file.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import multiprocessing
import json
import os
import re
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob, AiJobEvent
from project_copilot.db.models import AiRequestReservation, LoginSession, UserAccount
from project_copilot.db.session import Database
from project_copilot.services import ai_worker
from project_copilot.services.quotas import QuotaExceeded, QuotaService


def _owned_url(value):
    url = make_url(value)
    if (url.drivername != "postgresql+psycopg" or url.username != "project_copilot_test"
            or url.host not in {"127.0.0.1", "localhost", "postgres-test"}
            or url.port not in {5434, 5432} or url.query
            or not re.fullmatch(r"pc_test_d20_[0-9a-f]{32}", url.database or "")):
        raise ValueError("queue experiment requires its newly owned test database")
    return value


def _settings():
    return Settings(_env_file=None, environment="test", public_demo=False,
                    database_url=None, zhipu_api_key=None, deepseek_api_key=None,
                    public_proxy_secret=None, auth_public_origin=None,
                    redis_enabled=False, redis_admission_enabled=False, redis_cache_enabled=False, redis_url=None,
                    redis_cache_hmac_key=None,
                    queue_max_pending_jobs=3,
                    worker_max_active_jobs=2, final_rerank_device="cpu", final_rerank_dtype="float32",
                    tracing_enabled=False)


@contextmanager
def controlled_boundaries():
    """Explicitly bypass deployment health/release gates, never queue SQL.

    The normal runner is never used. This experiment cannot certify a live
    release, authentication flow, external provider or personal execution proof.
    """
    previous_open, previous_ready = ai_worker.require_open, ai_worker.dependencies_ready
    ai_worker.require_open = lambda *_args, **_kwargs: None
    ai_worker.dependencies_ready = lambda *_args, **_kwargs: None
    try:
        yield
    finally:
        ai_worker.require_open, ai_worker.dependencies_ready = previous_open, previous_ready


def seed_identity(database):
    """Synthetic rows only; no credentials leave the newly owned database."""
    user_id, login_id = uuid4(), uuid4()
    with database.transaction() as session:
        session.add(UserAccount(id=user_id, username="queue-lab-" + uuid4().hex[:16],
                                password_hash="$argon2id$synthetic-queue-lab"))
        session.flush()
        session.add(LoginSession(id=login_id, user_id=user_id,
            token_hash=sha256(uuid4().bytes).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
    return user_id, login_id


def enqueue(database, settings, user_id, login_id, *, key=None, payload=None):
    key = key or "queue-lab-" + uuid4().hex
    payload = payload or {"question": "synthetic queue experiment"}
    fingerprint = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with database.transaction() as session:
        row, replay = QuotaService(session, settings=settings, user_daily_limit=100,
            global_daily_limit=100, user_concurrency_limit=100).reserve(user_id, key, fingerprint, "rag")
        if not replay:
            session.add(AiJob(id=row.id, login_session_id=login_id, payload=payload))
        return row.id, key, replay


def synthetic_result(job):
    data = {"synthetic": True, "job_id": str(job.id), "worker_pid": os.getpid()}
    return SimpleNamespace(model_dump=lambda **_: data, generation=None)


def _worker_entry(url, barrier, output):
    database = None
    try:
        database = Database(_owned_url(url), pool_size=3, max_overflow=0, pool_timeout=2)
        settings = _settings()
        executed = []
        def runner(_settings, _database, job):
            executed.append(str(job.id))
            # A small synthetic CPU-work surrogate gives both independent
            # processes an opportunity to claim, with no provider or model.
            time.sleep(0.04)
            return synthetic_result(job)
        barrier.wait(timeout=15)
        with controlled_boundaries():
            while ai_worker.process_one(database, settings, runner=runner):
                pass
        output.put({"pid": os.getpid(), "executed": executed, "ok": True})
    except Exception:
        output.put({"pid": os.getpid(), "executed": [], "ok": False})
    finally:
        if database is not None:
            database.close()


def worker_pair(url):
    _owned_url(url)
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_worker_entry, args=(url, barrier, output)) for _ in range(2)]
    try:
        for process in processes:
            process.start()
        result = [output.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=5)
        if any(process.exitcode != 0 for process in processes) or not all(row["ok"] for row in result):
            raise RuntimeError("isolated queue workers failed")
        return result
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()
        output.join_thread()


def _crash_entry(url, connection):
    database = Database(_owned_url(url), pool_size=2, max_overflow=0)
    with controlled_boundaries():
        job = ai_worker.claim_one(database, settings=_settings())
    connection.send(str(job.id) if job is not None else None)
    connection.close()
    # Deliberate physical process exit after the claim commit, before any
    # provider, result or completion proof. General-mode work stays held.
    os._exit(73)


def crash_after_claim(url):
    _owned_url(url)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_crash_entry, args=(url, child))
    try:
        process.start()
        child.close()
        if not parent.poll(15):
            raise RuntimeError("isolated crash worker failed to claim")
        identity = parent.recv()
        process.join(timeout=5)
        if process.exitcode != 73 or identity is None:
            raise RuntimeError("isolated crash worker did not reach its boundary")
        return {"job_id": identity, "exit_code": process.exitcode}
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def run_queue(arguments):
    plan = {"workers": 2, "new_owned_database": True, "queue_capacity": 3,
            "execution_capacity": 2, "external_provider_calls": 0,
            "release_boundary": "controlled", "dependency_health": "controlled",
            "runner": "synthetic CPU result; no model loading", "personal_recovery_certified": False,
            "faults": {"redis_admission": "injected RedisUnavailable; no Redis server contacted",
                       "worker": "actual os._exit(73) after committed claim"}}
    if not getattr(arguments, "execute", False):
        return {"plan": plan, "network_accessed": False}
    from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database
    from project_copilot.db.migrations import upgrade
    from project_copilot.services.queue_control import inspect_failures
    from project_copilot.infrastructure.redis_runtime import RedisUnavailable
    from sqlalchemy import text

    started = time.perf_counter()
    with owned_test_database(TEST_DATABASE_URL) as url:
        database = Database(url)
        try:
            upgrade(database)
            settings = _settings()
            user_id, login_id = seed_identity(database)
            initial = [enqueue(database, settings, user_id, login_id) for _ in range(3)]
            rejected = False
            try:
                enqueue(database, settings, user_id, login_id)
            except QuotaExceeded as error:
                rejected = error.reason == "queue_backlog"
            replayed = enqueue(database, settings, user_id, login_id, key=initial[0][1])
            workers = worker_pair(url)
            executions = [identity for worker in workers for identity in worker["executed"]]
            expected = {str(row[0]) for row in initial}
            with database.transaction() as session:
                successes = session.scalars(select(AiRequestReservation).where(
                    AiRequestReservation.state == "succeeded")).all()
                terminals = session.scalar(select(func.count()).select_from(AiJobEvent).where(AiJobEvent.kind == "terminal"))
                succeeded_once = len(successes) == terminals == len(initial)
            failed_id, _, _ = enqueue(database, settings, user_id, login_id)
            def unavailable(*_):
                raise RedisUnavailable()
            with controlled_boundaries():
                ai_worker.process_one(database, settings, runner=unavailable)
            crash_id, _, _ = enqueue(database, settings, user_id, login_id)
            pending_id, _, _ = enqueue(database, settings, user_id, login_id)
            crash = crash_after_claim(url)
            unexpected_calls = []
            with controlled_boundaries():
                blocked = ai_worker.process_one(database, settings,
                    runner=lambda *_: unexpected_calls.append(True)) is False
            with database.transaction() as session:
                session.execute(text("SET TRANSACTION READ ONLY"))
                failures = inspect_failures(session)
                failed = session.get(AiRequestReservation, failed_id)
                crashed = session.get(AiRequestReservation, crash_id)
                pending = session.get(AiRequestReservation, pending_id)
                preserved = (failed.state == "unknown" and failed.slot_released_at is None
                             and crashed.state == "running" and crashed.slot_released_at is None
                             and pending.state == "reserved" and pending.slot_released_at is None)
                attempts = session.scalar(select(func.sum(AiRequestReservation.provider_attempts)))
            checks = {"queue_cap_rejected_new_request": rejected,
                "saturated_idempotent_replay": replayed[0] == initial[0][0] and replayed[2],
                "two_distinct_cpu_processes": len({worker["pid"] for worker in workers}) == 2,
                "both_workers_claimed": all(worker["executed"] for worker in workers),
                "every_initial_job_executed_once": len(executions) == len(expected) and set(executions) == expected,
                "success_and_terminal_committed_once": succeeded_once,
                "crash_claimed_expected_job": crash["job_id"] == str(crash_id),
                "unknown_and_crashed_slots_stay_held": preserved,
                "held_slots_block_new_claim": blocked and not unexpected_calls,
                "no_provider_requests": attempts == 0,
                "failure_view_read_only": len(failures) == 1 and failures[0]["automatic_replay_allowed"] is False}
            return {"plan": plan, "checks": checks, "workers": workers, "crash": crash,
                    "failure_view": failures, "network_accessed": True,
                    "elapsed_seconds": round(time.perf_counter() - started, 6),
                    "measurement_scope": "real PostgreSQL queue/advisory locks/journal; controlled release, health and provider boundaries",
                    "replay_policy": "running and unknown are never automatically rerun; this lab supplies no personal release proof"}
        finally:
            database.close()
