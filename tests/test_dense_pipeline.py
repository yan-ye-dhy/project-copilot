"""离线验证真实Qdrant本地模式与入库完成边界，不调用模型服务。"""

from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from project_copilot.infrastructure.models import EmbeddingRequestError
from project_copilot.infrastructure.qdrant import (
    DenseQdrantStore, QdrantConfigError, QdrantRequestError, QdrantResponseError,
)
from project_copilot.rag.ingestion import IngestionError, IngestionService, ManifestError, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import DenseIndexSpec, IndexManifest, RetrievedChunk


FIXTURES = Path(__file__).parent / "fixtures" / "documents"


class FakeEmbeddings:
    """固定二维向量只用于核实工程流程，不代表真实语义检索效果。"""

    model = "offline-test-model"
    expected_dimensions = 2

    def __init__(self) -> None:
        self.documents: list[list[str]] = []
        self.queries: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]


@pytest.fixture
def pipeline(tmp_path: Path):
    embeddings = FakeEmbeddings()
    spec = DenseIndexSpec(
        collection_name="pc_test_dense", embedding_model=embeddings.model,
        dimensions=2, splitter_version=SplitConfig().index_version,
    )
    manifest = ManifestStore(tmp_path / "manifests", spec)
    with DenseQdrantStore(
        QdrantClient(":memory:"), collection_name=spec.collection_name, dimensions=2,
    ) as store:
        yield embeddings, store, manifest


@pytest.mark.parametrize("name", ["sample.md", "sample.pdf"])
def test_file_ingestion_roundtrip_preserves_d3_identity_and_repeat_upsert(pipeline, name: str) -> None:
    embeddings, store, manifest = pipeline
    path = FIXTURES / name
    expected = split_document(load_document(path))
    service = IngestionService(embeddings, store, manifest)
    completed = service.ingest_file(path)
    repeated = service.ingest_file(path)
    assert completed == repeated
    assert completed.chunk_ids == tuple(chunk.metadata.chunk_id for chunk in expected)
    assert manifest.load().documents == (completed,)
    assert store._client.count(store.collection_name, exact=True).count == len(expected)
    assert embeddings.documents == [[chunk.text for chunk in expected]] * 2

    hits = DenseRetriever(embeddings, store, manifest, top_k=20).search("查找文档来源")
    assert {hit.metadata.chunk_id for hit in hits} == set(completed.chunk_ids)
    original = {chunk.metadata.chunk_id: chunk for chunk in expected}
    for hit in hits:
        assert hit.text == original[hit.metadata.chunk_id].text
        assert hit.metadata == original[hit.metadata.chunk_id].metadata
    assert embeddings.queries == ["查找文档来源"]
    if name.endswith(".pdf"):
        assert {hit.metadata.page for hit in hits} == {0, 2}


def test_real_qdrant_query_excludes_unregistered_documents_and_other_dense_version(pipeline) -> None:
    embeddings, store, manifest = pipeline
    completed = IngestionService(embeddings, store, manifest).ingest_file(FIXTURES / "sample.pdf")
    unregistered = split_document(load_document(FIXTURES / "sample.txt"))
    store.upsert(unregistered, [[1.0, 0.0]] * len(unregistered),
                 dense_index_version=manifest.spec.index_version)
    original = split_document(load_document(FIXTURES / "sample.pdf"))[0]
    # 同一已登记文档内的旧版本点：只靠document_id过滤不足以排除它。
    wrong_version = DocumentChunk(
        text=original.text, metadata=original.metadata.model_copy(update={"chunk_id": uuid4()}),
    )
    store.upsert([wrong_version], [[1.0, 0.0]],
                 dense_index_version="dense-v1-other")

    hits = DenseRetriever(embeddings, store, manifest, top_k=20).search("原始页码")
    assert {hit.metadata.chunk_id for hit in hits} == set(completed.chunk_ids)
    assert all(hit.metadata.document_id == completed.document_id for hit in hits)


@pytest.mark.parametrize("failure", ["empty", "malformed_manifest", "different_spec", "collection"])
def test_ingestion_preflight_rejects_without_embedding(pipeline, tmp_path: Path, monkeypatch, failure: str) -> None:
    embeddings, store, manifest = pipeline
    path = FIXTURES / "sample.txt"
    expected_error: type[Exception] = ManifestError
    if failure == "empty":
        path = tmp_path / "empty.txt"
        path.write_text("", encoding="utf-8")
        expected_error = IngestionError
    elif failure in ("malformed_manifest", "different_spec"):
        manifest.path.parent.mkdir()
        raw = "{invalid"
        if failure == "different_spec":
            incompatible = manifest.spec.model_copy(update={"embedding_model": "different-model"})
            raw = IndexManifest(spec=incompatible).model_dump_json()
        manifest.path.write_text(raw, encoding="utf-8")
    else:
        expected_error = QdrantConfigError
        def reject_collection() -> None:
            raise QdrantConfigError("已有集合维度不匹配")
        monkeypatch.setattr(store, "ensure_collection", reject_collection)

    with pytest.raises(expected_error):
        IngestionService(embeddings, store, manifest).ingest_file(path)
    assert embeddings.documents == []
    assert embeddings.queries == []


@pytest.mark.parametrize("failure", ["embedding", "upsert", "manifest_replace"])
def test_failures_never_register_document_as_completed(pipeline, monkeypatch, failure: str) -> None:
    embeddings, store, manifest = pipeline
    if failure == "embedding":
        def fail_embedding(_texts):
            raise EmbeddingRequestError("Embedding请求失败")
        monkeypatch.setattr(embeddings, "embed_documents", fail_embedding)
        expected_error = EmbeddingRequestError
    elif failure == "upsert":
        def fail_upsert(*_args, **_kwargs):
            raise QdrantRequestError("Qdrant写入失败")
        monkeypatch.setattr(store, "upsert", fail_upsert)
        expected_error = QdrantRequestError
    else:
        def fail_replace(*_args, **_kwargs):
            raise OSError("simulated disk write failure")
        monkeypatch.setattr(Path, "replace", fail_replace)
        expected_error = ManifestError

    with pytest.raises(expected_error):
        IngestionService(embeddings, store, manifest).ingest_file(FIXTURES / "sample.pdf")
    assert not manifest.path.exists()
    assert manifest.load().documents == ()
    # 清单保存失败时向量可能已落盘，但不能作为成功证据被查询。
    assert DenseRetriever(embeddings, store, manifest).search("有没有证据") == []
    assert embeddings.queries == []
    if failure == "manifest_replace":
        assert store._client.count(store.collection_name, exact=True).count == 2
        assert list(manifest.path.parent.glob(".manifest-*.tmp")) == []


def test_manifest_save_failure_preserves_previous_evidence_and_retry_is_idempotent(pipeline, monkeypatch) -> None:
    embeddings, store, manifest = pipeline
    service = IngestionService(embeddings, store, manifest)
    previous = service.ingest_file(FIXTURES / "sample.pdf")
    with monkeypatch.context() as failure:
        def reject_replace(*_args, **_kwargs):
            raise OSError("simulated disk write failure")
        failure.setattr(Path, "replace", reject_replace)
        with pytest.raises(ManifestError):
            service.ingest_file(FIXTURES / "sample.txt")
    assert manifest.load().documents == (previous,)
    assert {hit.metadata.document_id for hit in DenseRetriever(embeddings, store, manifest).search("来源")} == {
        previous.document_id
    }
    count_before_retry = store._client.count(store.collection_name, exact=True).count
    retried = service.ingest_file(FIXTURES / "sample.txt")
    assert manifest.load().documents == (previous, retried)
    assert store._client.count(store.collection_name, exact=True).count == count_before_retry


def test_empty_manifest_search_skips_collection_and_embedding(pipeline, monkeypatch) -> None:
    embeddings, store, manifest = pipeline
    def forbidden(*_args, **_kwargs):
        pytest.fail("空清单不能访问模型或Qdrant")
    monkeypatch.setattr(store, "ensure_collection", forbidden)
    monkeypatch.setattr(store, "query", forbidden)
    monkeypatch.setattr(embeddings, "embed_query", forbidden)
    assert DenseRetriever(embeddings, store, manifest).search("新索引查询") == []


@pytest.mark.parametrize("query", ["", "   ", None, 123])
def test_invalid_query_is_rejected_before_manifest_or_external_calls(pipeline, monkeypatch, query) -> None:
    embeddings, store, manifest = pipeline
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法问题必须在读取清单和外部请求前拒绝")
    monkeypatch.setattr(manifest, "load", forbidden)
    monkeypatch.setattr(store, "ensure_collection", forbidden)
    monkeypatch.setattr(embeddings, "embed_query", forbidden)
    with pytest.raises(ValueError, match="非空文本"):
        DenseRetriever(embeddings, store, manifest).search(query)


@pytest.mark.parametrize("tamper", ["chunk_id", "splitter_version", "content_hash"])
def test_retriever_rejects_evidence_not_matching_completed_identity(pipeline, monkeypatch, tamper: str) -> None:
    embeddings, store, manifest = pipeline
    IngestionService(embeddings, store, manifest).ingest_file(FIXTURES / "sample.pdf")
    chunk = split_document(load_document(FIXTURES / "sample.pdf"))[0]
    updates = {"chunk_id": uuid4()} if tamper == "chunk_id" else (
        {"index_version": "different-splitter"} if tamper == "splitter_version" else {"content_hash": "0" * 64}
    )
    forged = RetrievedChunk(
        text=chunk.text, metadata=chunk.metadata.model_copy(update=updates), score=1.0,
    )
    monkeypatch.setattr(store, "query", lambda *_args, **_kwargs: [forged])
    with pytest.raises(QdrantResponseError):
        DenseRetriever(embeddings, store, manifest).search("检查证据身份")


@pytest.mark.parametrize("binding", ["model", "dimensions", "collection", "splitter"])
def test_index_binding_changes_require_compatible_spec_before_requests(pipeline, binding: str) -> None:
    embeddings, store, manifest = pipeline
    split_config = SplitConfig()
    if binding == "model":
        embeddings.model = "another-model"
    elif binding == "dimensions":
        embeddings.expected_dimensions = 3
    elif binding == "collection":
        store.collection_name = "pc_other"
    else:
        split_config = SplitConfig(chunk_size=600, chunk_overlap=100)
    with pytest.raises(IngestionError):
        IngestionService(embeddings, store, manifest, split_config=split_config)
    assert embeddings.documents == embeddings.queries == []
