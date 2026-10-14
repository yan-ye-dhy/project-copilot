"""D4存储契约：SDK本地模式与真实HTTP序列化，不冒充Docker服务验收。"""

import json
import traceback
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from project_copilot.config import PROJECT_ROOT, Settings
from project_copilot.infrastructure.qdrant import (
    DenseQdrantStore,
    QdrantConfigError,
    QdrantRequestError,
    QdrantResponseError,
    create_qdrant_store,
)
from project_copilot.schemas.documents import ChunkMetadata, DocumentChunk


COLLECTION = "pc_test_dense"
DENSE_VERSION = "dense-v0-test"
PRIVATE_RESPONSE = "private-qdrant-response-canary-do-not-log"


@pytest.fixture(autouse=True)
def isolate_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PROJECT_COPILOT_QDRANT_URL",
        "PROJECT_COPILOT_QDRANT_COLLECTION_NAME",
        "PROJECT_COPILOT_QDRANT_TIMEOUT_SECONDS",
        "PROJECT_COPILOT_MANIFEST_DIR",
        "PROJECT_COPILOT_RAG_TOP_K",
        "PROJECT_COPILOT_EMBEDDING_EXPECTED_DIMENSIONS",
    ):
        monkeypatch.delenv(name, raising=False)


def _chunk(text: str = "中文知识原文", *, document_id: UUID | None = None) -> DocumentChunk:
    return DocumentChunk(
        text=text,
        metadata=ChunkMetadata(
            document_id=document_id or uuid4(),
            content_hash="a" * 64,
            source_name="手测.txt",
            page=None,
            chunk_id=uuid4(),
            chunk_index=0,
            index_version="split-v1-test",
        ),
    )


def _payload(chunk: DocumentChunk, *, version: str = DENSE_VERSION) -> dict[str, object]:
    return {
        "text": chunk.text,
        "metadata": chunk.metadata.model_dump(mode="json"),
        "dense_index_version": version,
    }


def _store(client: QdrantClient | Mock) -> DenseQdrantStore:
    return DenseQdrantStore(client, collection_name=COLLECTION, dimensions=3)


def test_sdk_local_dense_retrieval_preserves_payload_and_filters_document_and_version() -> None:
    """真SDK本地存储/余弦排序；仍不是HTTP服务和真实2048维Embedding。"""
    allowed_id = uuid4()
    relevant = _chunk("匹配知识：接口限流排查", document_id=allowed_id)
    distant = _chunk("无关知识：部署方式", document_id=allowed_id)
    outside = _chunk("不在成功清单中的文档")
    old = _chunk("旧Dense版本", document_id=allowed_id)
    with _store(QdrantClient(":memory:")) as store:
        store.ensure_collection()
        store.upsert([relevant, distant, outside], [[1, 0, 0], [0, 1, 0], [1, 0, 0]], dense_index_version=DENSE_VERSION)
        store.upsert([old], [[1, 0, 0]], dense_index_version="dense-v0-old")
        found = store.query([1, 0, 0], document_ids=[allowed_id], dense_index_version=DENSE_VERSION)
        assert [item.metadata.chunk_id for item in found] == [relevant.metadata.chunk_id, distant.metadata.chunk_id]
        assert found[0].text == relevant.text
        assert found[0].metadata == relevant.metadata
        assert found[0].score == pytest.approx(1.0)
        assert found[1].score == pytest.approx(0.0)


def test_repeated_upsert_uses_existing_chunk_uuid_and_waits_for_completion() -> None:
    chunk = _chunk()
    with closing(QdrantClient(":memory:")) as client:
        store = _store(client)
        store.ensure_collection()
        store.upsert([chunk], [[1, 0, 0]], dense_index_version=DENSE_VERSION)
        store.upsert([chunk], [[0, 1, 0]], dense_index_version=DENSE_VERSION)
        assert client.count(COLLECTION, exact=True).count == 1
        points = client.retrieve(COLLECTION, ids=[str(chunk.metadata.chunk_id)], with_vectors=True)
        assert points[0].payload == _payload(chunk)
        assert points[0].vector == {"dense": [0.0, 1.0, 0.0]}


@pytest.mark.parametrize(
    ("vectors", "sparse"),
    [
        ({"dense": models.VectorParams(size=4, distance=models.Distance.COSINE)}, None),
        ({"dense": models.VectorParams(size=3, distance=models.Distance.DOT)}, None),
        (models.VectorParams(size=3, distance=models.Distance.COSINE), None),
        ({"other": models.VectorParams(size=3, distance=models.Distance.COSINE)}, None),
        ({"dense": models.VectorParams(size=3, distance=models.Distance.COSINE), "other": models.VectorParams(size=3, distance=models.Distance.COSINE)}, None),
        ({"dense": models.VectorParams(size=3, distance=models.Distance.COSINE)}, {"sparse": models.SparseVectorParams()}),
        ({"dense": models.VectorParams(size=3, distance=models.Distance.COSINE, multivector_config=models.MultiVectorConfig(comparator=models.MultiVectorComparator.MAX_SIM))}, None),
    ],
)
def test_existing_incompatible_collection_is_rejected_without_recreating(vectors: object, sparse: object) -> None:
    with closing(QdrantClient(":memory:")) as client:
        client.create_collection(COLLECTION, vectors_config=vectors, sparse_vectors_config=sparse)
        before = client.get_collection(COLLECTION).config
        with pytest.raises(QdrantConfigError):
            _store(client).ensure_collection()
        assert client.get_collection(COLLECTION).config == before


def test_real_sdk_http_contract_has_no_constructor_request_and_serializes_named_dense_filter() -> None:
    chunk = _chunk()
    with closing(QdrantClient(":memory:")) as local:
        _store(local).ensure_collection()
        info = local.get_collection(COLLECTION).model_dump(mode="json")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.extensions["timeout"]["read"] == 7.0
        path = request.url.path
        if path.endswith("/exists"):
            result: object = {"exists": True}
        elif request.method == "GET":
            result = info
        elif path.endswith("/points"):
            assert request.method == "PUT"
            assert request.url.params["wait"] == "true"
            body = json.loads(request.content)
            assert body["points"] == [{"id": str(chunk.metadata.chunk_id), "vector": {"dense": [1.0, 0.0, 0.0]}, "payload": _payload(chunk)}]
            result = {"operation_id": 1, "status": "completed"}
        else:
            assert path.endswith("/points/query")
            body = json.loads(request.content)
            assert body["query"] == {"nearest": [1.0, 0.0, 0.0]}
            assert body["using"] == "dense"
            assert body["limit"] == 5
            assert body["with_payload"] is True
            assert body["with_vector"] is False
            assert body["filter"]["must"] == [
                {"key": "metadata.document_id", "match": {"any": [str(chunk.metadata.document_id)]}},
                {"key": "dense_index_version", "match": {"value": DENSE_VERSION}},
            ]
            result = {"points": [{"id": str(chunk.metadata.chunk_id), "version": 1, "score": 0.9, "payload": _payload(chunk)}]}
        return httpx.Response(200, json={"status": "ok", "time": 0.01, "result": result})

    settings = Settings(_env_file=None, qdrant_url="http://127.0.0.1:6433", qdrant_collection_name=COLLECTION, qdrant_timeout_seconds=7, embedding_expected_dimensions=3)
    with create_qdrant_store(settings, transport=httpx.MockTransport(handler)) as store:
        assert requests == []
        store.ensure_collection()
        store.upsert([chunk], [[1, 0, 0]], dense_index_version=DENSE_VERSION)
        assert store.query([1, 0, 0], document_ids=[chunk.metadata.document_id], dense_index_version=DENSE_VERSION)[0].text == chunk.text
    assert len(requests) == 4


def test_empty_success_scope_and_empty_upsert_never_send_requests() -> None:
    client = Mock()
    store = _store(client)
    store.upsert([], [], dense_index_version=DENSE_VERSION)
    assert store.query([1, 0, 0], document_ids=[], dense_index_version=DENSE_VERSION) == []
    client.upsert.assert_not_called()
    client.query_points.assert_not_called()


@pytest.mark.parametrize("vector", [[1, 0], [True, 0, 0], ["1", 0, 0], [float("nan"), 0, 0], [float("inf"), 0, 0]])
def test_invalid_vectors_are_rejected_before_writes(vector: list[object]) -> None:
    client = Mock()
    with pytest.raises(QdrantConfigError):
        _store(client).upsert([_chunk()], [vector], dense_index_version=DENSE_VERSION)
    client.upsert.assert_not_called()


def test_all_vectors_are_validated_before_any_write_and_lengths_must_match() -> None:
    client = Mock()
    store = _store(client)
    chunks = [_chunk(), _chunk()]
    with pytest.raises(QdrantConfigError):
        store.upsert(chunks, [[1, 0, 0], [1, 0]], dense_index_version=DENSE_VERSION)
    with pytest.raises(QdrantConfigError):
        store.upsert(chunks, [[1, 0, 0]], dense_index_version=DENSE_VERSION)
    client.upsert.assert_not_called()


def test_unconfirmed_upsert_cannot_be_reported_successful() -> None:
    client = Mock()
    client.upsert.return_value = models.UpdateResult(status=models.UpdateStatus.ACKNOWLEDGED)
    with pytest.raises(QdrantResponseError):
        _store(client).upsert([_chunk()], [[1, 0, 0]], dense_index_version=DENSE_VERSION)
    assert client.upsert.call_args.kwargs["wait"] is True


@pytest.mark.parametrize("invalid", ["point_id", "document_id", "dense_version", "metadata", "score_nan", "score_bool", "duplicate"])
def test_corrupted_or_out_of_scope_query_results_are_rejected(invalid: str) -> None:
    chunk = _chunk()
    payload = _payload(chunk)
    point = SimpleNamespace(id=str(chunk.metadata.chunk_id), payload=payload, score=0.9)
    if invalid == "point_id":
        point.id = str(uuid4())
    elif invalid == "document_id":
        payload["metadata"]["document_id"] = str(uuid4())
    elif invalid == "dense_version":
        payload["dense_index_version"] = "dense-other"
    elif invalid == "metadata":
        payload["metadata"]["page"] = -1
    elif invalid == "score_nan":
        point.score = float("nan")
    elif invalid == "score_bool":
        point.score = True
    client = Mock()
    client.query_points.return_value = SimpleNamespace(points=[point, point] if invalid == "duplicate" else [point])
    with pytest.raises(QdrantResponseError):
        _store(client).query([1, 0, 0], document_ids=[chunk.metadata.document_id], dense_index_version=DENSE_VERSION)


@pytest.mark.parametrize("method", ["ensure", "upsert", "query"])
def test_provider_body_and_connection_detail_do_not_escape_errors(method: str) -> None:
    client = Mock()
    error = UnexpectedResponse(503, "Unavailable", PRIVATE_RESPONSE.encode(), httpx.Headers())
    client.collection_exists.side_effect = error
    client.upsert.side_effect = error
    client.query_points.side_effect = error
    chunk = _chunk()
    store = _store(client)
    with pytest.raises(QdrantRequestError) as caught:
        if method == "ensure":
            store.ensure_collection()
        elif method == "upsert":
            store.upsert([chunk], [[1, 0, 0]], dense_index_version=DENSE_VERSION)
        else:
            store.query([1, 0, 0], document_ids=[chunk.metadata.document_id], dense_index_version=DENSE_VERSION)
    rendered = "".join(traceback.format_exception(caught.value))
    assert PRIVATE_RESPONSE not in rendered
    assert caught.value.__suppress_context__


def test_context_manager_closes_client_on_error() -> None:
    client = Mock()
    with pytest.raises(RuntimeError):
        with _store(client):
            raise RuntimeError("test")
    client.close.assert_called_once_with()


@pytest.mark.parametrize("url", ["http://user:pass@localhost:6433", "http://localhost:6433?q=secret", "http://localhost:6433/#secret"])
def test_configuration_rejects_credential_query_and_fragment_urls(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, qdrant_url=url)
    assert url not in str(caught.value)


def test_configuration_defaults_are_isolated_and_limits_are_checked() -> None:
    settings = Settings(_env_file=None)
    assert str(settings.qdrant_url).rstrip("/") == "http://127.0.0.1:6433"
    assert settings.qdrant_collection_name == "pc_knowledge_dense_v0"
    assert settings.manifest_dir == PROJECT_ROOT / "data" / "manifests"
    assert settings.rag_top_k == 5
    for overrides in ({"qdrant_collection_name": "old_shared_collection"}, {"rag_top_k": 0}, {"qdrant_timeout_seconds": float("inf")}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **overrides)


def test_relative_manifest_path_uses_project_root_and_reference_project_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert Settings(_env_file=None, manifest_dir="data/custom").manifest_dir == PROJECT_ROOT / "data" / "custom"
    reference = PROJECT_ROOT.parent / "conversational-agent-langchain" / "data"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, manifest_dir=reference)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, manifest_dir="../conversational-agent-langchain/data")
