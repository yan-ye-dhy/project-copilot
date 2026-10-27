"""实际编译图＋真实PostgreSQL/Qdrant；决策和二维Embedding是显式离线测试替身。

这些测试证明工具/状态/存储集成，不声称验证了真实模型的选择或回答质量。
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
import pytest
from qdrant_client import QdrantClient
from sqlalchemy import select

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.config import Settings
from project_copilot.db.models import Ticket
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.tickets import TicketService


pytestmark = pytest.mark.skipif(
    os.environ.get("PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION") != "1"
    or os.environ.get("PROJECT_COPILOT_RUN_QDRANT_INTEGRATION") != "1",
    reason="实际LangGraph/PG/Qdrant集成需要同时显式启用两个真实存储门禁",
)


class OfflineEmbeddings:
    """只测试真实HTTP检索契约，不对语义Embedding效果作结论。"""

    model = "agent-offline-storage-integration"
    expected_dimensions = 2

    def __init__(self):
        self.query_count = 0

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _query):
        self.query_count += 1
        return [1.0, 0.0]


@pytest.fixture
def real_knowledge(tmp_path: Path):
    source = tmp_path / "agent_knowledge.md"
    source.write_text("# 上传排查\n\n空文件上传返回HTTP422；上传大小上限为5 MiB，过大返回HTTP413。\n", encoding="utf-8")
    chunks = split_document(load_document(source))
    assert len(chunks) == 1
    collection = "pc_test_d24_" + uuid4().hex
    client = QdrantClient(url=os.environ.get("PROJECT_COPILOT_QDRANT_URL", "http://127.0.0.1:6433"),
                          timeout=10, prefer_grpc=False, check_compatibility=False, trust_env=False)
    embeddings = OfflineEmbeddings()
    spec = DenseIndexSpec(collection_name=collection, embedding_model=embeddings.model,
                          dimensions=2, splitter_version=SplitConfig().index_version)
    store = DenseQdrantStore(client, collection_name=collection, dimensions=2)
    manifest = ManifestStore(tmp_path / "manifests", spec)
    try:
        assert client.info().version == "1.19.2"
        completed = IngestionService(embeddings, store, manifest).ingest_file(source)
        assert client.count(collection, exact=True).count == 1
        assert manifest.load().documents == (completed,)
        yield DenseRetriever(embeddings, store, manifest, top_k=1), chunks[0], embeddings
    finally:
        # Only this fixture's newly generated collection is eligible for deletion.
        try:
            if client.collection_exists(collection):
                client.delete_collection(collection)
        finally:
            client.close()


class ScriptedDecisions:
    """脚本模型仅驱动真实编译图；每次调用记录实际消息供配对检查。"""

    def __init__(self, calls, source_ids):
        self.calls = calls
        self.source_ids = source_ids
        self.messages = []

    async def ainvoke(self, messages, tools):
        assert {tool["function"]["name"] for tool in tools} == {
            "get_ticket", "search_tickets", "search_knowledge",
        }
        self.messages.append(list(messages))
        index = len(self.messages) - 1
        metadata = {"model": "scripted-test-decisions", "usage": {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        }}
        if index < len(self.calls):
            name, args = self.calls[index]
            return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                                       "id": f"call-{index}", "type": "tool_call"}],
                             response_metadata=metadata)
        kinds = {source_id.split(":", 1)[0] for source_id in self.source_ids}
        answer = ({"knowledge": "空文件上传返回HTTP422，上传上限为5 MiB。",
                   "ticket": "上传HTTP422工单的状态为open。"}.get(next(iter(kinds)))
                  if len(kinds) == 1 else "工单状态为open；知识原文说明空文件HTTP422及5 MiB限制。")
        return AIMessage(content=json.dumps({"status": "answered", "answer": answer,
                                             "source_ids": self.source_ids}, ensure_ascii=False),
                         response_metadata=metadata)


@pytest.mark.parametrize("path", ["knowledge", "tickets", "combined"])
def test_compiled_agent_routes_use_actual_services_without_ticket_writes(ticket_database, real_knowledge, path):
    retriever, chunk, embeddings = real_knowledge
    with ticket_database.transaction() as session:
        ticket = TicketService(session).create(TicketCreate(title="上传HTTP422", description="上传空文件需要排查"))
    backend = ReadOnlyBackend(Settings(_env_file=None, environment="test", database_url=None),
                              ticket_database, knowledge_search=retriever.search)
    knowledge_id = f"knowledge:{chunk.metadata.chunk_id}"
    ticket_id = f"ticket:{ticket.id}"
    calls = {
        "knowledge": [("search_knowledge", {"query": "空文件上传限制"})],
        "tickets": [("search_tickets", {"q": "上传", "status": "open", "limit": 5}),
                    ("get_ticket", {"ticket_id": str(ticket.id)})],
        "combined": [("search_tickets", {"q": "上传", "limit": 5}),
                     ("get_ticket", {"ticket_id": str(ticket.id)}),
                     ("search_knowledge", {"query": "空文件上传限制"})],
    }[path]
    expected_ids = ([knowledge_id] if path == "knowledge" else [ticket_id] if path == "tickets"
                    else [ticket_id, knowledge_id])
    model = ScriptedDecisions(calls, expected_ids)
    runner = AgentRunner(model, build_readonly_registry(backend))
    assert isinstance(runner.graph, CompiledStateGraph)
    question = AgentQuestion(question="继续查看这个问题的记录和上传限制。", history=[
        {"role": "user", "content": "我正在排查上传HTTP422问题。"},
        {"role": "assistant", "content": "可以查询工单及上传知识。"},
    ])
    result = asyncio.run(runner.run(question, run_id="real-storage-" + path))
    assert result.status == "answered" and result.failure_type is None
    assert result.run_id == "real-storage-" + path
    assert result.model_calls == len(calls) + 1 and result.tool_calls == len(calls)
    assert result.trace_exported is False
    assert [event.name for event in result.events] == [name for name, _args in calls]
    assert all(event.status == "ok" and event.elapsed_ms >= 0 for event in result.events)
    assert [event.tool_call_id for event in result.events] == [f"call-{i}" for i in range(len(calls))]
    assert {source.id for source in result.sources} == set(expected_ids)
    actual_sources = {source.id: source.data for source in result.sources}
    if ticket_id in actual_sources:
        assert actual_sources[ticket_id] == ticket.model_dump(mode="json")
    if knowledge_id in actual_sources:
        evidence = actual_sources[knowledge_id]
        assert evidence["text"] == chunk.text
        assert evidence["metadata"] == chunk.metadata.model_dump(mode="json")
        assert evidence["score"] == pytest.approx(1.0)
    assert embeddings.query_count == (0 if path == "tickets" else 1)
    assert any(isinstance(message, HumanMessage) and "正在排查" in message.content for message in model.messages[0])
    for step, snapshot in enumerate(model.messages[1:], start=1):
        tool_messages = [message for message in snapshot if isinstance(message, ToolMessage)]
        assert [message.tool_call_id for message in tool_messages] == [f"call-{i}" for i in range(step)]
        assert all(json.loads(message.content)["status"] == "ok" for message in tool_messages)
    with ticket_database.transaction() as session:
        assert list(session.scalars(select(Ticket.id))) == [ticket.id]
    assert ticket_database.engine.pool.checkedout() == 0
