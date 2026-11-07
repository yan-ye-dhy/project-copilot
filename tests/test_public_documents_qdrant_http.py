"""Opt-in HTTP integration against the dedicated local ephemeral Qdrant only."""
import os
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.bm25 import BM25Index
from project_copilot.rag.deletion import DeletionError, DocumentDeletion
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import IngestionService, ManifestError, ManifestStore
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec

pytestmark = pytest.mark.skipif(os.environ.get("PC_TEST_WEB_QDRANT") != "1", reason="requires dedicated local ephemeral Qdrant on 16433")


class FakeEmbedding:
    model = "embedding-3"
    expected_dimensions = 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _):
        return [1.0, 0.0]


def test_real_http_delete_interruption_and_recovery(tmp_path, monkeypatch):
    # The fixed port is intentionally different from the project's 6433.
    client = QdrantClient(url="http://127.0.0.1:16433", trust_env=False, check_compatibility=False)
    name = "pc_web_test_" + uuid4().hex
    split = SplitConfig(chunk_size=80, chunk_overlap=10)
    spec = DenseIndexSpec(collection_name=name, embedding_model="embedding-3", dimensions=2, splitter_version=split.index_version)
    manifest = ManifestStore(tmp_path / "manifest", spec)
    embedding = FakeEmbedding()
    store = DenseQdrantStore(client, collection_name=name, dimensions=2)
    ingest = IngestionService(embedding, store, manifest, split_config=split)
    try:
        docs = []
        for filename, content in (("a.txt", "竹林协议设备故障排查。" * 20), ("b.txt", "云山协议用户服务说明。" * 20)):
            path = tmp_path / filename
            path.write_text(content, encoding="utf-8")
            docs.append(ingest.ingest_file(path))
        SnapshotManager(manifest).synchronize(client)
        original = client.delete

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("controlled post-delete interruption")

        monkeypatch.setattr(client, "delete", interrupted)
        with pytest.raises(DeletionError):
            DocumentDeletion(manifest, client).delete(docs[0].document_id)
        with pytest.raises(ManifestError):
            DenseRetriever(embedding, store, manifest).search("竹林")
        monkeypatch.setattr(client, "delete", original)
        DocumentDeletion(manifest, client).delete(docs[0].document_id)
        assert manifest.load().documents == (docs[1],)
        assert not client.retrieve(name, ids=[str(value) for value in docs[0].chunk_ids])
        assert len(client.retrieve(name, ids=[str(value) for value in docs[1].chunk_ids])) == len(docs[1].chunk_ids)
        snapshot = BM25Index.load(manifest.path.with_suffix(".bm25.json"), manifest.load())
        assert {item.metadata.document_id for item in snapshot.snapshot.chunks} == {docs[1].document_id}
        assert {hit.metadata.document_id for hit in DenseRetriever(embedding, store, manifest).search("云山")} == {docs[1].document_id}
    finally:
        # This UUID-scoped collection was created by this test in the disposable server.
        if client.collection_exists(name):
            client.delete_collection(name)
        client.close()
