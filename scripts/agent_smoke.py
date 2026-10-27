"""显式本地Agent验收：独立PG库/新Qdrant集合；--live才调用付费模型。"""

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from langchain_core.messages import AIMessage
from pydantic import SecretStr
from qdrant_client import QdrantClient
from sqlalchemy import func, select

from project_copilot.agent.backend import ReadOnlyBackend
from project_copilot.agent.graph import AgentRunner, GraphLimits
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.config import Settings, load_settings
from project_copilot.db.migrations import check_model_drift, upgrade
from project_copilot.db.models import Ticket
from project_copilot.db.session import Database
from project_copilot.infrastructure.agent_model import create_agent_model
from project_copilot.infrastructure.models import create_embedding_client
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.schemas.tickets import TicketCreate, TicketRead
from project_copilot.services.tickets import TicketService
from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database


class OfflineVectors:
    model = "d24-local-contract"
    expected_dimensions = 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0]


class CountedVectors:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.model, self.expected_dimensions = wrapped.model, wrapped.expected_dimensions
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return self.wrapped.embed_documents(texts)

    def embed_query(self, text):
        self.calls += 1
        return self.wrapped.embed_query(text)


class ScriptedModel:
    """Controlled decisions prove graph/service integration, never model capability."""
    def __init__(self, plan, source_ids):
        self.plan, self.source_ids = list(plan), source_ids

    async def ainvoke(self, messages, tools):
        if self.plan:
            name, args = self.plan.pop(0)
            return AIMessage(content="", tool_calls=[{"id": "call_" + uuid4().hex,
                             "name": name, "args": args, "type": "tool_call"}])
        return AIMessage(content=json.dumps({"status": "answered", "answer": "已核对真实工单及知识来源。",
                                             "source_ids": self.source_ids}, ensure_ascii=False))


class RecordingModel:
    def __init__(self, inner):
        self.inner, self.decisions = inner, []

    async def ainvoke(self, messages, tools):
        record = {"message_roles": [m.type for m in messages], "tools": [t["function"]["name"] for t in tools]}
        self.decisions.append(record)
        result = await self.inner.ainvoke(messages, tools)
        record.update({"content": result.content, "tool_calls": result.tool_calls,
                       "invalid_tool_calls": result.invalid_tool_calls, "metadata": result.response_metadata})
        return result


class ReplayedModel:
    """Exact archived AI messages, replayed against a fresh real-service snapshot."""
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    async def ainvoke(self, messages, tools):
        item = next(self.decisions)
        return AIMessage(content=item["content"], tool_calls=item["tool_calls"],
                         invalid_tool_calls=item["invalid_tool_calls"], response_metadata=item["metadata"])


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


async def execute(args, output, receipt):
    settings = load_settings() if args.live else Settings(_env_file=None, environment="test")
    settings = settings.model_copy(update={"deepseek_max_tokens": 512})
    collection = "pc_test_d24_" + uuid4().hex
    archive = json.loads(args.replay_model.read_text()) if args.replay_model else None
    if archive is not None:
        if archive.get("mode") != "live_model_real_services" or archive.get("status") != "ok":
            raise ValueError("只能回放已成功的真实模型原始记录")
    with owned_test_database(os.environ.get("PROJECT_COPILOT_TEST_DATABASE_URL", TEST_DATABASE_URL)) as url:
        database = Database(url)
        client = QdrantClient(url=str(settings.qdrant_url), timeout=10, prefer_grpc=False,
                              trust_env=False, check_compatibility=False)
        embeddings = None
        vectors = None
        try:
            embeddings = create_embedding_client(settings) if args.live else OfflineVectors()
            vectors = CountedVectors(embeddings)
            upgrade(database)
            check_model_drift(database)
            with database.transaction() as session:
                if archive is not None:
                    ticket = TicketRead.model_validate(archive["ticket"])
                    session.add(Ticket(**ticket.model_dump()))
                    session.flush()
                else:
                    ticket = TicketService(session).create(TicketCreate(title="D24上传故障",
                        description="上传6 MiB文件收到HTTP413；请核对大小限制。"))
            document = output / "knowledge.txt"
            document.write_text("Project Copilot 上传文件大小上限为5 MiB。超过上限返回HTTP413，空文件返回HTTP422。\n")
            spec = DenseIndexSpec(collection_name=collection, embedding_model=vectors.model,
                                  dimensions=vectors.expected_dimensions, splitter_version=SplitConfig().index_version)
            store = DenseQdrantStore(client, collection_name=collection, dimensions=spec.dimensions)
            manifest = ManifestStore(output / "manifests", spec)
            completed = IngestionService(vectors, store, manifest).ingest_file(document)
            retriever = DenseRetriever(vectors, store, manifest, top_k=1)
            backend = ReadOnlyBackend(settings, database, knowledge_search=retriever.search)
            registry = build_readonly_registry(backend)
            ticket_source = "ticket:" + str(ticket.id)
            knowledge_source = "knowledge:" + str(completed.chunk_ids[0])
            cases = [
                ("knowledge", "请使用search_knowledge查询上传文件大小上限及超限HTTP状态码，依据知识来源回答。",
                 [("search_knowledge", {"query": "上传文件大小上限与HTTP413"})], [knowledge_source]),
                ("ticket", f"请使用get_ticket查询工单{ticket.id}并告诉我标题和状态，依据工单来源回答。",
                 [("get_ticket", {"ticket_id": str(ticket.id)})], [ticket_source]),
                ("combined", f"请先search_tickets搜索D24上传故障，再search_knowledge核对上传上限，结合工单与知识解释HTTP413；需要两类来源。工单ID{ticket.id}。",
                 [("search_tickets", {"q": "D24上传故障", "limit": 5}),
                  ("search_knowledge", {"query": "上传文件大小上限与HTTP413"})], [ticket_source, knowledge_source]),
            ]
            receipt.update({"postgresql_version": None, "collection": collection,
                            "ticket": ticket.model_dump(mode="json"), "runs": [], "external_embedding_calls": 0})
            with database.transaction() as session:
                from sqlalchemy import text
                receipt["postgresql_version"] = session.scalar(text("SHOW server_version"))
            for name, question, plan, source_ids in cases:
                archived_run = next((run for run in archive["runs"] if run["path"] == name), None) if archive else None
                if archived_run:
                    question = archived_run["question"]
                if args.live:
                    async with create_agent_model(settings) as live_model:
                        model = RecordingModel(live_model)
                        result = await AgentRunner(model, registry, GraphLimits()).run(AgentQuestion(question=question), run_id=str(uuid4()))
                else:
                    model = RecordingModel(ReplayedModel(archived_run["decisions"]) if archived_run else ScriptedModel(plan, source_ids))
                    result = await AgentRunner(model, registry, GraphLimits()).run(AgentQuestion(question=question), run_id=str(uuid4()))
                receipt["runs"].append({"path": name, "question": question,
                                        "result": result.model_dump(mode="json"), "decisions": model.decisions})
                receipt["external_embedding_calls"] = vectors.calls if args.live else 0
                receipt["external_chat_calls"] = sum(len(r["decisions"]) for r in receipt["runs"]) if args.live else 0
                _write(output / "report.json", receipt)
                assert result.status == "answered", (name, result.failure_type)
                if archived_run:
                    old = archived_run["result"]
                    assert result.answer == old["answer"] and result.usage == old["usage"]
                    assert [s.id for s in result.sources] == [s["id"] for s in old["sources"]]
                    assert [e.name for e in result.events] == [e["name"] for e in old["events"]]
                assert set(source_ids) <= {s.id for s in result.sources}, name
                expected = {item[0] for item in plan}
                assert expected <= {e.name for e in result.events if e.status == "ok"}, name
                with database.transaction() as session:
                    assert session.scalar(select(func.count()).select_from(Ticket)) == 1
                assert database.engine.pool.checkedout() == 0
            receipt.update({"status": "ok", "ticket_count_after": 1, "pool_checked_out_after": 0})
        finally:
            receipt["external_embedding_calls"] = vectors.calls if args.live and vectors else 0
            if args.live and embeddings is not None:
                embeddings.close()
            # Delete only this run's freshly-created test collection and random test database.
            try:
                if client.collection_exists(collection):
                    client.delete_collection(collection)
            finally:
                client.close()
                database.close()
    receipt["owned_test_resources_cleaned"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--replay-model", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.local:
        parser.error("必须显式添加--local；--live另行允许真实模型，默认只做受控决策/假向量的真实存储集成")
    if args.live and args.replay_model:
        parser.error("真实采集与已保存模型回放不能同时执行")
    if args.output_dir.exists():
        parser.error("输出目录已存在，禁止覆盖原验收")
    args.output_dir.mkdir(parents=True)
    source_paths = [str(p) for parent in [Path("src/project_copilot/agent")] for p in parent.glob("*.py")]
    source_paths += ["src/project_copilot/infrastructure/agent_model.py", "src/project_copilot/schemas/agent.py",
                     "src/project_copilot/agent/backend.py", "scripts/agent_smoke.py", "uv.lock"]
    receipt = {"status": "running", "executor": "Codex/CLI user", "mode": "live_model_real_services" if args.live else "replayed_model_real_services" if args.replay_model else "scripted_model_real_services",
               "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
               "source_sha256": {name: sha256(Path(name).read_bytes()).hexdigest() for name in sorted(set(source_paths))},
               "automatic_retries": 0, "actual_account_cost": None, "external_chat_calls": 0}
    if args.replay_model:
        receipt["model_archive_sha256"] = sha256(args.replay_model.read_bytes()).hexdigest()
        receipt["usage_source"] = "historical live model messages; no new paid model requests"
    try:
        asyncio.run(execute(args, args.output_dir, receipt))
    except Exception as exc:
        receipt.update({"status": "failed", "error_type": type(exc).__name__})
        _write(args.output_dir / "report.json", receipt)
        print(json.dumps({"status": "error", "message": "Agent验收失败，已停止后续路径；检查局部报告，无自动重试"}, ensure_ascii=False))
        return 1
    _write(args.output_dir / "report.json", receipt)
    print(json.dumps({"status": "ok", "mode": receipt["mode"], "paths": len(receipt["runs"]),
                      "chat_calls": receipt["external_chat_calls"], "embedding_calls": receipt["external_embedding_calls"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
