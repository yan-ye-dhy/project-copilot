"""Real PostgreSQL ownership constraints, using only the isolated test fixture."""
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from project_copilot.db.migrations import check_model_drift, migration_config, upgrade


def test_legacy_rows_remain_unowned_after_upgrade(postgres_database):
    db = postgres_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0003_accounts")
        ticket_id = connection.execute(text("INSERT INTO tickets(title, description) VALUES ('旧工单', '保留') RETURNING id")).scalar_one()
        context_id = connection.execute(text("INSERT INTO agent_contexts(token_hash, expires_at) VALUES (:hash, now() + interval '1 hour') RETURNING id"), {"hash": "a" * 64}).scalar_one()
        action_id = connection.execute(text("INSERT INTO pending_actions(context_id, title, description, payload_fingerprint, expires_at) VALUES (:ctx, '旧提案', '保留', :hash, now() + interval '1 hour') RETURNING id"), {"ctx": context_id, "hash": "b" * 64}).scalar_one()
    upgrade(db)
    upgrade(db)
    check_model_drift(db)
    with db.engine.connect() as connection:
        for table, row_id in (("tickets", ticket_id), ("agent_contexts", context_id), ("pending_actions", action_id)):
            row = connection.execute(text(f"SELECT owner_user_id FROM {table} WHERE id = :id"), {"id": row_id}).one()
            assert row.owner_user_id is None
    # Pre-existing proposals remain confirmable under the legacy/local contract.
    with db.engine.begin() as connection:
        created_id = connection.execute(text("INSERT INTO tickets(title, description, creation_key) VALUES ('旧提案工单', '保留', :action) RETURNING id"), {"action": action_id}).scalar_one()
        connection.execute(text("UPDATE pending_actions SET status='completed', ticket_id=:ticket WHERE id=:action"), {"ticket": created_id, "action": action_id})
    assert db.engine.pool.checkedout() == 0


def test_database_enforces_ownership_and_binding(ticket_database):
    db = ticket_database
    owner_a, owner_b, context_id, action_id = (uuid4() for _ in range(4))
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO user_accounts(id, username, password_hash) VALUES (:a, 'alice', '$argon2id$test'), (:b, 'bobby', '$argon2id$test')"), {"a": owner_a, "b": owner_b})
        conn.execute(text("INSERT INTO agent_contexts(id, owner_user_id, token_hash, expires_at) VALUES (:id, :owner, :hash, now() + interval '1 hour')"), {"id": context_id, "owner": owner_a, "hash": "a" * 64})

    def insert_action(owner):
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO pending_actions(id, context_id, owner_user_id, title, description, payload_fingerprint, expires_at) VALUES (:id, :ctx, :owner, '提案', '描述', :hash, now() + interval '1 hour')"), {"id": action_id, "ctx": context_id, "owner": owner, "hash": "b" * 64})

    with pytest.raises(DBAPIError, match="context owner mismatch"):
        insert_action(owner_b)
    with pytest.raises(DBAPIError, match="context owner mismatch"):
        insert_action(None)
    insert_action(owner_a)
    with pytest.raises(DBAPIError):
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO tickets(owner_user_id, title, description) VALUES (:owner, '工单', '描述')"), {"owner": uuid4()})

    for table, row_id in (("agent_contexts", context_id), ("pending_actions", action_id)):
        with pytest.raises(DBAPIError, match="immutable"):
            with db.engine.begin() as conn:
                conn.execute(text(f"UPDATE {table} SET owner_user_id = :owner WHERE id = :id"), {"owner": owner_b, "id": row_id})

    ticket_id = uuid4()
    with pytest.raises(DBAPIError, match="ticket mismatch"):
        with db.engine.begin() as conn:
            conn.execute(text("INSERT INTO tickets(id, creation_key, owner_user_id, title, description) VALUES (:id, :action, :owner, '工单', '描述')"), {"id": ticket_id, "action": action_id, "owner": owner_b})
            conn.execute(text("UPDATE pending_actions SET status = 'completed', ticket_id = :ticket WHERE id = :id"), {"ticket": ticket_id, "id": action_id})
    with db.engine.begin() as conn:
        conn.execute(text("INSERT INTO tickets(id, creation_key, owner_user_id, title, description) VALUES (:id, :action, :owner, '工单', '描述')"), {"id": ticket_id, "action": action_id, "owner": owner_a})
        conn.execute(text("UPDATE pending_actions SET status = 'completed', ticket_id = :ticket WHERE id = :id"), {"ticket": ticket_id, "id": action_id})
    for column, value in (("owner_user_id", owner_b), ("creation_key", uuid4())):
        with pytest.raises(DBAPIError, match="immutable ticket"):
            with db.engine.begin() as conn:
                conn.execute(text(f"UPDATE tickets SET {column} = :value WHERE id = :id"), {"value": value, "id": ticket_id})
    with db.engine.connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM tickets WHERE id = :id AND owner_user_id = :owner"), {"id": ticket_id, "owner": owner_a}) == 1
        assert conn.scalar(text("SELECT count(*) FROM pending_actions WHERE id = :id AND status = 'completed' AND ticket_id = :ticket"), {"id": action_id, "ticket": ticket_id}) == 1
    assert db.engine.pool.checkedout() == 0
