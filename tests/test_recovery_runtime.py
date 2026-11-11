"""Runtime authority/proposal integration in owned PG; no live model or broker."""
import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select

from project_copilot.agent import service as agent_service
from project_copilot.agent.proposals import ProposalBackend
from project_copilot.db.models import AiRequestReservation, PendingAction, Ticket, UserAccount
from project_copilot.db.recovery import AiJobRecoveryState
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, track_execution
from project_copilot.infrastructure.recovery_runtime import recovery_scope
from project_copilot.schemas.auth import RegisterInput
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.ai_worker import execute
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.auth import AuthService
from project_copilot.services.job_recovery import charge_recovery_step, recover_job
from project_copilot.services.job_steps import StepRejected, fingerprint
from project_copilot.services.personal_profile import personal_runtime_settings
from project_copilot.services.tickets import TicketService
from test_job_recovery_postgres import recovery_setup
from test_rag_checkpoint_postgres import rag_setup


def test_cached_cross_owner_ticket_loses_access_after_admin_demotion(rag_setup):
    value = rag_setup
    with value.database.transaction() as session:
        auth = AuthService(session)
        other = auth.register(RegisterInput(username="otherowner", password="synthetic-owner-password-123"),
                              open_registration=True)
        ticket = TicketService(session, principal=other, enforce_ownership=True).create(
            TicketCreate(title="other owner's ticket", description="private backend evidence"))
        session.get(UserAccount, value.setup.user_id).role = "admin"
    digest = fingerprint({"ticket_id": str(ticket.id)})
    with value.scope() as runtime:
        # This was an authorized administrator read when the receipt was saved.
        with value.database.transaction() as session:
            principal, _ = AuthService(session).authenticate(value.setup.token)
            saved = TicketService(session, principal=principal, enforce_ownership=True).get(ticket.id)
        runtime.journal.begin("runtime:ticket", kind="tool", input_sha256=digest)
        runtime.journal.complete("runtime:ticket", {"data": saved.model_dump(mode="json"),
            "sources": [{"id": "ticket:" + str(ticket.id), "kind": "ticket", "data": saved.model_dump(mode="json")}]})
    with value.database.transaction() as session:
        session.get(UserAccount, value.setup.user_id).role = "user"
    value.rotate()
    with value.scope() as runtime, pytest.raises(StepRejected) as error:
        runtime.prepare()
        runtime.journal.begin("runtime:ticket", kind="tool", input_sha256=digest)
    assert error.value.reason == "revalidation"
    assert value.chat.calls == value.retriever.calls == 0
    with value.database.transaction() as session:
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 0


@pytest.mark.parametrize("status", ["pending", "cancelled", "completed"])
def test_execute_restores_original_live_proposal_before_model_creation(recovery_setup, monkeypatch, status):
    setup = recovery_setup
    with setup.database.transaction() as session:
        principal, _ = AuthService(session).authenticate(setup.token)
        credentials = ApprovalService(session, principal=principal, enforce_ownership=True).create_context()
    identity = setup.reserve("agent", context_id=credentials.context_id)
    first = setup.claim(identity)
    backend = ProposalBackend(setup.settings, setup.database, credentials.context_id, None,
                              auth_session_id=setup.login_id)
    tracker = ExecutionTracker(setup.database, identity, first.execution_lease.token)
    try:
        with track_execution(tracker):
            preview = asyncio.run(backend.propose_ticket("original stable preview", "explicit confirmation remains required"))
    finally:
        tracker.drain()
    action_id = UUID(preview["data"]["action_id"])
    with setup.database.transaction() as session:
        charge_recovery_step(session, identity, first.execution_lease.token, kind="model")
        charge_recovery_step(session, identity, first.execution_lease.token, kind="tool")
        session.get(AiRequestReservation, identity).provider_attempts = 1
        if status != "pending":
            principal, _ = AuthService(session).authenticate(setup.token)
            service = ApprovalService(session, principal=principal, enforce_ownership=True)
            context = service.authenticate_owned(credentials.context_id)
            service.decide(context, action_id, preview["data"]["payload_fingerprint"], approve=status == "completed")
    first.execution_lease.close()
    assert recover_job(setup.database, setup.settings, identity).reason == "checkpoint"
    second = setup.claim(identity)
    creations = []

    def forbidden_model(*_args, **_kwargs):
        creations.append(True)
        raise AssertionError("proposal restoration attempted to construct another model")

    monkeypatch.setattr(agent_service, "create_agent_model", forbidden_model)
    derived = personal_runtime_settings(setup.settings)
    with recovery_scope(setup.database, setup.settings, derived, second):
        result = execute(derived, setup.database, second)
    assert creations == [] and result.status == "pending_confirmation"
    assert result.sources[0].id == "proposal:" + str(action_id)
    assert result.sources[0].data["status"] == status
    assert result.model_calls == result.tool_calls == 1
    with setup.database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
        assert session.scalar(select(func.count()).select_from(Ticket)) == int(status == "completed")
        assert session.get(PendingAction, action_id).status == status
        assert session.get(AiRequestReservation, identity).provider_attempts == 1
        assert session.get(AiJobRecoveryState, identity).current_attempt_no == 2
        if status == "pending":
            # Only this separate, authenticated decision creates the ticket.
            principal, _ = AuthService(session).authenticate(setup.token)
            service = ApprovalService(session, principal=principal, enforce_ownership=True)
            context = service.authenticate_owned(credentials.context_id)
            first_confirm = service.decide(context, action_id, preview["data"]["payload_fingerprint"], approve=True)
            repeated = service.decide(context, action_id, preview["data"]["payload_fingerprint"], approve=True)
            assert first_confirm.ticket_id == repeated.ticket_id
            assert session.scalar(select(func.count()).select_from(Ticket)) == 1
