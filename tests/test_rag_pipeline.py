"""离线贯通真实SDK、D4检索编排、D5回答与HTTP响应。"""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure import models
from project_copilot.main import create_app
from project_copilot.rag import answering
from project_copilot.rag import final_retrieval
from project_copilot.rag.rerank import RerankedHit
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, RetrievedChunk


@pytest.mark.parametrize("bad_answer", [False, True])
def test_http_question_runs_sdk_retrieval_answering_and_closes_clients(tmp_path, monkeypatch, bad_answer):
    settings = Settings(_env_file=None, zhipu_api_key="offline-zhipu-key",
                        deepseek_api_key="offline-deepseek-key", embedding_expected_dimensions=2048,
                        manifest_dir=tmp_path, environment="test")
    chunks = split_document(load_document(Path(__file__).parent / "fixtures/documents/sample.pdf"))
    spec = DenseIndexSpec(collection_name=settings.qdrant_collection_name, embedding_model="embedding-3",
                          dimensions=2048, splitter_version=SplitConfig().index_version)
    manifest = ManifestStore(tmp_path, spec)
    manifest.record(manifest.load(), CompletedDocument(
        document_id=chunks[0].metadata.document_id, content_hash=chunks[0].metadata.content_hash,
        source_name=chunks[0].metadata.source_name, chunk_ids=tuple(c.metadata.chunk_id for c in chunks),
    ))
    calls = []
    transports = []

    def embedding_http(request):
        calls.append("embedding")
        body = json.loads(request.content)
        assert body["input"] == ["第一页描述什么？"]
        return httpx.Response(200, json={"model": "embedding-3",
                                       "data": [{"index": 0, "embedding": [1.0] + [0.0] * 2047}]})

    def chat_http(request):
        calls.append("chat")
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert chunks[0].text in body["messages"][1]["content"]
        content = {"status": "answered", "answer": "本地文档加载。",
                   "citation_numbers": [9 if bad_answer else 1]}
        return httpx.Response(200, json={"model": "deepseek-actual-version", "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content)}}
        ]})

    def embeddings_factory(active):
        client = httpx.Client(transport=httpx.MockTransport(embedding_http))
        transports.append(client)
        return models.create_embedding_client(active, http_client=client)

    def chat_factory(active):
        client = httpx.Client(transport=httpx.MockTransport(chat_http))
        transports.append(client)
        return models.create_chat_client(active, http_client=client)

    class Store:
        collection_name = settings.qdrant_collection_name
        dimensions = 2048
        vector_name = "dense"
        _client = object()

        def ensure_collection(self):
            calls.append("collection")

        def query(self, vector, *, document_ids, dense_index_version, limit):
            calls.append("query")
            assert vector == [1.0] + [0.0] * 2047
            assert document_ids == [chunks[0].metadata.document_id]
            assert dense_index_version == spec.index_version
            assert limit == 10
            return [RetrievedChunk(**chunk.model_dump(), score=0.9) for chunk in chunks]

    @contextmanager
    def store_factory(active):
        try:
            yield Store()
        finally:
            calls.append("store_closed")

    class FakeReranker:
        def __init__(self, active):
            pass

        def prepare(self):
            pass

        def rerank(self, query, candidates):
            calls.append("rerank")
            ordered = sorted(candidates, key=lambda hit: hit.metadata.page)
            return [RerankedHit(hit=hit, candidate_rank=i, rerank_score=1.0)
                    for i, hit in enumerate(ordered, 1)]

        def close(self):
            pass

    monkeypatch.setattr(final_retrieval, "SharedReranker", FakeReranker)
    monkeypatch.setattr(final_retrieval, "read_completed_chunks", lambda client, current: chunks)
    monkeypatch.setattr(final_retrieval, "create_embedding_client", embeddings_factory)
    monkeypatch.setattr(answering, "create_chat_client", chat_factory)
    monkeypatch.setattr(final_retrieval, "create_qdrant_store", store_factory)

    async def request():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/rag/ask", json={"question": "第一页描述什么？"})

    response = asyncio.run(request())
    assert response.status_code == (502 if bad_answer else 200)
    assert calls == ["collection", "embedding", "query", "rerank", "store_closed", "chat"]
    assert all(client.is_closed for client in transports)
    if not bad_answer:
        body = response.json()
        assert body["answer"] == "本地文档加载。"
        assert body["citations"][0]["text"] == chunks[0].text
        assert body["citations"][0]["metadata"]["page"] == 0
        assert body["index_version"] == spec.index_version
        assert body["generation"]["actual_model"] == "deepseek-actual-version"
        assert body["generation"]["usage"] is None
