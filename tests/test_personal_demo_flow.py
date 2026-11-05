"""One PERSONAL HTTP/PG pipeline with controlled RAG and Agent model decisions.

Real app/gateway, password sessions, quotas, compiled LangGraph, proposal service
and PostgreSQL transactions run. There is no real provider, Qdrant or BGE call.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import func, select

from project_copilot import main
from project_copilot.agent import service as agent_service
from project_copilot.config import Settings
from project_copilot.db.models import AiRequestReservation, PendingAction, Ticket
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.schemas.auth import RegisterInput
from project_copilot.services import ai_worker, release_control as release
from project_copilot.services.auth import AuthService


def test_personal_http_jobs_compiled_proposal_confirm_and_close(ticket_database, monkeypatch):
    db = ticket_database
    settings = Settings(_env_file=None, environment="production", public_demo=True,
                        public_proxy_secret="synthetic_proxy_" + "a" * 40,
                        auth_public_origin="https://personal-flow.example",
                        database_url="postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public")
    password = "synthetic-flow-password-4319"
    monkeypatch.setattr(release, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="flow_admin", password=password))
        alice = auth.register(RegisterInput(username="flow_alice", password=password), open_registration=True)
        release.change_state(session, settings, actor_id=admin.id, operation="PERSONAL_DEMO",
                             reason="synthetic personal HTTP flow", personal_user_id=alice.id, duration_minutes=90)
        release.change_state(session, settings, actor_id=admin.id, operation="worker-enable",
                             reason="synthetic personal flow worker")

    class ProposalModel:
        async def ainvoke(self, messages, tools):
            record_provider_attempt()  # Intercept accounting only; never sends network traffic.
            return AIMessage(content="", tool_calls=[{
                "name": "propose_ticket", "id": "controlled_personal_proposal",
                "args": {"title": "个人体验提案", "description": "测试独立确认与幂等。"},
            }])

    @asynccontextmanager
    async def controlled_model(_settings):
        yield ProposalModel()

    monkeypatch.setattr(agent_service, "create_agent_model", controlled_model)
    # The Settings DSN is only an isolated-deployment identity contract; actual
    # application I/O stays on ticket_database's newly-created random test DB.
    monkeypatch.setattr(main, "Database", lambda *args, **kwargs: db)
    app = main.create_app(settings)
    seen = []

    def controlled_runner(active, database, job):
        seen.append(job.kind)
        if job.kind == "rag":
            record_provider_attempt()
            return SimpleNamespace(generation=None, model_dump=lambda **_: {
                "status": "answered", "answer": "受控RAG结果；未调用实际检索或模型。", "citations": []})
        return ai_worker.execute(active, database, job)  # Real compiled graph and proposal backend.

    origin = str(settings.auth_public_origin).rstrip("/")
    proxy = {"X-Project-Copilot-Proxy-Secret": settings.public_proxy_secret.get_secret_value()}
    with TestClient(app, base_url=origin, headers=proxy) as client:
        login = client.post("/auth/login", headers={"Origin": origin},
                            json={"username": "flow_alice", "password": password})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert all(part in cookie for part in ("Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        assert "Domain=" not in cookie
        assert client.get("/auth/me").json()["user"]["id"] == str(alice.id)
        write = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        assert client.post("/agent/contexts", headers={"Origin": origin}).status_code == 403
        assert client.post("/tickets", headers=write,
                           json={"title": "绕过确认", "description": "不允许直接写入。"}).status_code == 403

        rag = client.post("/jobs", headers={**write, "Idempotency-Key": "personal-flow-rag-001"},
                          json={"kind": "rag", "payload": {"question": "合成知识问题"}})
        assert rag.status_code == 202
        rag_id = rag.json()["job_id"]
        assert client.get("/jobs/" + rag_id).json()["status"] == "reserved"
        assert ai_worker.process_one(db, settings, runner=controlled_runner)
        assert client.get("/jobs/" + rag_id).json()["result"]["status"] == "answered"

        context_response = client.post("/agent/contexts", headers=write)
        assert context_response.status_code == 201
        context = context_response.json()
        scoped = {**write, "X-Copilot-Context-Id": context["context_id"],
                  "X-Copilot-Context-Token": context["context_token"]}
        agent = client.post("/jobs", headers={**scoped, "Idempotency-Key": "personal-flow-agent-001"},
                            json={"kind": "agent", "payload": {"question": "请提出一个工单。"}})
        assert agent.status_code == 202
        agent_id = agent.json()["job_id"]
        assert ai_worker.process_one(db, settings, runner=controlled_runner)
        completed = client.get("/jobs/" + agent_id).json()
        assert completed["status"] == "succeeded"
        result = completed["result"]
        assert result["status"] == "pending_confirmation" and result["events"][0]["status"] == "ok"
        proposal = result["sources"][0]["data"]
        action_path = "/actions/" + proposal["action_id"]
        preview = client.get(action_path, headers=scoped)
        assert preview.status_code == 200 and preview.json()["ticket_id"] is None
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        decision = {"payload_fingerprint": proposal["payload_fingerprint"]}
        first = client.post(action_path + "/confirm", headers=scoped, json=decision)
        second = client.post(action_path + "/confirm", headers=scoped, json=decision)
        assert first.status_code == second.status_code == 200
        ticket_id = first.json()["ticket_id"]
        assert ticket_id and second.json()["ticket_id"] == ticket_id
        assert client.get("/tickets/" + ticket_id).json()["title"] == "个人体验提案"
        with db.transaction() as session:
            rows = session.scalars(select(AiRequestReservation)).all()
            assert len(rows) == 2 and all(row.personal_demo_grant_id is not None for row in rows)
            assert len({row.personal_demo_grant_id for row in rows}) == 1
            assert all(row.state == "succeeded" and row.provider_attempts == 1 for row in rows)
            ticket = session.get(Ticket, UUID(ticket_id))
            action = session.get(PendingAction, UUID(proposal["action_id"]))
            assert ticket.owner_user_id == action.owner_user_id == alice.id
            assert ticket.creation_key == action.id
            assert session.scalar(select(func.count()).select_from(Ticket)) == 1
            assert release.release_status(session)["personal_demo"]["jobs_used"] == 2
            release.change_state(session, settings, actor_id=admin.id, operation="CLOSED",
                                 reason="synthetic personal flow complete")
        assert client.get("/jobs/" + agent_id).status_code == 503
        assert not ai_worker.process_one(db, settings, runner=lambda *_: pytest.fail("CLOSED worker executed"))
    assert seen == ["rag", "agent"]
