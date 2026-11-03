"""Job HTTP/worker contracts against isolated PostgreSQL and controlled runners."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage, SystemMessage
from openai import AsyncOpenAI, OpenAI
from sqlalchemy import select

from project_copilot.api.jobs import router
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure.call_accounting import account_job, record_provider_attempt
from project_copilot.infrastructure.agent_model import DeepSeekToolModel
from project_copilot.infrastructure.models import EmbeddingRequestError, ZhipuEmbeddings
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.ai_worker import claim_one, process_one
from project_copilot.services.auth import AuthService, AuthenticationRejected, csrf_token


PASSWORD = "test-password-86420"
ORIGIN = "https://demo.example"


def worker_settings():
    return Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32, auth_public_origin=ORIGIN)


def seed(db):
    with db.transaction() as session:
        auth = AuthService(session)
        alice = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        bob = auth.register(RegisterInput(username="bobby", password=PASSWORD), open_registration=True)
        tokens = {name: auth.login(LoginInput(username=name, password=PASSWORD)).token for name in ("alice", "bobby")}
    return alice, bob, tokens


def client_for(db):
    app = FastAPI()
    # Router-only test deliberately does not remove the production gateway guard.
    app.state.settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32, auth_public_origin=ORIGIN)
    app.state.database = db
    app.include_router(router)
    return TestClient(app, base_url=ORIGIN)


def headers(token, key="request-key-1"):
    return {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(token), "Idempotency-Key": key}


def cookies(token):
    return {"__Host-pc_session": token}


def submit(client, token, question="问题一", key="request-key-1"):
    return client.post("/jobs", json={"kind": "rag", "payload": {"question": question}}, cookies=cookies(token), headers=headers(token, key))


def test_job_authentication_ownership_idempotency_and_csrf(ticket_database):
    db = ticket_database
    _, _, tokens = seed(db)
    with client_for(db) as client:
        assert client.post("/jobs", json={"kind": "rag", "payload": {"question": "问题"}}, headers=headers(tokens["alice"])).status_code == 401
        bad = headers(tokens["alice"])
        bad["Origin"] = "https://evil.example"
        assert client.post("/jobs", json={"kind": "rag", "payload": {"question": "问题"}}, cookies=cookies(tokens["alice"]), headers=bad).status_code == 403
        bad = headers(tokens["alice"])
        bad["X-CSRF-Token"] = "incorrect"
        assert client.post("/jobs", json={"kind": "rag", "payload": {"question": "问题"}}, cookies=cookies(tokens["alice"]), headers=bad).status_code == 403
        created = submit(client, tokens["alice"])
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        assert submit(client, tokens["alice"]).json()["job_id"] == job_id
        assert submit(client, tokens["alice"], question="变更载荷").status_code == 409
        assert submit(client, tokens["alice"], key="another-key").status_code == 429
        assert client.get(f"/jobs/{job_id}", cookies=cookies(tokens["bobby"])).status_code == 404
        assert client.get(f"/jobs/{job_id}", cookies=cookies(tokens["alice"])).json()["status"] == "reserved"
    with db.transaction() as session:
        stored = session.get(AiJob, UUID(job_id))
        assert stored.payload == {"question": "问题一"}
        assert tokens["alice"] not in str(stored.payload)
    assert db.engine.pool.checkedout() == 0


def test_workers_claim_once_and_crash_does_not_restart(ticket_database):
    db = ticket_database
    _, _, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])
    barrier = Barrier(5)

    def worker(_):
        barrier.wait(timeout=10)
        return claim_one(db)

    with ThreadPoolExecutor(max_workers=5) as pool:
        claimed = list(pool.map(worker, range(5)))
    assert [item.id for item in claimed if item is not None] == [job_id]
    # A worker that disappears after claim leaves running work occupied; no retry.
    assert claim_one(db) is None
    with client_for(db) as client:
        assert submit(client, tokens["alice"], key="another-key").status_code == 429
    assert db.engine.pool.checkedout() == 0


def test_revoked_session_never_calls_model_and_stays_unknown(ticket_database, monkeypatch):
    import project_copilot.services.ai_worker as worker

    db = ticket_database
    _, _, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])
    with db.transaction() as session:
        auth = AuthService(session)
        _, login = auth.authenticate(tokens["alice"], for_update=True)
        auth.logout(login)
    invoked = []
    monkeypatch.setattr(worker, "create_answer_service", lambda *args: invoked.append(True))
    assert process_one(db, worker_settings())
    assert invoked == []
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.state == "unknown" and reservation.provider_attempts == 0
    assert db.engine.pool.checkedout() == 0


def test_provider_accounting_propagates_to_async_and_to_thread(ticket_database):
    db = ticket_database
    alice, _, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])
    assert claim_one(db).id == job_id

    async def calls():
        record_provider_attempt()
        await asyncio.to_thread(record_provider_attempt)

    with account_job(db, alice.id, job_id):
        asyncio.run(calls())
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == 2 and reservation.state == "running"
    assert db.engine.pool.checkedout() == 0


def test_revocation_blocks_future_provider_initiation_and_owner_is_checked(ticket_database):
    db = ticket_database
    alice, bob, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])
    assert claim_one(db).id == job_id
    with account_job(db, bob.id, job_id), pytest.raises(AuthenticationRejected):
        record_provider_attempt()
    with account_job(db, alice.id, job_id):
        record_provider_attempt()
        with db.transaction() as session:
            auth = AuthService(session)
            _, login = auth.authenticate(tokens["alice"], for_update=True)
            auth.logout(login)
        with pytest.raises(AuthenticationRejected):
            record_provider_attempt()
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == 1 and reservation.state == "running"
    assert db.engine.pool.checkedout() == 0


def test_controlled_success_is_persisted_and_never_replayed(ticket_database):
    db = ticket_database
    _, _, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])

        class FakeResult:
            generation = SimpleNamespace(usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11})

            def model_dump(self, *, mode):
                return {"status": "answered", "answer": "受控测试结果"}

        def runner(settings, database, job):
            record_provider_attempt()
            return FakeResult()

        assert process_one(db, worker_settings(), runner=runner)
        assert not process_one(db, worker_settings(), runner=runner)
        assert client.get(f"/jobs/{job_id}", cookies=cookies(tokens["alice"])).json() == {
            "job_id": str(job_id), "kind": "rag", "status": "succeeded", "result_expired": False,
            "result": {"status": "answered", "answer": "受控测试结果"},
        }
        assert submit(client, tokens["alice"]).json()["job_id"] == str(job_id)
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == 1
        assert reservation.usage == {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
        assert reservation.cost_status == "unknown" and reservation.finished_at is not None
    assert db.engine.pool.checkedout() == 0


def test_timed_out_tool_keeps_user_slot_and_does_not_store_answer(ticket_database):
    db = ticket_database
    _, _, tokens = seed(db)
    with client_for(db) as client:
        response = client.post("/jobs", json={"kind": "agent", "payload": {"question": "问题"}},
                               cookies=cookies(tokens["alice"]), headers=headers(tokens["alice"]))
        job_id = UUID(response.json()["job_id"])

        class FakeResult:
            status = "answered"
            usage = {"prompt_tokens": 9}
            events = [SimpleNamespace(failure_type="tool_timeout")]
            model_events = []

            def model_dump(self, *, mode):
                return {"status": self.status}

        assert process_one(db, worker_settings(), runner=lambda *_: FakeResult())
        status = client.get(f"/jobs/{job_id}", cookies=cookies(tokens["alice"])).json()
        assert status["status"] == "unknown" and status["result"] is None
        assert submit(client, tokens["alice"], key="another-key").status_code == 429
    assert db.engine.pool.checkedout() == 0


def test_real_provider_adapters_record_initiations_using_mock_transport(ticket_database):
    db = ticket_database
    alice, _, tokens = seed(db)
    with client_for(db) as client:
        job_id = UUID(submit(client, tokens["alice"]).json()["job_id"])
    assert claim_one(db).id == job_id
    seen = []

    def respond(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"model": "fake-chat", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "answer"}}]})
        if len(seen) == 2:
            return httpx.Response(200, json={"model": "fake-embedding", "data": [{"index": 0, "embedding": [1.0, 2.0]}]})
        return httpx.Response(503, json={"error": {"message": "controlled failure"}})

    async def agent_call():
        transport = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        provider = AsyncOpenAI(api_key="test-only", base_url="https://example.invalid/v1", max_retries=0, http_client=transport)
        try:
            model = DeepSeekToolModel(provider, model="fake-chat", temperature=0, max_tokens=32)
            schema = [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}]
            await model.ainvoke([SystemMessage(content="测试规则"), HumanMessage(content="测试")], schema)
        finally:
            await provider.close()

    with account_job(db, alice.id, job_id):
        asyncio.run(agent_call())
        transport = httpx.Client(transport=httpx.MockTransport(respond))
        provider = OpenAI(api_key="test-only", base_url="https://example.invalid/v1", max_retries=0, http_client=transport)
        model = ZhipuEmbeddings(provider, model="fake-embedding", expected_dimensions=2, batch_size=1)
        try:
            assert model.embed_query("测试") == [1.0, 2.0]
            with pytest.raises(EmbeddingRequestError):
                model.embed_query("失败")
        finally:
            model.close()
    with db.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        assert reservation.provider_attempts == len(seen) == 3
    assert db.engine.pool.checkedout() == 0
