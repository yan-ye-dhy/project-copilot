"""Real PostgreSQL/compiled-graph isolation; model choices are controlled test doubles."""
import asyncio
from contextlib import asynccontextmanager
import json
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import func, select

from project_copilot.agent import service as agent_service
from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.api.actions import router as actions_router
from project_copilot.api.agent import router as agent_router
from project_copilot.config import Settings
from project_copilot.db.models import LoginSession, PendingAction, Ticket
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.auth import AuthService, Principal, csrf_token
from project_copilot.services.tickets import TicketService


PASSWORD = "test-password-for-isolation-824"


def seed(db):
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=PASSWORD))
        alice = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        bob = auth.register(RegisterInput(username="bob", password=PASSWORD), open_registration=True)
        tokens = {username: auth.login(LoginInput(username=username, password=PASSWORD)).token
                  for username in ("admin", "alice", "bob")}
    principals = {user.username: Principal(id=user.id, username=user.username, role=user.role)
                  for user in (admin, alice, bob)}
    return principals, tokens


def settings():
    return Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32,
                    auth_public_origin="https://demo.example")


def public_headers(token, context=None):
    headers = {"Origin": "https://demo.example", "X-CSRF-Token": csrf_token(token)}
    if context is not None:
        headers.update({"X-Copilot-Context-Id": context["context_id"],
                        "X-Copilot-Context-Token": context["context_token"]})
    return headers


def app_for(db):
    # The actual gateway intentionally remains closed; this isolated router app
    # exercises the same handlers without asserting that the site is deployable.
    app = FastAPI()
    app.state.settings, app.state.database = settings(), db

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.run_id = "test-public-ownership"
        return await call_next(request)

    app.include_router(actions_router)
    app.include_router(agent_router)
    return app


class ProposeModel:
    async def ainvoke(self, messages, tools):
        return AIMessage(content="", tool_calls=[{"name": "propose_ticket", "id": "controlled_propose",
                         "args": {"title": "Alice 的提案", "description": "需要确认后创建"}}])


def test_context_token_alone_cannot_cross_users_and_confirm_is_idempotent(ticket_database, monkeypatch):
    db = ticket_database
    principals, tokens = seed(db)

    @asynccontextmanager
    async def fake_model(_settings):
        yield ProposeModel()

    monkeypatch.setattr(agent_service, "create_agent_model", fake_model)
    with TestClient(app_for(db)) as client:
        cookies = {name: {"__Host-pc_session": token} for name, token in tokens.items()}
        response = client.post("/agent/contexts", cookies=cookies["alice"], headers=public_headers(tokens["alice"]))
        assert response.status_code == 201
        context = response.json()
        alice_headers = public_headers(tokens["alice"], context)
        async def run_agent():
            async with agent_service.create_agent_service(settings(), db,
                    context=(UUID(context["context_id"]), context["context_token"]), auth_token=tokens["alice"]) as service:
                return await service.run(AgentQuestion(question="请提出工单"), run_id="test-public-proposal")
        alice_agent = asyncio.run(run_agent())
        assert alice_agent.status == "pending_confirmation"
        action = alice_agent.sources[0].data
        decision = {"payload_fingerprint": action["payload_fingerprint"]}
        for user in ("bob", "admin"):
            stolen_context = public_headers(tokens[user], context)
            assert client.post("/actions/preview", cookies=cookies[user], headers=stolen_context,
                               json={"title": "跨用户", "description": "不允许"}).status_code == 403
            assert client.get("/actions/" + action["action_id"], cookies=cookies[user],
                              headers=stolen_context).status_code == 403
            assert client.post("/actions/" + action["action_id"] + "/confirm", cookies=cookies[user],
                               headers=stolen_context, json=decision).status_code == 403
            assert client.post("/agent/ask", cookies=cookies[user], headers=stolen_context,
                               json={"question": "请提出工单"}).status_code == 410
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        for _ in range(2):
            confirmation = client.post("/actions/" + action["action_id"] + "/confirm", cookies=cookies["alice"],
                                       headers=alice_headers, json=decision)
            assert confirmation.status_code == 200
            assert confirmation.json()["status"] == "completed"
            if _ == 0:
                ticket_id = confirmation.json()["ticket_id"]
            else:
                assert confirmation.json()["ticket_id"] == ticket_id
    with db.transaction() as session:
        ticket = session.get(Ticket, ticket_id)
        action_row = session.get(PendingAction, action["action_id"])
        assert ticket.owner_user_id == principals["alice"].id
        assert action_row.owner_user_id == principals["alice"].id
        assert ticket.creation_key == action_row.id
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1


class SearchModel:
    def __init__(self):
        self.seen = None

    async def ainvoke(self, messages, tools):
        tool_results = [item for item in messages if isinstance(item, ToolMessage)]
        if not tool_results:
            return AIMessage(content="", tool_calls=[{"name": "search_tickets", "id": "controlled_search",
                             "args": {"q": "关键词", "limit": 10}}])
        self.seen = json.loads(tool_results[-1].content)
        sources = self.seen.get("sources", [])
        if sources:
            result = {"status": "answered", "answer": "工单查询完成。", "source_ids": [x["id"] for x in sources]}
        else:
            result = {"status": "insufficient_evidence", "answer": "没有可用工单。", "source_ids": []}
        return AIMessage(content=json.dumps(result, ensure_ascii=False))


def test_compiled_agent_search_is_scoped_and_admin_sees_legacy(ticket_database):
    db = ticket_database
    principals, tokens = seed(db)
    with db.transaction() as session:
        alice = TicketService(session, principal=principals["alice"], enforce_ownership=True).create(
            TicketCreate(title="关键词 Alice", description="Alice 工单"))
        bob = TicketService(session, principal=principals["bob"], enforce_ownership=True).create(
            TicketCreate(title="关键词 Bob", description="Bob 工单"))
        old = TicketService(session).create(TicketCreate(title="关键词 旧工单", description="遗留工单"))
    for username, expected in (("alice", {alice.id}), ("bob", {bob.id}), ("admin", {alice.id, bob.id, old.id})):
        model = SearchModel()
        backend = ReadOnlyBackend(settings(), db, auth_token=tokens[username])
        runner = AgentRunner(model, build_readonly_registry(backend))
        assert type(runner.graph).__name__ == "CompiledStateGraph"
        result = asyncio.run(runner.run(AgentQuestion(question="搜索关键词工单"), run_id="controlled-search"))
        assert result.status == "answered" and result.events[0].status == "ok"
        assert {source.data["id"] for source in result.sources} == {str(value) for value in expected}
        assert {item["id"] for item in model.seen["data"]} == {str(value) for value in expected}


def test_session_revoked_before_tool_execution_does_not_leak_results(ticket_database):
    db = ticket_database
    principals, tokens = seed(db)
    with db.transaction() as session:
        TicketService(session, principal=principals["alice"], enforce_ownership=True).create(
            TicketCreate(title="关键词私人工单", description="仅 Alice 可读"))

    class RevokeBeforeTool(SearchModel):
        async def ainvoke(self, messages, tools):
            if not any(isinstance(item, ToolMessage) for item in messages):
                with db.transaction() as session:
                    service = AuthService(session)
                    _, login = service.authenticate(tokens["alice"], for_update=True)
                    service.logout(login)
            return await super().ainvoke(messages, tools)

    model = RevokeBeforeTool()
    backend = ReadOnlyBackend(settings(), db, auth_token=tokens["alice"])
    result = asyncio.run(AgentRunner(model, build_readonly_registry(backend)).run(
        AgentQuestion(question="查找关键词工单"), run_id="revoked-before-tool"))
    assert result.events[0].status == "error" and result.events[0].failure_type == "tool_exception"
    assert result.sources == [] and model.seen["status"] == "error"
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(LoginSession).where(LoginSession.revoked_at.is_not(None))) == 1
