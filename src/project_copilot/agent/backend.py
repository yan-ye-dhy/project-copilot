"""受控只读适配：复用工单Service和Dense检索，SQL事务额外声明只读。"""

from project_copilot.infrastructure.execution_tracking import tracked_to_thread
from uuid import UUID

from sqlalchemy import text

from project_copilot.config import Settings
from project_copilot.db.session import Database
from project_copilot.schemas.tickets import TicketFilters
from project_copilot.services.tickets import TicketService
from project_copilot.services.auth import AuthService


class ReadOnlyBackend:
    def __init__(self, settings: Settings, database: Database | None, *, knowledge_search=None, auth_token=None, auth_session_id=None):
        self.settings = settings
        self.database = database
        # Explicit dependency injection for real-service integration; production uses existing retriever.
        self._knowledge_search = knowledge_search
        self._auth_token = auth_token
        self._auth_session_id = auth_session_id

    def _principal(self, session):
        if self.settings.public_demo or self.settings.environment == 'production':
            auth = AuthService(session)
            principal, _ = (auth.authenticate_session_id(self._auth_session_id) if self._auth_session_id is not None
                            else auth.authenticate(self._auth_token))
            return principal
        return None

    def _tickets(self, operation):
        if self.database is None:
            raise RuntimeError("工单数据库未配置")
        with self.database.transaction() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SELECT set_config('statement_timeout', '5000', true)"))
            principal = self._principal(session)
            return operation(TicketService(session, principal=principal,
                                           enforce_ownership=principal is not None))

    @staticmethod
    def _ticket_result(tickets):
        sources = [{"id": "ticket:" + str(ticket.id), "kind": "ticket",
                    "data": ticket.model_dump(mode="json")} for ticket in tickets]
        return {"data": [source["data"] for source in sources], "sources": sources}

    async def get_ticket(self, ticket_id: UUID):
        ticket = await tracked_to_thread(self._tickets, lambda service: service.get(ticket_id))
        result = self._ticket_result([ticket])
        result["data"] = result["data"][0]
        return result

    async def search_tickets(self, status=None, q=None, limit=5):
        filters = TicketFilters(status=status, q=q, limit=limit, offset=0)
        result = await tracked_to_thread(self._tickets, lambda service: service.search(filters))
        return self._ticket_result(result.items)

    def _knowledge(self, query):
        if self.settings.public_demo or self.settings.environment == 'production':
            if self.database is None:
                raise RuntimeError('数据库未配置')
            with self.database.transaction() as session:
                self._principal(session)
        if self._knowledge_search is not None:
            return self._knowledge_search(query)
        from project_copilot.rag.final_retrieval import get_final_retriever
        return get_final_retriever(self.settings).search(query)

    async def search_knowledge(self, query: str):
        hits = await tracked_to_thread(self._knowledge, query)
        sources = [{"id": "knowledge:" + str(hit.metadata.chunk_id), "kind": "knowledge",
                    "data": hit.model_dump(mode="json")} for hit in hits]
        return {"data": [source["data"] for source in sources], "sources": sources}
