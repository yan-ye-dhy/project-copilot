"""受控只读适配：复用工单Service和Dense检索，SQL事务额外声明只读。"""

import asyncio
from uuid import UUID

from sqlalchemy import text

from project_copilot.config import Settings
from project_copilot.db.session import Database
from project_copilot.infrastructure.models import create_embedding_client
from project_copilot.infrastructure.qdrant import create_qdrant_store
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.schemas.tickets import TicketFilters
from project_copilot.services.tickets import TicketService


class ReadOnlyBackend:
    def __init__(self, settings: Settings, database: Database | None, *, knowledge_search=None):
        self.settings = settings
        self.database = database
        # Explicit dependency injection for real-service integration; production uses existing retriever.
        self._knowledge_search = knowledge_search

    def _tickets(self, operation):
        if self.database is None:
            raise RuntimeError("工单数据库未配置")
        with self.database.transaction() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SELECT set_config('statement_timeout', '5000', true)"))
            return operation(TicketService(session))

    @staticmethod
    def _ticket_result(tickets):
        sources = [{"id": "ticket:" + str(ticket.id), "kind": "ticket",
                    "data": ticket.model_dump(mode="json")} for ticket in tickets]
        return {"data": [source["data"] for source in sources], "sources": sources}

    async def get_ticket(self, ticket_id: UUID):
        ticket = await asyncio.to_thread(self._tickets, lambda service: service.get(ticket_id))
        result = self._ticket_result([ticket])
        result["data"] = result["data"][0]
        return result

    async def search_tickets(self, status=None, q=None, limit=5):
        filters = TicketFilters(status=status, q=q, limit=limit, offset=0)
        result = await asyncio.to_thread(self._tickets, lambda service: service.search(filters))
        return self._ticket_result(result.items)

    def _knowledge(self, query):
        if self._knowledge_search is not None:
            return self._knowledge_search(query)
        # Bounds apply only to agent clients; the existing RAG defaults stay unchanged.
        settings = self.settings.model_copy(update={
            "zhipu_timeout_seconds": min(self.settings.zhipu_timeout_seconds, 8.0),
            "qdrant_timeout_seconds": min(self.settings.qdrant_timeout_seconds, 5.0),
        })
        spec = DenseIndexSpec(collection_name=settings.qdrant_collection_name,
                              embedding_model=settings.embedding_model,
                              dimensions=settings.embedding_expected_dimensions,
                              splitter_version=SplitConfig().index_version)
        manifest = ManifestStore(settings.manifest_dir, spec)
        with create_embedding_client(settings) as embeddings, create_qdrant_store(settings) as store:
            return DenseRetriever(embeddings, store, manifest, top_k=min(settings.rag_top_k, 5)).search(query)

    async def search_knowledge(self, query: str):
        hits = await asyncio.to_thread(self._knowledge, query)
        sources = [{"id": "knowledge:" + str(hit.metadata.chunk_id), "kind": "knowledge",
                    "data": hit.model_dump(mode="json")} for hit in hits]
        return {"data": [source["data"] for source in sources], "sources": sources}
