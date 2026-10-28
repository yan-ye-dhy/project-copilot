"""Real SDK spans from actual app/graph; providers are controlled and no Phoenix/network is claimed."""

from contextlib import asynccontextmanager, contextmanager
import json
from uuid import UUID

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.tools import ToolRegistry, SearchKnowledgeArguments
from project_copilot.api import agent as agent_api, rag as rag_api
from project_copilot.config import Settings
from project_copilot.db.models import PendingAction, Ticket
from project_copilot.infrastructure import tracing
from project_copilot.infrastructure.models import ChatGeneration
from project_copilot.main import create_app
from project_copilot.rag.answering import RagAnswerer
from project_copilot.schemas.documents import ChunkMetadata
from project_copilot.schemas.retrieval import RetrievedChunk
from project_copilot.services.tickets import TicketService


PRIVATE_TEXT = "PRIVATE_DOC_TOKEN_do_not_export"
PRIVATE_QUESTION = "PRIVATE_QUESTION_do_not_export"
PRIVATE_ERROR = "PRIVATE_EXCEPTION_sk_do_not_export"
CHUNK_ID = UUID("80d95a2d-fcc6-47da-a879-f052460fa1c4")


def capture_exports(monkeypatch):
    exported = []

    def export(self, spans, *args, **kwargs):
        batch = list(spans)
        exported.extend(batch)
        return tracing.TraceExportResult(True, "accepted", len(batch))

    monkeypatch.setattr(tracing.SingleAttemptOTLPExporter, "export", export)
    return exported


def attrs(spans):
    return json.dumps([dict(span.attributes or {}) for span in spans], ensure_ascii=False)


def test_actual_agent_sdk_tree_records_safe_results_and_usage(monkeypatch):
    spans = capture_exports(monkeypatch)
    source_id = "knowledge:" + str(CHUNK_ID)

    class Model:
        def __init__(self):
            self.step = 0

        async def ainvoke(self, messages, tools):
            self.step += 1
            if self.step == 1:
                return AIMessage(content="", tool_calls=[{"name": "search_knowledge", "id": "call_trace_1",
                                  "args": {"query": PRIVATE_QUESTION}}],
                                 response_metadata={"model": "deepseek-flash", "usage": {
                                     "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
            return AIMessage(content=json.dumps({"status": "answered", "answer": PRIVATE_TEXT,
                             "source_ids": [source_id]}), response_metadata={"model": "deepseek-flash",
                             "usage": {"prompt_tokens": 14, "completion_tokens": 4, "total_tokens": 18}})

    async def search(query):
        return {"data": [{"text": PRIVATE_TEXT}], "sources": [{"id": source_id, "kind": "knowledge",
                 "data": {"text": PRIVATE_TEXT}}]}

    @asynccontextmanager
    async def factory(settings, database, **kwargs):
        registry = ToolRegistry()
        registry.add("search_knowledge", SearchKnowledgeArguments, search)
        yield AgentRunner(Model(), registry)

    monkeypatch.setattr(agent_api, "create_agent_service", factory)
    with TestClient(create_app(Settings(_env_file=None, environment="test", tracing_enabled=True))) as client:
        response = client.post("/agent/ask", json={"question": PRIVATE_QUESTION})
    assert response.status_code == 200 and response.json()["status"] == "answered"
    names = [span.name for span in spans]
    assert {"agent.run", "agent.model", "agent.tools", "agent.tool.execute", "agent.tool.result"} <= set(names)
    models = [span for span in spans if span.name == "agent.model"]
    assert len(models) == 2
    assert sum(span.attributes["llm.token_count.total"] for span in models) == 30
    run = next(span for span in spans if span.name == "agent.run")
    assert run.attributes["agent.tool_executions"] == 1 and run.attributes["status"] == "answered"
    assert all(span.context.trace_id == run.context.trace_id for span in spans)
    assert all(span.attributes.get("copilot.run_id") == response.json()["run_id"] for span in spans)
    assert PRIVATE_QUESTION not in attrs(spans) and PRIVATE_TEXT not in attrs(spans)


def test_swallowed_agent_model_error_is_error_span_without_private_text(monkeypatch):
    spans = capture_exports(monkeypatch)

    class Model:
        async def ainvoke(self, messages, tools):
            raise RuntimeError(PRIVATE_ERROR)

    @asynccontextmanager
    async def factory(settings, database, **kwargs):
        yield AgentRunner(Model(), ToolRegistry())

    monkeypatch.setattr(agent_api, "create_agent_service", factory)
    with TestClient(create_app(Settings(_env_file=None, environment="test", tracing_enabled=True))) as client:
        response = client.post("/agent/ask", json={"question": PRIVATE_QUESTION})
    assert response.status_code == 200 and response.json()["failure_type"] == "model_error"
    for name in ("agent.run", "agent.model"):
        span = next(span for span in spans if span.name == name)
        assert span.status.status_code.name == "ERROR"
        assert span.attributes["failure.type"] == "model_error"
    assert PRIVATE_ERROR not in attrs(spans) and PRIVATE_QUESTION not in attrs(spans)


def test_rag_generation_and_validation_use_sdk_spans_without_evidence(monkeypatch):
    spans = capture_exports(monkeypatch)
    hit = RetrievedChunk(text=PRIVATE_TEXT, score=1.0, metadata=ChunkMetadata(
        document_id=UUID(int=1), content_hash="a" * 64, source_name="private.txt", page=None,
        chunk_id=CHUNK_ID, chunk_index=0, index_version="test-v1"))

    class Retriever:
        def search(self, query):
            return [hit]

    class Chat:
        model = "deepseek-flash"
        temperature = 0.0
        max_tokens = 128

        def generate_json(self, messages):
            return ChatGeneration(content=json.dumps({"status": "answered", "answer": PRIVATE_TEXT,
                                  "citation_numbers": [1]}), model=self.model,
                                  usage={"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20})

    @contextmanager
    def factory(settings):
        yield RagAnswerer(Retriever(), Chat())

    monkeypatch.setattr(rag_api, "create_answer_service", factory)
    with TestClient(create_app(Settings(_env_file=None, environment="test", tracing_enabled=True))) as client:
        response = client.post("/rag/ask", json={"question": PRIVATE_QUESTION})
    assert response.status_code == 200 and response.json()["status"] == "answered"
    assert {"rag.answer", "rag.generate", "rag.answer.validate"} <= {span.name for span in spans}
    generation = next(span for span in spans if span.name == "rag.generate")
    assert generation.attributes["llm.token_count.total"] == 20
    assert PRIVATE_TEXT not in attrs(spans) and PRIVATE_QUESTION not in attrs(spans)


def test_real_postgres_transaction_trace_distinguishes_flush_commit_and_rollback(ticket_database, monkeypatch, caplog):
    spans = capture_exports(monkeypatch)
    db = ticket_database
    settings = Settings(_env_file=None, environment="test", tracing_enabled=True,
                        database_url=db.engine.url.render_as_string(hide_password=False))
    with TestClient(create_app(settings)) as client:
        credentials = client.post("/agent/contexts").json()
        headers = {"x-copilot-context-id": credentials["context_id"],
                   "x-copilot-context-token": credentials["context_token"]}
        action = client.post("/actions/preview", headers=headers,
                             json={"title": PRIVATE_TEXT, "description": PRIVATE_TEXT}).json()
        first = len(spans)
        confirmation = client.post("/actions/" + action["action_id"] + "/confirm", headers=headers,
                                   json={"payload_fingerprint": action["payload_fingerprint"]})
        assert confirmation.status_code == 200 and confirmation.json()["status"] == "completed"
        batch = spans[first:]
        assert next(s for s in batch if s.name == "ticket.create").attributes["status"] == "flushed"
        assert next(s for s in batch if s.name == "approval.confirm").attributes["transaction.state"] == "awaiting_commit"
        assert next(s for s in batch if s.name == "db.transaction").attributes["transaction.state"] == "committed"
        failed = client.post("/actions/preview", headers=headers,
                             json={"title": PRIVATE_TEXT, "description": PRIVATE_TEXT}).json()
        original = TicketService.create

        def failure(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise SQLAlchemyError(PRIVATE_ERROR)

        monkeypatch.setattr(TicketService, "create", failure)
        first = len(spans)
        response = client.post("/actions/" + failed["action_id"] + "/confirm", headers=headers,
                               json={"payload_fingerprint": failed["payload_fingerprint"]})
        assert response.status_code == 503
        transaction = next(s for s in spans[first:] if s.name == "db.transaction")
        assert transaction.attributes["transaction.state"] == "rolled_back"
        assert transaction.status.status_code.name == "ERROR"
        with db.transaction() as session:
            assert session.get(PendingAction, UUID(failed["action_id"])).status == "pending"
            assert session.scalar(select(func.count()).select_from(Ticket)) == 1
    exported = attrs(spans)
    assert credentials["context_token"] not in exported and PRIVATE_TEXT not in exported and PRIVATE_ERROR not in exported
    assert "Failed to detach context" not in caplog.text
