"""隔离Qdrant集合中的真实上传→统一GPU检索→RAG/Agent验收；显式--live。"""

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import httpx

from project_copilot.agent import service as agent_service
from project_copilot.config import load_settings
from project_copilot.main import create_app
from project_copilot.rag import answering, final_retrieval, hybrid, ingestion
from project_copilot.rag.bm25 import BM25Index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("真实Embedding和对话请求需显式添加--live")
    if args.output_dir.exists():
        parser.error("输出目录已存在，拒绝覆盖")
    args.output_dir.mkdir(parents=True)
    collection = "pc_test_final_live_" + uuid4().hex
    settings = load_settings().model_copy(update={
        "qdrant_collection_name": collection,
        "manifest_dir": args.output_dir.resolve() / "manifests",
        "database_url": None,
        "deepseek_max_tokens": 512,
    })
    calls = {"embedding": 0, "chat": 0, "bm25": 0, "rrf": 0}
    receipt = {"status": "running", "mode": "real_http_qdrant_real_embedding_real_chat_real_cuda",
               "executor": "Codex/CLI user", "collection": collection, "calls": calls,
               "automatic_retries": 0, "actual_account_cost": None,
               "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip(),
               "responses": []}
    source_paths = ["scripts/final_rag_live_smoke.py", "src/project_copilot/rag/final_retrieval.py",
                    "src/project_copilot/rag/index_lock.py", "src/project_copilot/rag/ingestion.py",
                    "src/project_copilot/rag/answering.py", "src/project_copilot/agent/backend.py",
                    "src/project_copilot/agent/graph.py", "src/project_copilot/agent/tools.py",
                    "src/project_copilot/agent/service.py", "src/project_copilot/infrastructure/agent_model.py",
                    "src/project_copilot/rag/bm25.py", "src/project_copilot/rag/hybrid.py",
                    "src/project_copilot/rag/fusion.py", "src/project_copilot/rag/rerank.py",
                    "src/project_copilot/api/rag.py", "src/project_copilot/api/agent.py",
                    "src/project_copilot/api/documents.py", "src/project_copilot/main.py",
                    "src/project_copilot/config.py", "uv.lock"]
    receipt["source_sha256"] = {p: sha256(Path(p).read_bytes()).hexdigest() for p in source_paths}

    def save():
        (args.output_dir / "report.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")

    original_embedding = final_retrieval.create_embedding_client
    original_chat = answering.create_chat_client
    original_agent_model = agent_service.create_agent_model
    original_bm25 = BM25Index.search
    original_rrf = hybrid.reciprocal_rank_fusion

    @contextmanager
    def embeddings(active):
        with original_embedding(active) as wrapped:
            class Proxy:
                model = wrapped.model
                expected_dimensions = wrapped.expected_dimensions
                def embed_documents(self, values):
                    calls["embedding"] += 1
                    return wrapped.embed_documents(values)
                def embed_query(self, value):
                    calls["embedding"] += 1
                    return wrapped.embed_query(value)
            yield Proxy()

    @contextmanager
    def chat(active):
        with original_chat(active) as wrapped:
            class Proxy:
                model, temperature, max_tokens = wrapped.model, wrapped.temperature, wrapped.max_tokens
                def generate_json(self, messages):
                    calls["chat"] += 1
                    return wrapped.generate_json(messages)
            yield Proxy()

    @asynccontextmanager
    async def model(active):
        async with original_agent_model(active) as wrapped:
            class Proxy:
                async def ainvoke(self, messages, tools):
                    calls["chat"] += 1
                    return await wrapped.ainvoke(messages, tools)
            yield Proxy()

    def bm25(index, query, **kwargs):
        calls["bm25"] += 1
        return original_bm25(index, query, **kwargs)

    def rrf(rankings, **kwargs):
        calls["rrf"] += 1
        return original_rrf(rankings, **kwargs)

    final_retrieval.create_embedding_client = embeddings
    ingestion.create_embedding_client = embeddings
    answering.create_chat_client = chat
    agent_service.create_agent_model = model
    BM25Index.search = bm25
    hybrid.reciprocal_rank_fusion = rrf

    async def execute():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=180) as client:
                async def record(name, response, expected):
                    assert response.status_code == expected, (name, response.status_code)
                    assert response.headers.get("x-run-id")
                    body = response.json()
                    receipt["responses"].append({"name": name, "http": response.status_code,
                                                "run_id": response.headers["x-run-id"], "body": body})
                    save()
                    return body

                empty = await record("empty", await client.post("/rag/ask", json={"question": "蓝鹤协议识别码是什么？"}), 200)
                assert empty["status"] == "insufficient_evidence" and calls["chat"] == calls["embedding"] == 0
                text = "D24最终检索验收：蓝鹤协议的唯一识别码是 TICKET-ALPHA-739，知识文档只用于隔离测试。"
                uploaded = await record("upload", await client.post("/documents/upload", files={
                    "file": ("d24-final-test.txt", text.encode(), "text/plain")}), 200)
                assert uploaded["mode"] == "HYBRID_RERANK"
                manager = final_retrieval.get_final_retriever(settings)
                assert manager.snapshots.path.exists()
                snapshot_sha = sha256(manager.snapshots.path.read_bytes()).hexdigest()
                question = "蓝鹤协议的唯一识别码是什么？"
                rag = await record("rag", await client.post("/rag/ask", json={"question": question}), 200)
                assert rag["status"] == "answered" and "TICKET-ALPHA-739" in rag["answer"]
                assert any(c["metadata"]["source_name"] == "d24-final-test.txt" for c in rag["citations"])
                loaded_model_id = id(manager.reranker.scorer._model)
                assert manager.reranker.scorer._model is not None and manager.reranker._warm
                agent = await record("agent", await client.post("/agent/ask", json={"question":
                    "请调用search_knowledge检索蓝鹤协议并回答其唯一识别码，引用真实知识来源。"}), 200)
                assert agent["status"] == "answered" and "TICKET-ALPHA-739" in agent["answer"]
                assert any(e["name"] == "search_knowledge" and e["status"] == "ok" for e in agent["events"])
                assert any(s["data"]["metadata"]["source_name"] == "d24-final-test.txt" for s in agent["sources"])
                assert final_retrieval.get_final_retriever(settings) is manager
                assert id(manager.reranker.scorer._model) == loaded_model_id
                receipt.update({"single_process_model_reused": True, "snapshot_sha256": snapshot_sha,
                                "snapshot_inode": manager.snapshots.path.stat().st_ino,
                                "reranker_model": manager.reranker.scorer.config.model_name,
                                "reranker_revision": manager.reranker.scorer.config.revision,
                                "reranker_device": manager.reranker.scorer.config.device,
                                "reranker_dtype": manager.reranker.scorer.config.dtype,
                                "model_load_ms": manager.reranker.scorer.load_ms})
                import torch
                receipt["gpu_allocated_bytes_before_shutdown"] = torch.cuda.memory_allocated()
                receipt["gpu_peak_allocated_bytes"] = torch.cuda.max_memory_allocated()
        assert manager.reranker.scorer._model is None
        receipt["model_released_on_app_shutdown"] = True
        receipt["gpu_allocated_bytes_after_shutdown"] = torch.cuda.memory_allocated()

        # An independent Python process loads the same published snapshot after the first app shuts down.
        code = '''from pathlib import Path
import json,sys
from qdrant_client import QdrantClient
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec
spec=DenseIndexSpec(collection_name=sys.argv[2],embedding_model="embedding-3",dimensions=2048,splitter_version=SplitConfig().index_version)
client=QdrantClient(url=sys.argv[3],trust_env=False,check_compatibility=False)
try:
 m=SnapshotManager(ManifestStore(Path(sys.argv[1]),spec)); i=m.synchronize(client)
 print(json.dumps({"count":len(i.snapshot.chunks),"inode":m.path.stat().st_ino,"source":i.snapshot.chunks[0].metadata.source_name}))
finally: client.close()
'''
        process = subprocess.run([sys.executable, "-c", code, str(settings.manifest_dir), collection,
                                  str(settings.qdrant_url)], capture_output=True, text=True, timeout=30, check=True)
        restored = json.loads(process.stdout)
        assert restored["count"] == 1 and restored["source"] == "d24-final-test.txt"
        assert restored["inode"] == receipt["snapshot_inode"]
        receipt["new_process_snapshot_reuse"] = restored
        receipt["status"] = "ok"
        receipt["retained_test_collection"] = collection
        save()

    try:
        asyncio.run(execute())
    except Exception as exc:
        receipt.update(status="failed", error_type=type(exc).__name__)
        save()
        print(json.dumps({"status": "error", "type": type(exc).__name__, "calls": calls}))
        return 1
    print(json.dumps({"status": "ok", "calls": calls, "collection": collection}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
