"""Owned loopback Qdrant HTTP evidence checks; synthetic vectors, no model API."""
from contextlib import contextmanager
import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from project_copilot.config import Settings
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag import final_retrieval as final
from project_copilot.rag.bm25 import BM25Error
from project_copilot.rag.ingestion import IngestionService
from project_copilot.services.job_steps import fingerprint
from test_final_retrieval import ReverseReranker, Vectors


@pytest.fixture
def http_checkpoint(tmp_path, monkeypatch):
    if os.environ.get("PROJECT_COPILOT_RUN_FINAL_QDRANT") != "1":
        pytest.skip("dedicated HTTP Qdrant checkpoint test requires explicit enablement")
    url = os.environ.get("PROJECT_COPILOT_QDRANT_URL", "http://127.0.0.1:16433")
    if url != "http://127.0.0.1:16433":
        pytest.fail("checkpoint HTTP tests require the dedicated loopback Qdrant at port 16433")
    collection = "pc_test_checkpoint_" + uuid4().hex
    settings = Settings(_env_file=None, environment="test", qdrant_url=url,
        qdrant_collection_name=collection, manifest_dir=tmp_path,
        final_rerank_device="cpu", final_rerank_dtype="float32")
    client = QdrantClient(url=url, timeout=5, trust_env=False, check_compatibility=False)
    store = DenseQdrantStore(client, collection_name=collection, dimensions=2048)
    vectors, reranker = Vectors(), ReverseReranker()
    retriever = final.FinalRetriever(settings, reranker=reranker)
    @contextmanager
    def store_factory(_):
        yield store
    monkeypatch.setattr(final, "create_qdrant_store", store_factory)
    path = tmp_path / "checkpoint-evidence.txt"
    path.write_text("真实 HTTP 恢复核验的合成资料 CHECKPOINT-HTTP-381。", encoding="utf-8")
    document = IngestionService(vectors, store, retriever.manifest).ingest_file(path)
    monkeypatch.setattr(final, "create_embedding_client", lambda *_: pytest.fail("checkpoint requested embedding client"))
    monkeypatch.setattr(vectors, "embed_query", lambda *_: pytest.fail("checkpoint embedded query"))
    print("owned_checkpoint_collection=" + collection)
    try:
        yield settings, client, retriever, document
    finally:
        retriever.close()
        client.close()
        # Retain this newly created synthetic collection. Never delete shared
        # Qdrant data or treat an existing collection as a disposable fixture.


def test_http_checkpoint_view_preserves_manifest_evidence_without_embedding(http_checkpoint):
    settings, client, retriever, document = http_checkpoint
    generation, chunks = retriever.checkpoint_view()
    assert generation == fingerprint(retriever.manifest.load().model_dump(mode="json"))
    assert set(chunks) == set(document.chunk_ids)
    assert {chunk.text for chunk in chunks.values()} == {"真实 HTTP 恢复核验的合成资料 CHECKPOINT-HTTP-381。"}
    assert client.count(settings.qdrant_collection_name, exact=True).count == len(document.chunk_ids)
    assert retriever.reranker.prepares == 1 and retriever.reranker.seen == []


def test_http_checkpoint_view_rejects_corrupt_payload_and_unavailable_reranker(http_checkpoint, monkeypatch):
    settings, client, retriever, document = http_checkpoint
    _, original = retriever.checkpoint_view()
    identity = document.chunk_ids[0]
    client.set_payload(collection_name=settings.qdrant_collection_name,
        payload={"text": "controlled modified checkpoint evidence"}, points=[str(identity)], wait=True)
    with pytest.raises(BM25Error):
        retriever.checkpoint_view()
    client.set_payload(collection_name=settings.qdrant_collection_name,
        payload={"text": original[identity].text}, points=[str(identity)], wait=True)
    def unavailable():
        raise final.FinalRetrievalError("controlled unavailable local reranker")
    monkeypatch.setattr(retriever.reranker, "prepare", unavailable)
    with pytest.raises(final.FinalRetrievalError):
        retriever.checkpoint_view()
