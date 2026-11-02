"""Synthetic, isolated Qdrant deletion and recovery; no model or real collection."""
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from qdrant_client import QdrantClient

from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.bm25 import BM25Index
from project_copilot.rag.deletion import DeletionError, DocumentDeletion, DocumentNotFound
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import IngestionService, ManifestError, ManifestStore
from project_copilot.rag.index_lock import IndexLockError, index_lock
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec
from scripts.bm25_index import _read_manifest


class OfflineEmbedding:
    model = "embedding-3"
    expected_dimensions = 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _text):
        return [1.0, 0.0]


def test_index_lock_is_reentrant_only_for_the_owning_thread(tmp_path):
    path = tmp_path / "manifest.json"

    def contender():
        with index_lock(path, timeout_seconds=0.1):
            return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        with index_lock(path):
            with index_lock(path, timeout_seconds=0.1):
                with pytest.raises(IndexLockError):
                    executor.submit(contender).result(timeout=2)
        assert executor.submit(contender).result(timeout=2) is True


def test_lock_canonicalizes_symlinks_and_does_not_modify_evaluation_archive(tmp_path):
    archive = tmp_path / "evals" / "results" / "run-01"
    archive.mkdir(parents=True)
    manifest = archive / "index_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    alias = tmp_path / "archive-link"
    alias.symlink_to(archive, target_is_directory=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with index_lock(manifest):
            def contender():
                with index_lock(alias / manifest.name, timeout_seconds=0.1):
                    return True
            with pytest.raises(IndexLockError):
                executor.submit(contender).result(timeout=2)
    assert list(archive.iterdir()) == [manifest]


@pytest.fixture
def pipeline(tmp_path):
    embeddings = OfflineEmbedding()
    split = SplitConfig(chunk_size=80, chunk_overlap=10)
    spec = DenseIndexSpec(collection_name="pc_deletion_memory", embedding_model=embeddings.model,
                          dimensions=2, splitter_version=split.index_version)
    client = QdrantClient(":memory:")
    store = DenseQdrantStore(client, collection_name=spec.collection_name, dimensions=2)
    manifest = ManifestStore(tmp_path / "manifests", spec)
    ingest = IngestionService(embeddings, store, manifest, split_config=split)
    docs = []
    for filename, content in (("a.txt", "竹林协议设备故障排查。" * 30),
                              ("b.txt", "云山协议用户服务说明。" * 30)):
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        docs.append(ingest.ingest_file(path))
    SnapshotManager(manifest).synchronize(client)
    yield SimpleNamespace(client=client, store=store, manifest=manifest, spec=spec,
                          embeddings=embeddings, docs=docs)
    client.close()


def test_delete_exact_points_and_rebuild_snapshot_without_losing_other_doc(pipeline):
    state = pipeline
    first, second = state.docs
    result = DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert result == first
    assert state.manifest.load().documents == (second,)
    assert not state.manifest.deletion_journal_path.exists()
    assert state.client.retrieve(state.spec.collection_name, ids=[str(x) for x in first.chunk_ids]) == []
    assert len(state.client.retrieve(state.spec.collection_name, ids=[str(x) for x in second.chunk_ids])) == len(second.chunk_ids)
    snapshot = BM25Index.load(state.manifest.path.with_suffix(".bm25.json"), state.manifest.load())
    assert {chunk.metadata.document_id for chunk in snapshot.snapshot.chunks} == {second.document_id}
    assert {hit.metadata.document_id for hit in DenseRetriever(state.embeddings, state.store, state.manifest).search("云山")} == {second.document_id}
    with pytest.raises(DocumentNotFound):
        DocumentDeletion(state.manifest, state.client).delete(first.document_id)


def test_interrupted_delete_blocks_manifest_and_cli_until_same_operation_recovers(pipeline, monkeypatch):
    state = pipeline
    first, second = state.docs
    original = state.client.delete

    def crash_after_delete(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated crash after physical deletion")

    monkeypatch.setattr(state.client, "delete", crash_after_delete)
    with pytest.raises(DeletionError):
        DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert state.manifest.deletion_journal_path.exists()
    with pytest.raises(ManifestError):
        state.manifest.load()
    with pytest.raises(ManifestError):
        DenseRetriever(state.embeddings, state.store, state.manifest).search("竹林")
    with pytest.raises(ValueError):
        _read_manifest(state.manifest.path)
    with pytest.raises(DeletionError):
        DocumentDeletion(state.manifest, state.client).delete(second.document_id)
    monkeypatch.setattr(state.client, "delete", original)
    assert DocumentDeletion(state.manifest, state.client).delete(first.document_id) == first
    assert state.manifest.load().documents == (second,)
    assert not state.manifest.deletion_journal_path.exists()


def test_snapshot_publish_failure_is_recoverable_after_manifest_change(pipeline, monkeypatch):
    state = pipeline
    first, second = state.docs
    original = SnapshotManager.get_locked

    def fail(*args, **kwargs):
        raise RuntimeError("simulated crash before snapshot")

    monkeypatch.setattr(SnapshotManager, "get_locked", fail)
    with pytest.raises(DeletionError):
        DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert state.manifest.load(_pending_delete_ok=True).documents == (second,)
    with pytest.raises(ManifestError):
        state.manifest.load()
    monkeypatch.setattr(SnapshotManager, "get_locked", original)
    DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert BM25Index.load(state.manifest.path.with_suffix(".bm25.json"), state.manifest.load()).snapshot.manifest.documents == (second,)


def test_invalid_journal_blocks_access_and_never_deletes_points(pipeline):
    state = pipeline
    first, _ = state.docs
    state.manifest.deletion_journal_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ManifestError):
        state.manifest.load()
    with pytest.raises(DeletionError):
        DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert len(state.client.retrieve(state.spec.collection_name, ids=[str(x) for x in first.chunk_ids])) == len(first.chunk_ids)


def test_mismatched_existing_qdrant_payload_is_not_deleted(pipeline):
    state = pipeline
    first, _ = state.docs
    state.client.set_payload(state.spec.collection_name, payload={"unexpected": True},
                             points=[str(first.chunk_ids[0])], wait=True)
    with pytest.raises(DeletionError):
        DocumentDeletion(state.manifest, state.client).delete(first.document_id)
    assert not state.manifest.deletion_journal_path.exists()
    assert state.manifest.load().documents == tuple(state.docs)
    assert len(state.client.retrieve(state.spec.collection_name, ids=[str(x) for x in first.chunk_ids])) == len(first.chunk_ids)
