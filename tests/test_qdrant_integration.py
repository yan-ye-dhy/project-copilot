"""显式启用的真实HTTP存储验收；假二维向量，不调用模型或读取.env。"""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantConfigError
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import DenseIndexSpec


pytestmark = pytest.mark.skipif(
    os.environ.get("PROJECT_COPILOT_RUN_QDRANT_INTEGRATION") != "1",
    reason="真实Qdrant HTTP测试须显式启用，不属于默认离线测试",
)
FIXTURES = Path(__file__).parent / "fixtures" / "documents"


def test_real_http_dense_storage_and_completion_boundary(tmp_path: Path) -> None:
    class TestEmbeddings:
        model = "offline-http-test"
        expected_dimensions = 2

        def embed_documents(self, texts):
            return [[1.0, 0.0] if "page one" in text else [0.0, 1.0] for text in texts]

        def embed_query(self, _text):
            return [1.0, 0.0]

    collection = "pc_test_d4_" + uuid4().hex
    client = QdrantClient(
        url=os.environ.get("PROJECT_COPILOT_QDRANT_URL", "http://127.0.0.1:6433"),
        timeout=10, prefer_grpc=False, check_compatibility=False, trust_env=False,
    )
    spec = DenseIndexSpec(
        collection_name=collection, embedding_model=TestEmbeddings.model,
        dimensions=2, splitter_version=SplitConfig().index_version,
    )
    store = DenseQdrantStore(client, collection_name=collection, dimensions=2)
    manifest = ManifestStore(tmp_path, spec)
    try:
        # 这是独立HTTP服务器，不是SDK的:memory:模式。
        assert client.info().version == "1.19.2"
        service = IngestionService(TestEmbeddings(), store, manifest)
        completed = service.ingest_file(FIXTURES / "sample.pdf")
        assert service.ingest_file(FIXTURES / "sample.pdf") == completed
        assert client.count(collection, exact=True).count == 2
        assert manifest.load().documents == (completed,)

        # 未登记文档即使已经写入同一集合，也不能作为完成文档被返回。
        pending = split_document(load_document(FIXTURES / "sample.txt"))
        store.upsert(pending, [[1.0, 0.0]] * len(pending),
                     dense_index_version=spec.index_version)
        hits = DenseRetriever(TestEmbeddings(), store, manifest).search("page one")
        expected = {c.metadata.chunk_id: c for c in split_document(load_document(FIXTURES / "sample.pdf"))}
        assert len(hits) == 2
        assert hits[0].metadata.page == 0 and hits[0].score == pytest.approx(1.0)
        assert {hit.metadata.page for hit in hits} == {0, 2}
        for hit in hits:
            assert hit.text == expected[hit.metadata.chunk_id].text
            assert hit.metadata == expected[hit.metadata.chunk_id].metadata

        # 配置冲突必须拒绝，不能删除/重建已有集合。
        before = client.count(collection, exact=True).count
        incompatible = DenseQdrantStore(client, collection_name=collection, dimensions=3)
        with pytest.raises(QdrantConfigError, match="维度或距离"):
            incompatible.ensure_collection()
        assert client.count(collection, exact=True).count == before
    finally:
        # 仅清理本次生成的pc_test_集合，保留应用集合与其他项目数据。
        try:
            if client.collection_exists(collection):
                client.delete_collection(collection)
        finally:
            client.close()
