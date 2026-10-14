"""检索脚本的显式收费开关、JSON交接及资源释放；全程假Embedding。"""

import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from project_copilot import config
from project_copilot.infrastructure import models, qdrant
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import DenseIndexSpec


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dense_index.py"
FIXTURE = ROOT / "tests" / "fixtures" / "documents" / "sample.pdf"


@pytest.mark.parametrize("args", [("--help",), ("ingest", "missing.txt"), ("search", "问题")])
def test_cli_help_or_missing_live_never_loads_settings_or_constructs_clients(monkeypatch, args) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("--help或未确认--live时不能读取密钥/构造外部客户端")
    monkeypatch.setattr(config, "load_settings", forbidden)
    monkeypatch.setattr(models, "create_embedding_client", forbidden)
    monkeypatch.setattr(qdrant, "create_qdrant_store", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    main = runpy.run_path(str(SCRIPT))["main"]
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == (0 if args == ("--help",) else 2)


def test_missing_live_has_real_process_exit_two() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "search", "离线开关测试"],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=15,
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "--live" in completed.stderr


@pytest.fixture
def cli_resources(tmp_path: Path, monkeypatch):
    class FakeEmbeddings:
        model = "offline-test-model"
        expected_dimensions = 2
        closed = False

        def embed_documents(self, texts):
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, _text):
            return [1.0, 0.0]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

    embeddings = FakeEmbeddings()
    store = qdrant.DenseQdrantStore(
        QdrantClient(":memory:"), collection_name="pc_cli_test", dimensions=2,
    )
    closed = []
    original_close = store.close
    def close_store():
        closed.append(True)
        original_close()
    monkeypatch.setattr(store, "close", close_store)
    settings = SimpleNamespace(
        qdrant_collection_name=store.collection_name, embedding_model=embeddings.model,
        embedding_expected_dimensions=2, manifest_dir=tmp_path / "manifests", rag_top_k=5,
    )
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(models, "create_embedding_client", lambda _settings: embeddings)
    monkeypatch.setattr(qdrant, "create_qdrant_store", lambda _settings: store)
    spec = DenseIndexSpec(
        collection_name=store.collection_name, embedding_model=embeddings.model,
        dimensions=2, splitter_version=SplitConfig().index_version,
    )
    yield embeddings, store, ManifestStore(settings.manifest_dir, spec), closed
    if not closed:
        original_close()


@pytest.mark.parametrize("command", ["ingest", "search"])
def test_live_cli_emits_dense_evidence_and_closes_both_resources(cli_resources, monkeypatch, capsys, command) -> None:
    embeddings, store, manifest, closed = cli_resources
    if command == "search":
        IngestionService(embeddings, store, manifest).ingest_file(FIXTURE)
    argument = str(FIXTURE) if command == "ingest" else "第三页来源"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), command, argument, "--live"])
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["status"] == "ok"
    assert result["mode"] == "DENSE"
    assert result["collection"] == manifest.spec.collection_name
    assert result["index_version"] == manifest.spec.index_version
    expected = split_document(load_document(FIXTURE))
    if command == "ingest":
        assert result["chunk_count"] == 2
        assert result["document"]["chunk_ids"] == [str(chunk.metadata.chunk_id) for chunk in expected]
    else:
        assert result["hit_count"] == 2
        assert {hit["metadata"]["page"] for hit in result["hits"]} == {0, 2}
        assert {hit["metadata"]["chunk_id"] for hit in result["hits"]} == {
            str(chunk.metadata.chunk_id) for chunk in expected
        }
    assert embeddings.closed is True
    assert closed == [True]


def test_cli_service_failure_is_safe_json_and_releases_resources(cli_resources, monkeypatch, capsys) -> None:
    embeddings, store, manifest, closed = cli_resources
    def reject_collection():
        raise qdrant.QdrantRequestError("无法读取或创建Qdrant Dense集合")
    monkeypatch.setattr(store, "ensure_collection", reject_collection)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "ingest", str(FIXTURE), "--live"])
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "error", "message": "无法读取或创建Qdrant Dense集合"}
    assert "Traceback" not in captured.err
    assert not manifest.path.exists()
    assert embeddings.closed is True
    assert closed == [True]
