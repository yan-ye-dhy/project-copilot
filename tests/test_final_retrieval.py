"""统一检索及快照的可重复本地测试；固定向量和打分均非真实模型。"""

from contextlib import contextmanager
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from project_copilot.config import Settings
from pydantic import ValidationError
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag import final_retrieval as final
from project_copilot.rag.bm25 import BM25Error
from project_copilot.rag.ingestion import IngestionService
from project_copilot.rag.rerank import RerankedHit


class Vectors:
    model = "embedding-3"
    expected_dimensions = 2048

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * 2047 for _ in texts]

    def embed_query(self, query):
        return [1.0] + [0.0] * 2047


class ReverseReranker:
    def __init__(self):
        self.seen = []
        self.prepares = 0

    def prepare(self):
        self.prepares += 1

    def rerank(self, query, candidates):
        self.seen.append(list(candidates))
        return [RerankedHit(hit=hit, candidate_rank=len(candidates)-i, rerank_score=float(i))
                for i, hit in enumerate(reversed(candidates))]

    def close(self):
        pass


@pytest.fixture
def system(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, manifest_dir=tmp_path, environment="test")
    client = QdrantClient(":memory:")
    store = DenseQdrantStore(client, collection_name=settings.qdrant_collection_name, dimensions=2048)
    vectors = Vectors()
    reranker = ReverseReranker()
    retriever = final.FinalRetriever(settings, reranker=reranker)

    @contextmanager
    def embedding_factory(active):
        yield vectors

    @contextmanager
    def store_factory(active):
        yield store

    monkeypatch.setattr(final, "create_embedding_client", embedding_factory)
    monkeypatch.setattr(final, "create_qdrant_store", store_factory)
    yield settings, store, vectors, retriever
    retriever.close()
    client.close()


def ingest(system, tmp_path, name, text):
    settings, store, vectors, retriever = system
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return IngestionService(vectors, store, retriever.manifest).ingest_file(path)


def test_hybrid_candidates_are_reranked_and_preserve_identity(system, tmp_path):
    first = ingest(system, tmp_path, "one.txt", "上传文件超过5 MiB会返回HTTP413。")
    second = ingest(system, tmp_path, "two.txt", "数据库记录工单及状态。")
    retriever = system[3]
    hits = retriever.search("上传文件HTTP413")
    candidates = retriever.reranker.seen[0]
    assert len(candidates) == len(hits) == 2
    assert [h.metadata.chunk_id for h in hits] == [h.metadata.chunk_id for h in reversed(candidates)]
    assert {h.metadata.document_id for h in hits} == {first.document_id, second.document_id}
    assert all(h.text and h.metadata.content_hash for h in hits)
    assert retriever.reranker.prepares == 1


def test_snapshot_reused_then_rebuilt_after_successful_ingestion(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "one.txt", "上传文件限制5 MiB。")
    calls = []
    original = final.read_completed_chunks

    def counted(client, manifest):
        calls.append(len(manifest.documents))
        return original(client, manifest)

    monkeypatch.setattr(final, "read_completed_chunks", counted)
    retriever = system[3]
    retriever.search("上传")
    identity = retriever.snapshots.path.stat().st_ino
    retriever.search("上传")
    assert calls == [1] and retriever.snapshots.path.stat().st_ino == identity
    second = ingest(system, tmp_path, "two.txt", "新的工单数据和文档。")
    hits = retriever.search("工单")
    assert calls == [1, 2]
    assert second.document_id in {hit.metadata.document_id for hit in hits}


def test_stale_snapshot_failure_never_falls_back_and_retry_repairs(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "one.txt", "旧的成功文档。")
    retriever = system[3]
    retriever.search("旧文档")
    before = retriever.snapshots.path.read_bytes()
    ingest(system, tmp_path, "two.txt", "新的成功文档。")
    original = final.read_completed_chunks

    def fail(client, manifest):
        raise BM25Error("controlled snapshot rebuild failure")

    monkeypatch.setattr(final, "read_completed_chunks", fail)
    with pytest.raises(BM25Error):
        retriever.search("新的文档")
    assert retriever.snapshots.path.read_bytes() == before
    monkeypatch.setattr(final, "read_completed_chunks", original)
    assert len(retriever.search("新的文档")) == 2
    assert retriever.snapshots.path.read_bytes() != before


def test_corrupted_snapshot_is_not_read_as_valid(system, tmp_path):
    ingest(system, tmp_path, "one.txt", "文档中有HTTP413。")
    retriever = system[3]
    retriever.search("HTTP413")
    retriever.snapshots.path.write_text("not a complete snapshot")
    assert len(retriever.search("HTTP413")) == 1
    assert retriever.snapshots.path.read_text().startswith("{")


def test_missing_gpu_model_fails_before_embedding_and_never_falls_back(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "one.txt", "文档中有HTTP413。")
    retriever = system[3]
    def fail():
        raise final.FinalRetrievalError("controlled unavailable model")
    monkeypatch.setattr(retriever.reranker, "prepare", fail)
    monkeypatch.setattr(system[2], "embed_query", lambda query: pytest.fail("不能在预热失败后发Embedding"))
    with pytest.raises(final.FinalRetrievalError):
        retriever.search("HTTP413")


def test_atomic_publish_failure_keeps_old_snapshot_and_retry_recovers(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "one.txt", "第一份文档。")
    retriever = system[3]
    retriever.search("文档")
    old = retriever.snapshots.path.read_bytes()
    ingest(system, tmp_path, "two.txt", "第二份文档。")
    replace = final.os.replace
    monkeypatch.setattr(final.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("controlled")))
    with pytest.raises(BM25Error):
        retriever.search("文档")
    assert retriever.snapshots.path.read_bytes() == old
    assert not list(tmp_path.glob("*.tmp"))
    monkeypatch.setattr(final.os, "replace", replace)
    assert len(retriever.search("文档")) == 2


def test_empty_completed_manifest_returns_empty_without_model_or_embedding(system, monkeypatch):
    monkeypatch.setattr(final, "create_embedding_client", lambda settings: pytest.fail("空清单不应请求Embedding"))
    assert system[3].search("没有文档") == []
    assert system[3].reranker.prepares == 0


def test_rerank_timeout_fails_closed_until_running_work_finishes(tmp_path, monkeypatch):
    import threading
    event = threading.Event()
    settings = Settings(_env_file=None, manifest_dir=tmp_path, final_rerank_timeout_seconds=0.03)
    runtime = final.SharedReranker(settings)
    monkeypatch.setattr(runtime.scorer, "score", lambda *args: event.wait(5))
    try:
        with pytest.raises(final.FinalRetrievalError, match="超时"):
            runtime.prepare()
        with pytest.raises(final.FinalRetrievalError, match="不可用"):
            runtime.prepare()
    finally:
        event.set()
        runtime.close()


def test_inference_timeout_fails_closed_without_returning_candidates(tmp_path, monkeypatch):
    import threading
    event = threading.Event()
    settings = Settings(_env_file=None, manifest_dir=tmp_path, final_rerank_timeout_seconds=0.03)
    runtime = final.SharedReranker(settings)
    monkeypatch.setattr(runtime.reranker, "rerank", lambda *args: event.wait(5))
    runtime._warm = True
    try:
        with pytest.raises(final.FinalRetrievalError, match="超时"):
            runtime.rerank("查询", [object()])
        with pytest.raises(final.FinalRetrievalError, match="不可用"):
            runtime.rerank("查询", [object()])
    finally:
        event.set()
        runtime.close()


def test_cpu_requires_explicit_float32_and_agent_budget_covers_model_timeout():
    settings = Settings(_env_file=None, final_rerank_device="cpu", final_rerank_dtype="float32")
    assert settings.final_rerank_device == "cpu" and settings.final_rerank_dtype == "float32"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, final_rerank_device="cpu", final_rerank_dtype="float16")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_tool_timeout_seconds=10)
