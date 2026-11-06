"""Personal v2 admission and serial Worker contracts on isolated PostgreSQL.

All provider boundaries are controlled completions; no network/model is invoked.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, insert, select, text

from project_copilot import main
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.release import ReleaseControl
from project_copilot.infrastructure.execution_tracking import provider_request
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services import ai_worker, release_control as release
from project_copilot.services.auth import AuthService
from project_copilot.services.quotas import QuotaExceeded, QuotaService


PASSWORD = "synthetic-capacity-password-7031"


@pytest.fixture
def capacity(ticket_database, monkeypatch, tmp_path, request):
    db = ticket_database
    settings = Settings(_env_file=None, environment="production", public_demo=True,
        public_proxy_secret="synthetic_capacity_" + "a" * 40,
        auth_public_origin="https://capacity.example",
        manifest_dir=tmp_path / "manifests",
        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public",
        **getattr(request, "param", {}))
    settings.manifest_dir.mkdir()
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="capacity_admin", password=PASSWORD))
        user = auth.register(RegisterInput(username="capacity_user", password=PASSWORD), open_registration=True)
        other = auth.register(RegisterInput(username="capacity_other", password=PASSWORD), open_registration=True)
        user_login = auth.login(LoginInput(username="capacity_user", password=PASSWORD))
        _, login = auth.authenticate(user_login.token)
        other_login = auth.login(LoginInput(username="capacity_other", password=PASSWORD))
        result = release.change_state(session, settings, actor_id=admin.id, operation="PERSONAL_DEMO",
            reason="synthetic high capacity test", personal_user_id=user.id, duration_minutes=1440)
        release.change_state(session, settings, actor_id=admin.id, operation="worker-enable",
            reason="synthetic worker enable")
        row = session.get(ReleaseControl, 1)
        grant = UUID(row.evidence["grant_id"])
    return SimpleNamespace(db=db, settings=settings, admin=admin.id, user=user.id, other=other.id,
        login=login.id, token=user_login.token, other_token=other_login.token, grant=grant, authorization=result)


def reserve(fixture, key, *, kind="rag", with_job=False):
    with fixture.db.transaction() as session:
        row, reused = QuotaService(session, settings=fixture.settings).reserve(fixture.user, key, "a" * 64, kind)
        if with_job and not reused:
            session.add(AiJob(id=row.id, login_session_id=fixture.login, payload={"question": "synthetic"}))
        return row.id, reused


def complete(fixture, identity):
    with fixture.db.transaction() as session:
        quota = QuotaService(session)
        assert quota.claim(fixture.user, identity)
        quota.finish(fixture.user, identity, outcome="failed")


def seed_released(fixture, count, *, user=None, grant=None, prior_day=False):
    """Bulk synthetic terminal history, only in this fixture's random database."""
    with fixture.db.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        day = now.astimezone(timezone.utc).date() - timedelta(days=int(prior_day))
        rows = [{"id": uuid4(), "user_id": user or fixture.user,
            "personal_demo_grant_id": grant, "idempotency_key": f"seed-{uuid4().hex}",
            "payload_fingerprint": "b" * 64, "kind": "rag", "state": "failed", "utc_day": day,
            "created_at": now, "finished_at": now, "slot_released_at": now, "provider_attempts": 0}
            for _ in range(count)]
        session.execute(insert(AiRequestReservation), rows)


def test_explicit_close_ends_only_new_unclaimed_queue_without_refund(capacity):
    identities = [reserve(capacity, f"queued-before-close-{n}", with_job=True)[0] for n in range(3)]
    with capacity.db.transaction() as session:
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
            reason="explicit synthetic queue closure")
        for identity in identities:
            row = session.get(AiRequestReservation, identity)
            assert row.state == "failed" and row.slot_released_at is not None and row.provider_attempts == 0
            assert row.personal_demo_grant_id == capacity.grant and row.cost_status == "unknown"
            assert session.get(AiJob, identity).failure_type == "grant_ended"
            assert not QuotaService(session).claim(capacity.user, identity)
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 3
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="PERSONAL_DEMO",
            personal_user_id=capacity.user, duration_minutes=1440, reason="explicit new synthetic window")
    for n in range(3):
        reserve(capacity, f"new-window-queue-{n}")
    with pytest.raises(QuotaExceeded):
        reserve(capacity, "fourth-new-window-task")


@pytest.mark.parametrize("original_state", ["running", "unknown"])
def test_close_never_retires_started_or_older_grant_work(capacity, original_state):
    active, _ = reserve(capacity, "already-started-task", with_job=True)
    older, _ = reserve(capacity, "older-grant-queue", with_job=True)
    with capacity.db.transaction() as session:
        row = session.get(AiRequestReservation, active)
        row.state = original_state
        session.get(AiRequestReservation, older).personal_demo_grant_id = uuid4()
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
            reason="explicit preserve uncertain work")
        assert session.get(AiRequestReservation, active).state == original_state
        assert session.get(AiRequestReservation, active).slot_released_at is None
        assert session.get(AiRequestReservation, older).state == "reserved"
        assert session.get(AiRequestReservation, older).slot_released_at is None


def test_queue_cleanup_failure_cannot_block_emergency_closed(capacity, monkeypatch):
    identity, _ = reserve(capacity, "cleanup-rollback-probe", with_job=True)
    def fail_queue(session, _):
        session.execute(text("SELECT 1 / 0"))
    with monkeypatch.context() as patch:
        patch.setattr(release, "_end_unclaimed_personal_queue", fail_queue)
        with capacity.db.transaction() as session:
            result = release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
                reason="synthetic failed queue cleanup")
            assert result["state"] == "CLOSED" and result["worker_enabled"] is False
            assert session.get(AiRequestReservation, identity).state == "reserved"
    with capacity.db.transaction() as session:
        # A later explicit CLOSED can retry only this never-started queue cleanup.
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
            reason="synthetic repaired queue cleanup")
        assert session.get(AiRequestReservation, identity).state == "failed"


def test_invalid_queue_identity_still_allows_closed(capacity):
    with capacity.db.transaction() as session:
        control = session.get(ReleaseControl, 1)
        control.evidence = {**control.evidence, "grant_id": "invalid"}
        result = release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
            reason="synthetic invalid identity closure")
        assert result["state"] == "CLOSED" and result["worker_enabled"] is False


def test_exact_500_tasks_count_failed_history_and_survive_utc_day_change(capacity):
    seed_released(capacity, 499, grant=capacity.grant, prior_day=True)
    identity, reused = reserve(capacity, "capacity-final-500")
    assert not reused
    complete(capacity, identity)
    assert reserve(capacity, "capacity-final-500") == (identity, True)
    with pytest.raises(QuotaExceeded) as error:
        reserve(capacity, "capacity-excess-501")
    assert error.value.reason == "personal_demo_limit"
    with capacity.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation).where(
            AiRequestReservation.personal_demo_grant_id == capacity.grant)) == 500
        assert session.get(AiRequestReservation, identity).state == "failed"


def test_user_utc_daily_1000_includes_previous_grants_without_refund(capacity):
    seed_released(capacity, 999, grant=uuid4())
    identity, _ = reserve(capacity, "capacity-user-1000")
    complete(capacity, identity)
    with pytest.raises(QuotaExceeded) as error:
        reserve(capacity, "capacity-user-1001")
    assert error.value.reason == "user_daily"
    with capacity.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1000
        assert session.scalar(select(func.count()).select_from(AiRequestReservation).where(
            AiRequestReservation.personal_demo_grant_id == capacity.grant)) == 1


def test_global_utc_daily_2000_includes_other_users_without_refund(capacity):
    seed_released(capacity, 1999, user=capacity.other)
    identity, _ = reserve(capacity, "capacity-global-2000")
    complete(capacity, identity)
    with pytest.raises(QuotaExceeded) as error:
        reserve(capacity, "capacity-global-2001")
    assert error.value.reason == "global_daily"
    with capacity.db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 2000


def test_three_queued_slots_are_admitted_and_fourth_is_rejected(capacity):
    identities = [reserve(capacity, f"capacity-queued-{index}", with_job=True)[0] for index in range(3)]
    with pytest.raises(QuotaExceeded) as error:
        reserve(capacity, "capacity-queued-excess")
    assert error.value.reason == "user_concurrency"
    with capacity.db.transaction() as session:
        assert all(session.get(AiRequestReservation, identity).state == "reserved" for identity in identities)


def test_three_slot_admission_is_atomic_under_concurrent_submitters(capacity):
    barrier = Barrier(6)
    def attempt(index):
        barrier.wait(timeout=10)
        try:
            reserve(capacity, f"capacity-racing-{index}")
            return "accepted"
        except QuotaExceeded as error:
            return error.reason
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(attempt, range(6)))
    assert outcomes.count("accepted") == 3 and outcomes.count("user_concurrency") == 3


def test_one_worker_loop_executes_three_jobs_sequentially_with_effective_limits(capacity):
    identities = [reserve(capacity, f"capacity-serial-{index}", with_job=True)[0] for index in range(3)]
    observations = []
    active = 0
    maximum = 0
    def runner(settings, database, job):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        assert database is capacity.db
        observations.append((job.id, settings.agent_max_model_calls, settings.agent_max_tool_calls,
            settings.agent_total_timeout_seconds, settings.agent_tool_timeout_seconds,
            settings.agent_model_timeout_seconds, settings.deepseek_max_tokens, settings.max_provider_attempts_per_job))
        active -= 1
        return SimpleNamespace(generation=None, model_dump=lambda **_: {"status": "answered", "answer": "synthetic"})
    for _ in range(3):
        assert ai_worker.process_one(capacity.db, capacity.settings, runner=runner)
    assert not ai_worker.process_one(capacity.db, capacity.settings, runner=runner)
    assert maximum == 1 and [item[0] for item in observations] == identities
    assert all(item[1:] == (24, 80, 900, 300, 60, 4096, 160) for item in observations)
    assert capacity.settings.agent_max_model_calls == 4 and capacity.settings.agent_max_tool_calls == 6
    assert capacity.settings.deepseek_max_tokens == 1024 and capacity.settings.max_provider_attempts_per_job == 16
    with capacity.db.transaction() as session:
        assert all(session.get(AiRequestReservation, identity).state == "succeeded" and
                   session.get(AiRequestReservation, identity).slot_released_at is not None for identity in identities)


def test_160_accounted_provider_attempts_and_161st_rejected_before_external_call(capacity):
    identity, _ = reserve(capacity, "capacity-provider-budget", with_job=True)
    fake_requests = []
    def runner(settings, database, job):
        for index in range(161):
            with provider_request() as attempt:
                fake_requests.append(index)
                attempt.completed()
        pytest.fail("161st request must not start")
    assert ai_worker.process_one(capacity.db, capacity.settings, runner=runner)
    assert fake_requests == list(range(160))
    with capacity.db.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        assert row.provider_attempts == 160 and row.state == "failed" and row.slot_released_at is not None
        assert session.get(AiJob, identity).failure_type == "provider_budget"
        assert row.usage is None and row.cost_status == "unknown"


def test_24_hour_grant_is_explicit_and_expiry_blocks_jobs_worker_and_other_user(capacity):
    with capacity.db.transaction() as session:
        row = session.get(ReleaseControl, 1)
        started = datetime.fromisoformat(row.evidence["started_at"])
        assert row.expires_at - started == timedelta(hours=24)
        assert row.evidence["profile"] == "personal-demo-v2"
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, capacity.settings, user_id=capacity.other)
    identity, _ = reserve(capacity, "capacity-expiry-queued", with_job=True)
    with capacity.db.transaction() as session:
        session.get(ReleaseControl, 1).expires_at = session.scalar(select(func.clock_timestamp())) - timedelta(seconds=1)
    with pytest.raises(release.ReleaseClosed):
        reserve(capacity, "capacity-expiry-new")
    assert not ai_worker.process_one(capacity.db, capacity.settings,
        runner=lambda *_: pytest.fail("expired task executed"))
    with capacity.db.transaction() as session:
        assert session.get(AiRequestReservation, identity).state == "reserved"


def test_authorization_cannot_exceed_24_hours_or_renew_without_closed(capacity):
    with pytest.raises(ValueError), capacity.db.transaction() as session:
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="PERSONAL_DEMO",
            reason="no implicit renewal", personal_user_id=capacity.user, duration_minutes=1440)
    with capacity.db.transaction() as session:
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="CLOSED",
            reason="synthetic new grant window")
    with pytest.raises(ValueError), capacity.db.transaction() as session:
        release.change_state(session, capacity.settings, actor_id=capacity.admin, operation="PERSONAL_DEMO",
            reason="too long synthetic grant", personal_user_id=capacity.user, duration_minutes=1441)


def test_ui_profile_shows_current_owner_counts_and_no_private_settings(capacity, monkeypatch):
    reserve(capacity, "capacity-profile-first")
    monkeypatch.setattr(main, "Database", lambda *_, **__: capacity.db)
    app = main.create_app(capacity.settings)
    headers = {"X-Project-Copilot-Proxy-Secret": capacity.settings.public_proxy_secret.get_secret_value()}
    with TestClient(app, base_url="https://capacity.example", headers=headers) as client:
        response = client.get("/ui/config", cookies={"__Host-pc_session": capacity.token})
        assert response.status_code == 200
        profile = response.json()["personal_demo"]
        assert profile["job_limit"] == 500 and profile["jobs_used"] == profile["occupied_slots"] == 1
        assert profile["user_daily_limit"] == 1000 and profile["global_daily_limit"] == 2000
        assert profile["concurrent_limit"] == 3 and 0 < profile["remaining_seconds"] <= 86400
        assert profile["expires_at"] and profile["worker_enabled"] is True
        for key in ("database_url", "proxy_secret", "deepseek_api_key", "manifest_dir", "allowed_user_ids"):
            assert key not in response.text
        assert "personal_demo" not in client.get("/ui/config").json()
        assert "personal_demo" not in client.get("/ui/config", cookies={"__Host-pc_session": capacity.other_token}).json()


@pytest.mark.parametrize("capacity", [{"personal_max_provider_attempts_per_job": 1}], indirect=True)
@pytest.mark.parametrize("blocked_stage", ["model", "tool"])
def test_compiled_agent_preserves_provider_budget_through_graph_and_worker(capacity, blocked_stage):
    from project_copilot.agent.graph import AgentRunner
    from project_copilot.agent.service import graph_limits
    from project_copilot.agent.tools import build_readonly_registry
    from project_copilot.db.executions import AiJobExecution
    from project_copilot.schemas.agent import AgentQuestion
    from test_agent_graph import Backend, TICKET_ID

    identity, _ = reserve(capacity, f"compiled-provider-{blocked_stage}", kind="agent", with_job=True)
    sent = []
    graph_results = []
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    class LimitedModel:
        async def ainvoke(self, messages, tools):
            with provider_request() as attempt:
                sent.append("model")
                attempt.completed()
            calls = ([{"id": "read", "name": "get_ticket", "args": {"ticket_id": TICKET_ID}}]
                     if blocked_stage == "model" else [
                         {"id": "embedding", "name": "search_knowledge", "args": {"query": "synthetic"}},
                         {"id": "must_not_run", "name": "get_ticket", "args": {"ticket_id": TICKET_ID}},
                     ])
            return AIMessage(content="", tool_calls=calls, response_metadata={"usage": usage})

    class LimitedBackend(Backend):
        async def search_knowledge(self, query):
            self.calls.append(("search_knowledge", query))
            with provider_request() as attempt:
                sent.append("embedding")
                attempt.completed()
            pytest.fail("provider budget must reject before embedding")

    backend = LimitedBackend()

    def runner(settings, database, job):
        graph = AgentRunner(LimitedModel(), build_readonly_registry(backend), graph_limits(settings))
        result = asyncio.run(graph.run(AgentQuestion(question="synthetic provider budget"), str(job.id)))
        graph_results.append(result)
        return result

    assert ai_worker.process_one(capacity.db, capacity.settings, runner=runner)
    assert sent == ["model"] and len(graph_results) == 1
    result = graph_results[0]
    assert result.status == "budget_exhausted" and result.failure_type == "provider_budget"
    assert result.usage == usage
    if blocked_stage == "model":
        assert result.model_calls == 2 and result.tool_calls == result.tool_executions == 1
    else:
        assert result.model_calls == 1 and result.tool_calls == result.tool_failures == 2
        assert result.tool_executions == 1 and backend.calls == [("search_knowledge", "synthetic")]
    with capacity.db.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        execution = session.get(AiJobExecution, identity)
        assert row.state == "failed" and row.slot_released_at is not None and row.provider_attempts == 1
        assert row.usage == usage and row.cost_status == "unknown"
        assert session.get(AiJob, identity).failure_type == "provider_budget"
        assert execution.provider_open == 0 and execution.provider_uncertain is False
