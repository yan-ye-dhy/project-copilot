"""Isolation tests run against separately provisioned real PostgreSQL, never SQLite."""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from project_copilot.api.tickets import router
from project_copilot.config import Settings
from project_copilot.db.models import Ticket
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.schemas.tickets import TicketCreate, TicketFilters
from project_copilot.services.auth import AuthService, Principal, csrf_token
from project_copilot.services.tickets import TicketNotFoundError, TicketService


PASSWORD = "unit-test-password-4231"


def seed(db):
    with db.transaction() as session:
        auth = AuthService(session)
        admin = auth.bootstrap_admin(RegisterInput(username="admin", password=PASSWORD))
        alice = auth.register(RegisterInput(username="alice", password=PASSWORD), open_registration=True)
        bob = auth.register(RegisterInput(username="bob", password=PASSWORD), open_registration=True)
        issued = {name: auth.login(LoginInput(username=name, password=PASSWORD)).token
                  for name in ("admin", "alice", "bob")}
    principals = {user.username: Principal(id=user.id, username=user.username, role=user.role)
                  for user in (admin, alice, bob)}
    with db.transaction() as session:
        a = TicketService(session, principal=principals["alice"], enforce_ownership=True).create(
            TicketCreate(title="共享关键词 Alice", description="属于 Alice"))
        b = TicketService(session, principal=principals["bob"], enforce_ownership=True).create(
            TicketCreate(title="共享关键词 Bob", description="属于 Bob"))
        old = TicketService(session).create(TicketCreate(title="共享关键词 历史", description="无账户的旧工单"))
    return principals, issued, a, b, old


def app_for(db):
    app = FastAPI()
    # Router-only application deliberately omits the public gateway: production
    # remains fail-closed until all public security controls are complete.
    app.state.settings = Settings(_env_file=None, public_demo=True,
                                  public_proxy_secret="a" * 32,
                                  auth_public_origin="https://demo.example")
    app.state.database = db
    app.include_router(router)
    return app


def test_owner_filter_is_sql_bound_for_user_and_admin_can_see_all():
    user_id = uuid4()
    user = Principal(id=user_id, username="alice", role="user")
    admin = Principal(id=uuid4(), username="admin", role="admin")
    scoped = TicketService(None, principal=user, enforce_ownership=True)._scope(select(Ticket))
    compiled = scoped.compile(dialect=postgresql.dialect())
    assert "tickets.owner_user_id =" in str(compiled)
    assert user_id in compiled.params.values()
    assert "tickets.owner_user_id =" not in str(
        TicketService(None, principal=admin, enforce_ownership=True)._scope(select(Ticket))
    )
    with pytest.raises(ValueError):
        TicketService(None, enforce_ownership=True)


def test_service_queries_scope_before_pagination_and_keep_legacy_admin_only(ticket_database):
    db = ticket_database
    principals, _, a, b, old = seed(db)
    with db.transaction() as session:
        with pytest.raises(ValueError):
            TicketService(session, enforce_ownership=True)
        alice = TicketService(session, principal=principals["alice"], enforce_ownership=True)
        assert alice.get(a.id).title == a.title
        for ticket_id in (b.id, old.id, uuid4()):
            with pytest.raises(TicketNotFoundError):
                alice.get(ticket_id)
        assert [item.id for item in alice.search(TicketFilters(q="共享关键词", limit=1)).items] == [a.id]
        assert alice.search(TicketFilters(q="共享关键词", limit=1, offset=1)).items == ()
        admin = TicketService(session, principal=principals["admin"], enforce_ownership=True)
        assert {item.id for item in admin.search(TicketFilters()).items} == {a.id, b.id, old.id}
        assert admin.get(old.id).id == old.id
        assert session.get(Ticket, a.id).owner_user_id == principals["alice"].id
        assert session.get(Ticket, b.id).owner_user_id == principals["bob"].id
        assert session.get(Ticket, old.id).owner_user_id is None
    assert db.engine.pool.checkedout() == 0


def test_public_http_requires_session_and_cannot_create_or_read_others(ticket_database):
    db = ticket_database
    _, tokens, a, b, old = seed(db)
    with TestClient(app_for(db)) as client:
        assert client.get("/tickets").status_code == 401
        alice_cookie = {"__Host-pc_session": tokens["alice"]}
        admin_cookie = {"__Host-pc_session": tokens["admin"]}
        assert client.get("/tickets", cookies=alice_cookie).json()["items"][0]["id"] == str(a.id)
        for ticket_id in (b.id, old.id):
            assert client.get(f"/tickets/{ticket_id}", cookies=alice_cookie).status_code == 404
        assert client.get(f"/tickets/{old.id}", cookies=admin_cookie).status_code == 200
        payload = {"title": "非法直建", "description": "应由确认流程创建"}
        assert client.post("/tickets", json=payload, cookies=alice_cookie).status_code == 403
        headers = {"Origin": "https://demo.example", "X-CSRF-Token": csrf_token(tokens["alice"])}
        response = client.post("/tickets", json=payload, cookies=alice_cookie, headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "请通过工单提案确认创建"
    with db.transaction() as session:
        assert len(session.scalars(select(Ticket)).all()) == 3
    assert db.engine.pool.checkedout() == 0
