"""Explicit admin retention prunes only settled display receipts, never proofs."""
from uuid import uuid4
import pytest
from sqlalchemy import select
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.recovery import AiJobStep
from project_copilot.services.job_steps import fingerprint
from project_copilot.services.public_maintenance import cleanup, MaintenanceRejected
from test_public_agent_approvals import seed
from test_public_operations_postgres import job


def receipt(database, identity):
    with database.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        data = {"text": "ordinary synthetic receipt"}
        session.add(AiJobStep(job_id=identity, step_key="synthetic.receipt", kind="node", status="completed",
            input_sha256="a" * 64, config_sha256="b" * 64, attempt_no=1, execution_token=uuid4(),
            result=data, result_sha256=fingerprint(data), completed_at=row.created_at))


def test_settled_terminal_receipts_pruned_but_unknown_and_held_evidence_remains(ticket_database):
    database = ticket_database
    users, _ = seed(database)
    done = job(database, users["alice"])
    unknown = job(database, users["alice"], state="unknown")
    held = job(database, users["alice"], state="failed")
    with database.transaction() as session:
        row = session.get(AiRequestReservation, done)
        row.slot_released_at = row.finished_at
    for identity in (done, unknown, held):
        receipt(database, identity)
    with database.transaction() as session:
        assert cleanup(session, users["admin"].id)["step_content"] == 1
    with database.transaction() as session:
        assert session.get(AiJobStep, (done, "synthetic.receipt")) is None
        assert session.get(AiJobStep, (unknown, "synthetic.receipt")) is not None
        assert session.get(AiJobStep, (held, "synthetic.receipt")) is not None
        assert session.get(AiRequestReservation, done).provider_attempts == 1
        assert session.get(AiRequestReservation, unknown).state == "unknown"
        assert cleanup(session, users["admin"].id)["step_content"] == 0


def test_non_admin_cannot_prune_receipts(ticket_database):
    users, _ = seed(ticket_database)
    identity = job(ticket_database, users["alice"])
    receipt(ticket_database, identity)
    with pytest.raises(MaintenanceRejected):
        with ticket_database.transaction() as session:
            cleanup(session, users["alice"].id)
    with ticket_database.transaction() as session:
        assert session.get(AiJobStep, (identity, "synthetic.receipt")) is not None
