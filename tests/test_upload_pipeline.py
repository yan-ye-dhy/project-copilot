"""上传贯通真实D3/D4和内存Qdrant；Embedding固定假向量，不请求模型或外部服务。"""

import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from qdrant_client import QdrantClient

from project_copilot.config import Settings
from project_copilot.infrastructure.models import EmbeddingRequestError
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantRequestError
from project_copilot.main import create_app
from project_copilot.rag import ingestion
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.schemas.uploads import DocumentUploadResult


FIXTURES = Path(__file__).parent / "fixtures" / "documents"


class OfflineEmbeddings:
    """二维固定值只验证工程协作，不代表真实Embedding或语义质量。"""

    model = "embedding-3"
    expected_dimensions = 2

    def __init__(self):
        self.documents = []

    def embed_documents(self, texts):
        self.documents.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _text):
        pytest.fail("上传不应请求问题Embedding")


def upload(settings, filename, content, media_type="application/octet-stream"):
    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            ) as client:
                return await client.post("/documents/upload", files={"file": (filename, content, media_type)})
    return asyncio.run(run())


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None,
                        embedding_expected_dimensions=2, manifest_dir=tmp_path / "manifests",
                        environment="test")
    state = SimpleNamespace(settings=settings, embeddings=OfflineEmbeddings(), events=[],
                            paths=[], snapshots=[], failure=None)
    original_ingest = IngestionService.ingest_file

    def record_ingest(service, path, *, source_name=None):
        path = Path(path)
        assert path.is_file()
        assert path.name != source_name
        state.paths.append(path)
        return original_ingest(service, path, source_name=source_name)

    @contextmanager
    def embeddings_factory(active):
        assert active is settings
        state.events.append("embedding_opened")
        try:
            yield state.embeddings
        finally:
            state.events.append("embedding_closed")

    @contextmanager
    def store_factory(active):
        assert active is settings
        store = DenseQdrantStore(QdrantClient(":memory:"),
                                 collection_name=settings.qdrant_collection_name, dimensions=2)
        state.events.append("qdrant_opened")
        try:
            if state.failure == "qdrant":
                def reject(*_args, **_kwargs):
                    raise QdrantRequestError("private-write-response")
                monkeypatch.setattr(store, "upsert", reject)
            yield store
        finally:
            if store._client.collection_exists(store.collection_name):
                points, _ = store._client.scroll(store.collection_name, limit=100, with_payload=True)
                state.snapshots.append(points)
            store.close()
            state.events.append("qdrant_closed")

    monkeypatch.setattr(IngestionService, "ingest_file", record_ingest)
    monkeypatch.setattr(ingestion, "create_embedding_client", embeddings_factory)
    monkeypatch.setattr(ingestion, "create_qdrant_store", store_factory)
    return state


@pytest.mark.parametrize("fixture_name,source_name", [
    ("sample.txt", "上传原文.TXT"), ("sample.md", "上传说明.md"), ("sample.pdf", "上传手册.pdf"),
])
def test_three_formats_preserve_source_pages_and_stable_d3_identity(pipeline, fixture_name, source_name):
    source = FIXTURES / fixture_name
    expected = split_document(load_document(source))
    response = upload(pipeline.settings, source_name, source.read_bytes())
    assert response.status_code == 200
    result = DocumentUploadResult.model_validate(response.json())
    assert result.document.document_id == expected[0].metadata.document_id
    assert result.document.content_hash == expected[0].metadata.content_hash
    assert result.document.source_name == source_name
    assert result.document.chunk_ids == tuple(chunk.metadata.chunk_id for chunk in expected)
    assert result.chunk_count == len(expected)
    assert result.size_bytes == source.stat().st_size
    assert str(result.run_id) == response.headers["X-Run-ID"] and result.trace_exported is False
    points = pipeline.snapshots[0]
    assert {point.id for point in points} == {str(chunk.metadata.chunk_id) for chunk in expected}
    by_id = {str(chunk.metadata.chunk_id): chunk for chunk in expected}
    for point in points:
        assert point.payload["text"] == by_id[point.id].text
        assert point.payload["metadata"] == by_id[point.id].metadata.model_copy(
            update={"source_name": source_name},
        ).model_dump(mode="json")
        assert point.payload["dense_index_version"] == result.index_version
    if fixture_name.endswith(".pdf"):
        assert {point.payload["metadata"]["page"] for point in points} == {0, 2}
    assert pipeline.embeddings.documents == [[chunk.text for chunk in expected]]
    spec = DenseIndexSpec(collection_name=pipeline.settings.qdrant_collection_name,
                          embedding_model="embedding-3", dimensions=2,
                          splitter_version=SplitConfig().index_version)
    assert ManifestStore(pipeline.settings.manifest_dir, spec).load().documents == (result.document,)
    assert len(pipeline.paths) == 1 and not pipeline.paths[0].exists()
    assert pipeline.events == ["embedding_opened", "qdrant_opened", "qdrant_closed", "embedding_closed"]


@pytest.mark.parametrize("filename,content", [
    ("invalid.txt", b"\xff\xfeprivate-document"),
    ("broken.pdf", b"%PDF-1.7\nnot a complete PDF private-document"),
    ("blank.txt", b" \t\r\n "), ("blank.md", b"\n\n\t "),
])
def test_invalid_or_blank_content_is_422_without_embedding_or_completed_manifest(
    pipeline, filename, content,
):
    response = upload(pipeline.settings, filename, content)
    assert response.status_code == 422
    assert "private-document" not in response.text
    assert pipeline.embeddings.documents == []
    assert not list(pipeline.settings.manifest_dir.glob("*.json"))
    assert len(pipeline.paths) == 1 and not pipeline.paths[0].exists()
    assert pipeline.events[-2:] == ["qdrant_closed", "embedding_closed"]


@pytest.mark.parametrize("failure,status", [("embedding", 502), ("qdrant", 502), ("manifest", 503)])
def test_model_write_and_manifest_failures_cleanup_without_success(pipeline, monkeypatch, failure, status):
    pipeline.failure = failure
    if failure == "embedding":
        def reject(_texts):
            raise EmbeddingRequestError("private-embedding-response")
        monkeypatch.setattr(pipeline.embeddings, "embed_documents", reject)
    elif failure == "manifest":
        original_replace = Path.replace

        def reject_replace(path, target):
            if path.name.startswith(".manifest-"):
                raise OSError("private-disk-path")
            return original_replace(path, target)
        monkeypatch.setattr(Path, "replace", reject_replace)

    response = upload(pipeline.settings, "上传手册.pdf", (FIXTURES / "sample.pdf").read_bytes())
    assert response.status_code == status
    assert "private-" not in response.text
    assert set(response.json()) == {"detail"}
    assert len(pipeline.paths) == 1 and not pipeline.paths[0].exists()
    assert pipeline.events == ["embedding_opened", "qdrant_opened", "qdrant_closed", "embedding_closed"]
    assert not list(pipeline.settings.manifest_dir.glob("*.json"))
    assert not list(pipeline.settings.manifest_dir.glob(".manifest-*.tmp"))
    # 清单失败可能发生在向量落盘之后，HTTP仍不能返回indexed。
    assert len(pipeline.snapshots[0]) == (2 if failure == "manifest" else 0)


def test_same_bytes_original_path_and_random_upload_path_keep_document_and_chunk_ids(tmp_path):
    embeddings = OfflineEmbeddings()
    spec = DenseIndexSpec(collection_name="pc_upload_identity", embedding_model=embeddings.model,
                          dimensions=2, splitter_version=SplitConfig().index_version)
    manifest = ManifestStore(tmp_path / "manifests", spec)
    original = FIXTURES / "sample.md"
    temporary = tmp_path / "server-random-0c82.md"
    temporary.write_bytes(original.read_bytes())
    with DenseQdrantStore(QdrantClient(":memory:"), collection_name=spec.collection_name, dimensions=2) as store:
        service = IngestionService(embeddings, store, manifest)
        first = service.ingest_file(original)
        uploaded = service.ingest_file(temporary, source_name="用户原始名称.md")
        assert uploaded.document_id == first.document_id
        assert uploaded.content_hash == first.content_hash
        assert uploaded.chunk_ids == first.chunk_ids
        assert uploaded.source_name == "用户原始名称.md"
        assert store._client.count(store.collection_name, exact=True).count == len(first.chunk_ids)
        assert manifest.load().documents == (uploaded,)
