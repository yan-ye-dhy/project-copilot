"""阶段C：真实PostgreSQL与实际FastAPI路由，模型接口完全不参与。"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

from project_copilot.config import Settings
from project_copilot.db.models import Ticket
from project_copilot.main import create_app
from project_copilot.schemas.tickets import TicketRead


def request_db(database, method, path, **kwargs):
    async def run():
        settings = Settings(_env_file=None, environment="test", database_url=database.engine.url.render_as_string(hide_password=False))
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
                response = await client.request(method, path, **kwargs)
                assert app.state.database.engine.pool.checkedout() == 0
                return response
    return asyncio.run(run())


def test_post_detail_and_list_match_independent_database_connection(ticket_database):
    db = ticket_database
    response = request_db(db, "POST", "/tickets", json={"title": " 中文上传故障 ", "description": " 复现HTTP 422 "})
    assert response.status_code == 201
    created = TicketRead.model_validate(response.json())
    assert created.title == "中文上传故障" and created.status.value == "open"
    assert created.created_at.tzinfo is not None and created.created_at == created.updated_at
    with db.transaction() as session:
        stored = session.get(Ticket, created.id)
        assert TicketRead.model_validate(stored) == created
    detail = request_db(db, "GET", f"/tickets/{created.id}")
    assert detail.status_code == 200 and detail.json() == response.json()
    listing = request_db(db, "GET", "/tickets")
    assert listing.status_code == 200 and listing.json() == {"items": [response.json()], "limit": 20, "offset": 0}


def test_status_keywords_case_and_literal_wildcards_filter_real_rows(ticket_database):
    db = ticket_database
    with db.transaction() as s:
        s.add_all([
            Ticket(title="Upload 中文故障", description="HTTP 422", status="open"),
            Ticket(title="历史工单", description="UPLOAD 重复问题", status="closed"),
            Ticket(title="进度50%_完成", description="显示字面百分号", status="in_progress"),
        ])
    def search(**params):
        result = request_db(db, "GET", "/tickets", params=params)
        assert result.status_code == 200
        return result.json()["items"]
    assert len(search(q="upload")) == 2
    assert [x["status"] for x in search(q="UPLOAD", status="closed")] == ["closed"]
    assert len(search(q="中文")) == 1
    assert len(search(q="%_")) == 1
    assert search(q="' OR 1=1; --") == []
    assert search(q="没有这个词") == []
    assert len(search(limit=1, offset=1)) == 1
    assert search(limit=1, offset=3) == []


@pytest.mark.parametrize("payload", [
    {}, {"title": " ", "description": "正常"}, {"title": "x" * 201, "description": "正常"},
    {"title": "正常", "description": "x" * 10001}, {"title": True, "description": "正常"},
    {"title": "正常", "description": "正常", "status": "closed"},
    {"title": "bad\x00input", "description": "不可由PG存储的NUL应先返回422"},
])
def test_invalid_post_has_422_and_no_database_rows(ticket_database, payload):
    response = request_db(ticket_database, "POST", "/tickets", json=payload)
    assert response.status_code == 422
    with ticket_database.transaction() as s:
        assert s.scalar(text("SELECT count(*) FROM tickets")) == 0


@pytest.mark.parametrize("path", ["/tickets/not-a-uuid", "/tickets?status=invalid", "/tickets?limit=101",
                                      "/tickets?offset=-1", "/tickets?q=%20%20", "/tickets?q=%00", "/tickets?unknown=value"])
def test_invalid_path_or_query_has_422(ticket_database, path):
    assert request_db(ticket_database, "GET", path).status_code == 422


def test_missing_ticket_is_404_and_next_request_still_works(ticket_database):
    response = request_db(ticket_database, "GET", f"/tickets/{uuid4()}")
    assert response.status_code == 404 and response.json() == {"detail": "工单不存在"}
    assert request_db(ticket_database, "GET", "/tickets").json()["items"] == []


def test_unconfigured_database_keeps_health_working_but_tickets_returns_503():
    async def run():
        app = create_app(Settings(_env_file=None, database_url=None, environment="test"))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/health")).status_code == 200
                response = await client.get("/tickets")
                assert response.status_code == 503 and response.headers["x-run-id"]
    asyncio.run(run())


# Stage D: the following cases use PostgreSQL transactions, not mock commits.
def test_service_failure_after_two_flushes_rolls_back_both_tickets(ticket_database):
    from project_copilot.schemas.tickets import TicketCreate
    from project_copilot.services.tickets import TicketService

    with pytest.raises(RuntimeError):
        with ticket_database.transaction() as session:
            service = TicketService(session)
            one = service.create(TicketCreate(title="第一步", description="已flush"))
            two = service.create(TicketCreate(title="第二步", description="已flush"))
            assert one.id != two.id
            assert session.scalar(text("SELECT count(*) FROM tickets")) == 2
            raise RuntimeError("controlled business error")
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM tickets")) == 0
    assert ticket_database.engine.pool.checkedout() == 0


def test_http_business_error_after_flush_returns_500_and_rolls_back(ticket_database, monkeypatch, caplog):
    from project_copilot.services.tickets import TicketService
    original = TicketService.create

    def fail(self, payload):
        original(self, payload)
        raise RuntimeError("private-postgres-business-error")

    monkeypatch.setattr(TicketService, "create", fail)
    response = request_db(ticket_database, "POST", "/tickets", json={"title": "不得保留", "description": "flush后失败"})
    assert response.status_code == 500 and response.headers["x-run-id"]
    assert "private-postgres-business-error" not in response.text + caplog.text
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM tickets")) == 0


def test_real_commit_failure_becomes_503_before_201_and_does_not_leave_duplicate(ticket_database, caplog):
    # Isolated test-only deferred constraint forces a genuine COMMIT failure after flush succeeded.
    with ticket_database.transaction() as session:
        session.execute(text("ALTER TABLE tickets ADD CONSTRAINT uq_test_title UNIQUE (title) DEFERRABLE INITIALLY DEFERRED"))
    payload = {"title": "重复标题", "description": "延迟约束让失败发生于commit而非flush"}
    assert request_db(ticket_database, "POST", "/tickets", json=payload).status_code == 201
    response = request_db(ticket_database, "POST", "/tickets", json=payload)
    assert response.status_code == 503
    assert "INSERT" not in response.text and "uq_test_title" not in response.text + caplog.text
    with ticket_database.transaction() as session:
        assert session.scalar(text("SELECT count(*) FROM tickets")) == 1


def test_missing_migration_is_503_not_a_successful_empty_list(postgres_database):
    response = request_db(postgres_database, "GET", "/tickets")
    assert response.status_code == 503
    assert "SELECT" not in response.text and "UndefinedTable" not in response.text


def test_real_connection_failure_is_503_with_secret_and_sql_sanitized(ticket_database, caplog):
    import socket
    from project_copilot.db.session import Database

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))  # This owned bound-but-not-listening port refuses connections.
        url = ticket_database.engine.url.set(port=sock.getsockname()[1])
        unavailable = Database(url.render_as_string(hide_password=False), connect_timeout=1)
        try:
            response = request_db(unavailable, "GET", "/tickets")
        finally:
            unavailable.close()
    assert response.status_code == 503
    assert "local-test-only" not in response.text + caplog.text
    assert "connection refused" not in response.text.lower()


def test_controlled_rollback_diagnostic_does_not_remove_existing_ticket(ticket_database):
    from project_copilot.schemas.tickets import TicketCreate
    from project_copilot.services.ticket_checks import verify_rollback
    from project_copilot.services.tickets import TicketService

    with ticket_database.transaction() as session:
        saved = TicketService(session).create(TicketCreate(title="原有成功工单", description="必须保留"))
    result = verify_rollback(ticket_database)
    assert result["flushed_before_failure"] and not result["visible_after_rollback"]
    assert result["pool_checked_out_after"] == 0
    with ticket_database.transaction() as session:
        assert session.get(Ticket, saved.id).title == "原有成功工单"
        assert session.scalar(text("SELECT count(*) FROM tickets")) == 1
