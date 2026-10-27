"""Real isolated PostgreSQL: transitions, transaction rollback and racing clients."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.db.models import PendingAction, Ticket
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ApprovalService, ActionNotFound, ContextRejected, FingerprintMismatch, fingerprint
from project_copilot.services.tickets import TicketService


def make_action(db):
    with db.transaction() as session:
        service = ApprovalService(session)
        creds = service.create_context()
        action = service.preview(service.authenticate(creds.context_id, creds.context_token),
                                 TicketCreate(title='测试提案', description='事务与并发验证'))
    return creds, action


def decide(db, creds, action, approve=True, digest=None):
    with db.transaction() as session:
        service = ApprovalService(session)
        return service.decide(service.authenticate(creds.context_id, creds.context_token), action.action_id,
                              digest or action.payload_fingerprint, approve=approve)


def test_confirm_and_reject_are_terminal_and_idempotent(ticket_database):
    db = ticket_database
    creds, action = make_action(db)
    first, second = decide(db, creds, action), decide(db, creds, action)
    assert first.status == second.status == 'completed' and first.ticket_id == second.ticket_id
    assert decide(db, creds, action, approve=False).ticket_id == first.ticket_id
    with db.transaction() as session:
        ticket = session.get(Ticket, first.ticket_id)
        assert ticket.creation_key == action.action_id and ticket.title == action.payload.title
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1
    other, proposal = make_action(db)
    assert decide(db, other, proposal, approve=False).status == 'cancelled'
    assert decide(db, other, proposal).status == 'cancelled'
    assert db.engine.pool.checkedout() == 0


def test_context_fingerprint_and_database_immutability(ticket_database):
    db = ticket_database
    creds, action = make_action(db)
    other, _ = make_action(db)
    with pytest.raises(ActionNotFound):
        decide(db, other, action)
    with pytest.raises(ContextRejected):
        with db.transaction() as session:
            ApprovalService(session).authenticate(creds.context_id, 'x' * 43)
    with pytest.raises(FingerprintMismatch):
        decide(db, creds, action, digest='0' * 64)
    with pytest.raises(SQLAlchemyError):
        with db.transaction() as session:
            session.execute(text('UPDATE pending_actions SET title=:title WHERE id=:id'), {'title': '篡改', 'id': action.action_id})
    with db.transaction() as session:
        assert session.get(PendingAction, action.action_id).status == 'pending'
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_expired_proposal_never_creates_ticket(ticket_database):
    db = ticket_database
    creds, _ = make_action(db)
    with db.transaction() as session:
        svc = ApprovalService(session)
        now = svc.now()
        payload = TicketCreate(title='过期', description='不能创建')
        action = PendingAction(context_id=creds.context_id, title=payload.title, description=payload.description,
                               payload_fingerprint=fingerprint(payload), created_at=now-timedelta(seconds=20),
                               expires_at=now-timedelta(seconds=10))
        session.add(action); session.flush(); action = svc.read(action)
    assert decide(db, creds, action).status == 'expired'
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0


def test_failure_after_ticket_flush_rolls_back_both(ticket_database, monkeypatch):
    db = ticket_database
    creds, action = make_action(db)
    create = TicketService.create
    def fail_after_flush(self, *args, **kwargs):
        create(self, *args, **kwargs)
        raise RuntimeError('controlled failure')
    monkeypatch.setattr(TicketService, 'create', fail_after_flush)
    with pytest.raises(RuntimeError):
        decide(db, creds, action)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        assert session.get(PendingAction, action.action_id).status == 'pending'
    monkeypatch.setattr(TicketService, 'create', create)
    assert decide(db, creds, action).status == 'completed'
    assert db.engine.pool.checkedout() == 0


def test_parallel_confirm_creates_one_ticket(ticket_database):
    db = ticket_database
    creds, action = make_action(db)
    gate = Barrier(8)
    def worker(_):
        gate.wait(timeout=10)
        return decide(db, creds, action)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert all(result.status == 'completed' for result in results)
    assert len({result.ticket_id for result in results}) == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Ticket).where(Ticket.creation_key == action.action_id)) == 1
    assert db.engine.pool.checkedout() == 0


@pytest.mark.parametrize('round_no', range(3))
def test_confirm_reject_race_has_one_consistent_outcome(ticket_database, round_no):
    db = ticket_database
    creds, action = make_action(db)
    gate = Barrier(2)
    def worker(approve):
        gate.wait(timeout=10)
        return decide(db, creds, action, approve=approve)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [True, False]))
    assert results[0].status == results[1].status
    with db.transaction() as session:
        count = session.scalar(select(func.count()).select_from(Ticket).where(Ticket.creation_key == action.action_id))
    assert count == (1 if results[0].status == 'completed' else 0)
    assert db.engine.pool.checkedout() == 0


def test_upgrade_preserves_existing_ticket(postgres_database):
    from alembic import command
    from project_copilot.db.migrations import check_model_drift, migration_config, upgrade
    db = postgres_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes['connection'] = connection
        command.upgrade(config, '0001_tickets')
        ticket_id = connection.execute(text("INSERT INTO tickets (title, description) VALUES ('旧工单', '迁移保留') RETURNING id")).scalar_one()
    upgrade(db)
    check_model_drift(db)
    with db.transaction() as session:
        ticket = session.get(Ticket, ticket_id)
        assert ticket.title == '旧工单' and ticket.creation_key is None


def test_commit_failure_does_not_publish_success(ticket_database):
    db = ticket_database
    creds, action = make_action(db)
    with db.engine.begin() as connection:
        connection.execute(text('CREATE TABLE deferred_guard (value integer UNIQUE DEFERRABLE INITIALLY DEFERRED)'))
        connection.execute(text('INSERT INTO deferred_guard (value) VALUES (1)'))
    with pytest.raises(SQLAlchemyError):
        with db.transaction() as session:
            svc = ApprovalService(session)
            result = svc.decide(svc.authenticate(creds.context_id, creds.context_token), action.action_id,
                                action.payload_fingerprint, approve=True)
            assert result.status == 'completed'
            session.execute(text('INSERT INTO deferred_guard (value) VALUES (1)'))
    with db.transaction() as session:
        assert session.get(PendingAction, action.action_id).status == 'pending'
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
    assert decide(db, creds, action).status == 'completed'


def test_expired_context_cannot_authorize_proposal_or_confirmation(ticket_database):
    from hashlib import sha256
    from uuid import uuid4
    from project_copilot.db.models import AgentContext
    db = ticket_database
    token = 'q' * 43
    context_id = uuid4()
    with db.transaction() as session:
        now = ApprovalService(session).now()
        session.add(AgentContext(id=context_id, token_hash=sha256(token.encode()).hexdigest(),
                                 created_at=now-timedelta(hours=2), expires_at=now-timedelta(hours=1)))
    with pytest.raises(ContextRejected):
        with db.transaction() as session:
            ApprovalService(session).authenticate(context_id, token)
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 0
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 0
