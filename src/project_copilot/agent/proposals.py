"""The model can request only a pending proposal; credentials stay in backend memory."""
import asyncio
import threading

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.infrastructure.execution_tracking import current_execution, tracked_to_thread
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ApprovalService


class ProposalBackend(ReadOnlyBackend):
    def __init__(self, settings, database, context_id, token, *, auth_token=None, auth_session_id=None):
        super().__init__(settings, database, auth_token=auth_token, auth_session_id=auth_session_id)
        self._context_id, self._token = context_id, token

    def _context(self, service):
        if self._auth_session_id is not None:
            return service.authenticate_owned(self._context_id)
        return service.authenticate(self._context_id, self._token)

    def authenticate(self):
        if self.database is None:
            raise RuntimeError('数据库未配置')
        with self.database.transaction() as session:
            principal = self._principal(session)
            self._context(ApprovalService(session, principal=principal, enforce_ownership=principal is not None))

    def _create_proposal(self, payload, cancelled):
        with self.database.transaction() as session:
            if self.settings.public_demo or self.settings.environment == 'production':
                from project_copilot.services.auth import AuthService
                auth = AuthService(session)
                principal, _ = (auth.authenticate_session_id(self._auth_session_id, for_update=True)
                                if self._auth_session_id is not None else auth.authenticate(self._auth_token, for_update=True))
            else:
                principal = None
            service = ApprovalService(session, principal=principal, enforce_ownership=principal is not None)
            context = self._context(service)
            tracker = current_execution()
            execution = None
            if tracker is not None:
                from project_copilot.services.execution_slots import ExecutionFenced, guard_execution
                execution = guard_execution(session, tracker.job_id, tracker.token)
                if execution.action_id is not None:
                    # A timed-out tool may have committed before cancellation.
                    # A later tool decision reuses that preview rather than creating a duplicate.
                    from project_copilot.db.models import PendingAction
                    existing = session.get(PendingAction, execution.action_id)
                    if (existing is None or existing.context_id != context.id or
                            existing.owner_user_id != principal.id):
                        raise ExecutionFenced("proposal binding mismatch")
                    return service.read(existing)
            result = service.preview(context, payload)
            if execution is not None:
                execution.action_id = result.action_id
            if cancelled.is_set():
                raise RuntimeError('request cancelled')
        return result

    async def propose_ticket(self, title: str, description: str):
        cancelled = threading.Event()
        try:
            action = await tracked_to_thread(self._create_proposal, TicketCreate(title=title, description=description), cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        data = action.model_dump(mode='json')
        return {'data': data, 'sources': [{'id': 'proposal:' + str(action.action_id), 'kind': 'proposal', 'data': data}]}
