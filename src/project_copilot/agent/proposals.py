"""The model can request only a pending proposal; credentials stay in backend memory."""
import asyncio
import threading

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ApprovalService


class ProposalBackend(ReadOnlyBackend):
    def __init__(self, settings, database, context_id, token):
        super().__init__(settings, database)
        self._context_id, self._token = context_id, token

    def authenticate(self):
        if self.database is None:
            raise RuntimeError('数据库未配置')
        with self.database.transaction() as session:
            ApprovalService(session).authenticate(self._context_id, self._token)

    def _create_proposal(self, payload, cancelled):
        with self.database.transaction() as session:
            service = ApprovalService(session)
            context = service.authenticate(self._context_id, self._token)
            result = service.preview(context, payload)
            if cancelled.is_set():
                raise RuntimeError('request cancelled')
        return result

    async def propose_ticket(self, title: str, description: str):
        cancelled = threading.Event()
        try:
            action = await asyncio.to_thread(self._create_proposal, TicketCreate(title=title, description=description), cancelled)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        data = action.model_dump(mode='json')
        return {'data': data, 'sources': [{'id': 'proposal:' + str(action.action_id), 'kind': 'proposal', 'data': data}]}
