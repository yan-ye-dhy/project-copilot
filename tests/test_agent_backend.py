"""真实PostgreSQL只读工具边界；不读取.env，不使用SQLite或假commit。"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.config import Settings
from project_copilot.db.models import Ticket
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.tickets import TicketNotFoundError, TicketService


def backend_for(database):
    return ReadOnlyBackend(Settings(_env_file=None, environment="test", database_url=None), database)


def seed(database):
    with database.transaction() as session:
        service = TicketService(session)
        expected = service.create(TicketCreate(title="上传100%失败", description="排查上传HTTP422"))
        service.create(TicketCreate(title="上传1000失败", description="不能被字面百分号匹配"))
    return expected


def test_agent_backend_returns_actual_postgres_ticket_and_literal_filter(ticket_database):
    expected = seed(ticket_database)
    backend = backend_for(ticket_database)

    async def run():
        result = await backend.get_ticket(expected.id)
        assert result["data"] == expected.model_dump(mode="json")
        assert result["sources"] == [{"id": f"ticket:{expected.id}", "kind": "ticket",
                                      "data": expected.model_dump(mode="json")}]
        result = await backend.search_tickets(status="open", q="100%", limit=5)
        assert result["data"] == [expected.model_dump(mode="json")]
        assert [source["id"] for source in result["sources"]] == [f"ticket:{expected.id}"]
        assert ticket_database.engine.pool.checkedout() == 0

    asyncio.run(run())
    with ticket_database.transaction() as session:
        assert len(list(session.scalars(select(Ticket)))) == 2
    assert ticket_database.engine.pool.checkedout() == 0


def test_agent_backend_transaction_is_really_read_only_on_postgres(ticket_database):
    backend = backend_for(ticket_database)
    assert backend._tickets(lambda service: service.session.scalar(text("SHOW transaction_read_only"))) == "on"
    assert ticket_database.engine.pool.checkedout() == 0


def test_agent_backend_postgres_rejects_write_even_if_backend_callback_attempts_it(ticket_database):
    expected = seed(ticket_database)
    backend = backend_for(ticket_database)
    with pytest.raises(SQLAlchemyError) as rejected:
        backend._tickets(lambda service: service.create(TicketCreate(title="不得写入", description="只读事务拒绝INSERT")))
    assert rejected.value.orig.sqlstate == "25006"  # PostgreSQL read_only_sql_transaction.
    assert ticket_database.engine.pool.checkedout() == 0
    with ticket_database.transaction() as session:
        rows = list(session.scalars(select(Ticket)))
        assert len(rows) == 2 and expected.id in {row.id for row in rows}
        assert all(row.title != "不得写入" for row in rows)
    assert ticket_database.engine.pool.checkedout() == 0


def test_agent_backend_not_found_releases_actual_session(ticket_database):
    backend = backend_for(ticket_database)
    with pytest.raises(TicketNotFoundError):
        asyncio.run(backend.get_ticket(uuid4()))
    assert ticket_database.engine.pool.checkedout() == 0


def test_agent_backend_no_database_fails_without_creating_storage():
    backend = backend_for(None)
    with pytest.raises(RuntimeError, match="未配置"):
        asyncio.run(backend.get_ticket(uuid4()))
