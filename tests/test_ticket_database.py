"""阶段B真实PG：空库迁移、ORM一致性、约束及连接释放，无SQLite/Mock替代。"""

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.db.migrations import check_model_drift, upgrade
from project_copilot.db.models import Ticket


def test_empty_postgres_migrates_to_model_and_upgrade_is_repeatable(postgres_database):
    db = postgres_database
    assert inspect(db.engine).get_table_names() == []
    with db.transaction() as session:
        assert session.scalar(text("SELECT 1")) == 1
        assert session.scalar(text("SELECT current_database()")).startswith("pc_test_d20_")
    upgrade(db)
    upgrade(db)
    assert set(inspect(db.engine).get_table_names()) == {"alembic_version", "tickets"}
    check_model_drift(db)
    columns = {c["name"]: c for c in inspect(db.engine).get_columns("tickets")}
    assert set(columns) == {"id", "title", "description", "status", "created_at", "updated_at"}
    assert all(not col["nullable"] for col in columns.values())
    assert columns["title"]["type"].length == 200
    assert columns["created_at"]["type"].timezone is True
    assert "gen_random_uuid" in columns["id"]["default"]
    with db.transaction() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == "0001_tickets"
    assert db.engine.pool.checkedout() == 0


@pytest.mark.parametrize("column,value", [
    ("title", None), ("title", ""), ("title", "  "), ("title", "\t\n"), ("title", "长" * 201),
    ("description", None), ("description", ""), ("description", " "), ("description", "\t\n"), ("description", "长" * 10001),
    ("status", "invalid"),
])
def test_postgres_constraints_reject_direct_sql_and_rollback(ticket_database, column, value):
    db = ticket_database
    values = {"title": "有效标题", "description": "有效描述", "status": "open", column: value}
    # The identifier is a fixed parameterized-test column; input values are always bound.
    with pytest.raises(SQLAlchemyError):
        with db.transaction() as session:
            session.execute(text("INSERT INTO tickets (title, description, status) VALUES (:title, :description, :status)"), values)
    with db.transaction() as session:
        assert list(session.scalars(select(Ticket))) == []
    assert db.engine.pool.checkedout() == 0


def test_database_generates_real_id_open_status_and_timezone_timestamps(ticket_database):
    db = ticket_database
    with db.transaction() as session:
        ticket = Ticket(title="真实中文工单", description="独立数据库写入")
        assert ticket.id is None
        session.add(ticket)
        session.flush()
        ticket_id = ticket.id
        assert ticket_id is not None and ticket.status == "open"
        assert ticket.created_at.tzinfo is not None and ticket.updated_at == ticket.created_at
    with db.transaction() as session:
        stored = session.get(Ticket, ticket_id)
        assert stored is not None and stored.title == "真实中文工单"
    assert db.engine.pool.checkedout() == 0
