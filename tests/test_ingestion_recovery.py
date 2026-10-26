"""D18恢复边界：内存Qdrant与合成二维向量，不调用模型服务。"""

import asyncio
import builtins
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import socket
import sys

import httpx
import pytest
from qdrant_client import QdrantClient, models

from project_copilot.api import documents as uploads
from project_copilot.config import Settings
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantRequestError, QdrantResponseError
from project_copilot.main import create_app
from project_copilot.rag.bm25 import BM25Index, read_completed_chunks
from project_copilot.rag.hybrid import HybridError, HybridRetriever
from project_copilot.rag.ingestion import IngestionService, ManifestError, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import DenseIndexSpec


FIXTURES = Path(__file__).parent / "fixtures/documents"


@pytest.fixture(autouse=True)
def no_model_network_or_dotenv(monkeypatch):
    actual_open, actual_import = Path.open, builtins.__import__

    def safe_open(path, *args, **kwargs):
        assert path.name != ".env", "恢复测试不读取真实密钥"
        return actual_open(path, *args, **kwargs)

    def safe_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"torch", "transformers"}, "恢复测试不加载模型"
        return actual_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("离线恢复测试不访问网络")

    monkeypatch.setattr(Path, "open", safe_open)
    monkeypatch.setattr(builtins, "__import__", safe_import)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


class OfflineEmbeddings:
    model = "embedding-3"
    expected_dimensions = 2

    def __init__(self):
        self.documents, self.queries = [], []

    def embed_documents(self, texts):
        self.documents.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]


@pytest.fixture
def pipeline(tmp_path):
    embeddings = OfflineEmbeddings()
    split = SplitConfig(chunk_size=80, chunk_overlap=10)
    spec = DenseIndexSpec(collection_name="pc_d18_memory", embedding_model=embeddings.model,
                          dimensions=2, splitter_version=split.index_version)
    client = QdrantClient(":memory:")
    store = DenseQdrantStore(client, collection_name=spec.collection_name, dimensions=2)
    manifest = ManifestStore(tmp_path / "manifests", spec)
    state = SimpleNamespace(embeddings=embeddings, split=split, spec=spec, client=client,
                            store=store, manifest=manifest, root=tmp_path)
    yield state
    client.close()


def service(state):
    return IngestionService(state.embeddings, state.store, state.manifest, split_config=state.split)


def sample(state, filename="recovery.txt"):
    path = state.root / filename
    path.write_text("\n\n".join(f"恢复片段{i}：写入失败必须保留旧证据，只有完成后才能检索。" * 2 for i in range(7)))
    assert len(split_document(load_document(path), state.split)) > 2
    return path


def completed_views(state):
    manifest = state.manifest.load()
    dense = DenseRetriever(state.embeddings, state.store, state.manifest, top_k=100).search("恢复")
    bm25 = BM25Index.build(manifest, read_completed_chunks(state.client, manifest))
    return {h.metadata.document_id for h in dense}, {c.metadata.document_id for c in bm25.snapshot.chunks}


def test_retransmission_and_rename_skip_embedding_and_writes(pipeline, monkeypatch):
    state = pipeline
    original = service(state).ingest_file(FIXTURES / "sample.pdf")
    before = state.manifest.path.read_bytes()
    renamed = state.root / "renamed.pdf"
    renamed.write_bytes((FIXTURES / "sample.pdf").read_bytes())

    def forbidden(*args, **kwargs):
        pytest.fail("已完成的同内容重传不能再次Embedding或upsert")

    monkeypatch.setattr(state.embeddings, "embed_documents", forbidden)
    monkeypatch.setattr(state.store, "upsert", forbidden)
    assert service(state).ingest_file(renamed, source_name="另一个名称.pdf") == original
    assert state.manifest.path.read_bytes() == before
    assert state.client.count(state.spec.collection_name, exact=True).count == 2


def test_same_filename_different_bytes_remain_separate(pipeline):
    state = pipeline;path = state.root / "same.txt"
    path.write_text("第一份内容，独立文档。")
    first = service(state).ingest_file(path)
    path.write_text("第二份内容，不能按文件名覆盖第一份。")
    second = service(state).ingest_file(path)
    assert first.source_name == second.source_name == "same.txt"
    assert first.document_id != second.document_id and set(first.chunk_ids).isdisjoint(second.chunk_ids)
    assert state.manifest.load().documents == (first, second)
    assert completed_views(state) == ({first.document_id, second.document_id},) * 2


@pytest.mark.parametrize("failure", ["partial_write", "manifest_replace"])
def test_failure_then_fresh_service_retry_keeps_old_evidence_and_same_ids(pipeline, monkeypatch, failure):
    state = pipeline
    first = service(state).ingest_file(FIXTURES / "sample.txt")
    before = state.manifest.path.read_bytes()
    candidate = sample(state)
    chunks = split_document(load_document(candidate), state.split)
    calls_before = len(state.embeddings.documents)
    with monkeypatch.context() as fault:
        if failure == "partial_write":
            actual = state.store.upsert

            def partial(chunks, vectors, *, dense_index_version):
                actual(chunks[:1], vectors[:1], dense_index_version=dense_index_version)
                raise QdrantRequestError("合成部分写入失败")

            fault.setattr(state.store, "upsert", partial)
            expected = QdrantRequestError
        else:
            original_replace = Path.replace

            def fail_replace(path, target):
                if path.name.startswith(".manifest-"):
                    raise OSError("合成磁盘失败")
                return original_replace(path, target)

            fault.setattr(Path, "replace", fail_replace)
            expected = ManifestError
        with pytest.raises(expected):
            service(state).ingest_file(candidate)
    assert len(state.embeddings.documents) == calls_before + 1  # 无自动重试。
    assert state.manifest.path.read_bytes() == before
    assert completed_views(state) == ({first.document_id},) * 2
    assert not list(state.manifest.path.parent.glob(".manifest-*.tmp"))
    orphan_count = state.client.count(state.spec.collection_name, exact=True).count
    assert orphan_count == len(first.chunk_ids) + (1 if failure == "partial_write" else len(chunks))
    # 模拟新service/清单实例读取持久状态；不是原对象的内存成功标记。
    state.manifest = ManifestStore(state.manifest.path.parent, state.spec)
    retried = service(state).ingest_file(candidate)
    assert retried.chunk_ids == tuple(c.metadata.chunk_id for c in chunks)
    assert state.client.count(state.spec.collection_name, exact=True).count == len(first.chunk_ids) + len(chunks)
    assert completed_views(state) == ({first.document_id, retried.document_id},) * 2
    calls = len(state.embeddings.documents)
    assert service(state).ingest_file(candidate) == retried
    assert len(state.embeddings.documents) == calls


def test_failed_document_leaves_existing_bm25_valid_but_success_requires_explicit_rebuild(pipeline, monkeypatch):
    state = pipeline
    first = service(state).ingest_file(FIXTURES / "sample.txt")
    bm25 = BM25Index.build(state.manifest.load(), read_completed_chunks(state.client, state.manifest.load()))
    dense = DenseRetriever(state.embeddings, state.store, state.manifest, top_k=10)
    hybrid = HybridRetriever(dense, bm25, top_k=10)
    candidate = sample(state)
    with monkeypatch.context() as fault:
        def refuse_record(*args, **kwargs):
            raise ManifestError("保存失败")
        fault.setattr(state.manifest, "record", refuse_record)
        with pytest.raises(ManifestError):
            service(state).ingest_file(candidate)
    assert {h.metadata.document_id for h in hybrid.search("恢复")} == {first.document_id}
    retried = service(state).ingest_file(candidate)
    with pytest.raises(HybridError, match="快照不一致"):
        hybrid.search("恢复")
    rebuilt = BM25Index.build(state.manifest.load(), read_completed_chunks(state.client, state.manifest.load()))
    assert {c.metadata.document_id for c in rebuilt.snapshot.chunks} == {first.document_id, retried.document_id}


@pytest.mark.parametrize("change", ["missing", "duplicate", "unknown_id", "text", "source_name", "version", "bool_index", "not_list", "request"])
def test_readback_error_never_publishes_success_and_retry_repairs_same_ids(pipeline, monkeypatch, change):
    state = pipeline;candidate = sample(state)
    actual = state.client.retrieve

    def bad_read(*args, **kwargs):
        result = deepcopy(actual(*args, **kwargs))
        assert kwargs["with_vectors"] is False
        if change == "request": raise httpx.ConnectError("private-upstream")
        if change == "not_list": return None
        if change == "missing": return result[:-1]
        if change == "duplicate": return result + result[:1]
        if change == "unknown_id": result[0].id = str(uuid4())
        elif change == "text": result[0].payload["text"] += "changed"
        elif change == "source_name": result[0].payload["metadata"]["source_name"] = "wrong.txt"
        elif change == "version": result[0].payload["dense_index_version"] = "wrong"
        elif change == "bool_index": result[0].payload["metadata"]["chunk_index"] = False
        return result

    with monkeypatch.context() as fault:
        fault.setattr(state.client, "retrieve", bad_read)
        with pytest.raises((QdrantRequestError, QdrantResponseError)):
            service(state).ingest_file(candidate)
    assert state.manifest.load().documents == ()
    count = state.client.count(state.spec.collection_name, exact=True).count
    completed = service(state).ingest_file(candidate)
    assert state.client.count(state.spec.collection_name, exact=True).count == count == len(completed.chunk_ids)


def test_duplicate_with_missing_stored_chunk_fails_without_new_model_request(pipeline):
    state = pipeline
    completed = service(state).ingest_file(FIXTURES / "sample.pdf")
    state.client.delete(state.spec.collection_name, models.PointIdsList(points=[str(completed.chunk_ids[0])]), wait=True)
    calls = len(state.embeddings.documents)
    with pytest.raises(QdrantResponseError, match="不完整"):
        service(state).ingest_file(FIXTURES / "sample.pdf")
    assert len(state.embeddings.documents) == calls


def test_manifest_identity_conflict_is_not_silently_repaired(pipeline):
    state = pipeline
    completed = service(state).ingest_file(FIXTURES / "sample.pdf")
    broken = completed.model_copy(update={"chunk_ids": (uuid4(),)})
    state.manifest.record(state.manifest.load(), broken)
    with pytest.raises(ManifestError, match="身份"):
        service(state).ingest_file(FIXTURES / "sample.pdf")
    assert len(state.embeddings.documents) == 1


def test_large_document_readback_batches_exact_ids_without_vectors(pipeline, monkeypatch):
    state = pipeline;state.store.ensure_collection()
    document = load_document(sample(state))
    chunks = split_document(document, SplitConfig(chunk_size=4, chunk_overlap=0))
    assert len(chunks) > 64
    state.store.upsert(chunks, [[1.0, 0.0]] * len(chunks), dense_index_version=state.spec.index_version)
    actual, sizes = state.client.retrieve, []

    def record(*args, **kwargs):
        sizes.append(len(kwargs["ids"]))
        assert kwargs["with_vectors"] is False
        return actual(*args, **kwargs)

    monkeypatch.setattr(state.client, "retrieve", record)
    state.store.verify_chunks(chunks, dense_index_version=state.spec.index_version)
    assert sum(sizes) == len(chunks) and max(sizes) == 64


def test_original_frozen_collector_still_refuses_changed_live_sources():
    from evals.rerank_run import prepare_experiment
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match="源码及依赖已变化"):
        prepare_experiment(root / "evals/datasets/copilot_v0", root / "evals/results/hybrid-v2-20261022-01",
                           root / "evals/configs/rerank_v3.json")


def test_recovery_script_whole_workflow_uses_only_injected_memory_client(tmp_path):
    from scripts.ingestion_recovery_smoke import run_recovery
    client = QdrantClient(":memory:")
    client.info = lambda: SimpleNamespace(version="in-memory-test")
    result = run_recovery(tmp_path / "script-result", client=client)
    assert result["status"] == "ok" and result["transport"] == "injected-client"
    assert result["model_api_calls"] == 0 and result["test_collection_deleted"] is True
    assert [e["registered_documents"] for e in result["events"]] == [1, 1, 2, 2, 3, 3, 4, 4]
    assert result["synthetic_embedding_batches"] == 6 and result["stale_bm25_rejected"] is True
    assert result["events"][-1]["points"] == result["events"][-2]["points"]


def test_recovery_script_gate_precedes_output_or_clients(tmp_path, monkeypatch, capsys):
    from scripts import ingestion_recovery_smoke as smoke
    output = tmp_path / "never-created"
    monkeypatch.setattr(sys, "argv", ["recovery", "--output-dir", str(output)])
    with pytest.raises(SystemExit) as result:
        smoke.main()
    assert result.value.code == 2 and "--local" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("failure,status", [("partial", 502), ("manifest", 503)])
def test_upload_failure_retry_and_duplicate_preserve_temporary_cleanup(pipeline, monkeypatch, failure, status):
    state = pipeline
    temporary = state.root / "uploads";temporary.mkdir()
    monkeypatch.setattr(uploads.tempfile, "tempdir", str(temporary))
    settings = Settings(_env_file=None, environment="test", zhipu_api_key=None, deepseek_api_key=None,
                        embedding_expected_dimensions=2, qdrant_collection_name=state.spec.collection_name,
                        manifest_dir=state.manifest.path.parent)

    @contextmanager
    def factory(active):
        assert active is settings
        yield service(state)

    monkeypatch.setattr(uploads, "create_ingestion_service", factory)
    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                first = await client.post("/documents/upload", files={"file": ("first.txt", b"first completed", "text/plain")})
                assert first.status_code == 200
                before = state.manifest.path.read_bytes()
                with monkeypatch.context() as fault:
                    if failure == "partial":
                        actual = state.store.upsert
                        def fail(chunks, vectors, *, dense_index_version):
                            actual(chunks[:1], vectors[:1], dense_index_version=dense_index_version)
                            raise QdrantRequestError("private-failure")
                        fault.setattr(state.store, "upsert", fail)
                    else:
                        def refuse_record(*args, **kwargs):
                            raise ManifestError("private-failure")
                        fault.setattr(state.manifest, "record", refuse_record)
                    response = await client.post("/documents/upload", files={"file": ("recovery.pdf", (FIXTURES / "sample.pdf").read_bytes(), "application/pdf")})
                assert response.status_code == status and set(response.json()) == {"detail"}
                assert "private-" not in response.text and response.headers.get("x-run-id")
                assert state.manifest.path.read_bytes() == before
                assert not list(temporary.glob("project-copilot-upload-*"))
                retried = await client.post("/documents/upload", files={"file": ("recovery.pdf", (FIXTURES / "sample.pdf").read_bytes(), "application/pdf")})
                assert retried.status_code == 200
                count, calls = state.client.count(state.spec.collection_name, exact=True).count, len(state.embeddings.documents)
                duplicate = await client.post("/documents/upload", files={"file": ("renamed.pdf", (FIXTURES / "sample.pdf").read_bytes(), "application/pdf")})
                assert duplicate.status_code == 200 and duplicate.json()["document"] == retried.json()["document"]
                assert len(state.embeddings.documents) == calls
                assert state.client.count(state.spec.collection_name, exact=True).count == count
                assert not list(temporary.glob("project-copilot-upload-*"))
                assert not list(state.manifest.path.parent.glob(".manifest-*.tmp"))
    asyncio.run(run())
