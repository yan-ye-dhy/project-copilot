"""D13命令行：真实本地快照与内存Qdrant，不请求外部服务或模型。"""

import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
from qdrant_client import QdrantClient, models

from project_copilot.rag.bm25 import BM25Index
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bm25_index.py"


@pytest.fixture
def cli():
    return runpy.run_path(str(SCRIPT))["main"]


@pytest.fixture
def corpus(tmp_path: Path):
    chunks = []
    documents = []
    for name, text in (
        ("qdrant.txt", "Qdrant 向量数据库支持本地检索。"),
        ("startup.txt", "FastAPI 的 lifespan 在启动阶段读取配置。"),
        ("upload.txt", "上传文件不能为空，仅支持TXT、Markdown和PDF。"),
    ):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        document = load_document(path)
        document_chunks = split_document(document)
        chunks.extend(document_chunks)
        documents.append(CompletedDocument(
            document_id=document.document_id, content_hash=document.content_hash,
            source_name=document.source_name,
            chunk_ids=tuple(chunk.metadata.chunk_id for chunk in document_chunks),
        ))
    manifest = IndexManifest(
        spec=DenseIndexSpec(
            collection_name="pc_bm25_cli", embedding_model="offline-test-model",
            dimensions=2, splitter_version=SplitConfig().index_version,
        ),
        documents=tuple(documents),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    return manifest, chunks, manifest_path, tmp_path / "bm25.json"


def forbidden_client(**_kwargs):
    pytest.fail("此命令不应连接Qdrant或模型")


def test_tokenize_works_in_process_without_live_and_preserves_technical_terms() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "tokenize",
         "中文配置 PROJECT_COPILOT_ZHIPU_API_KEY embedding-3 /health --frozen HTTP 503"],
        cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "ok"
    assert result["model_calls"] == 0
    assert set(result["tokens"]) >= {
        "中文", "配置", "project_copilot_zhipu_api_key", "embedding-3", "/health",
        "--frozen", "http", "503",
    }
    assert result["tokenizer"]["hmm"] is False
    assert "Traceback" not in completed.stderr


@pytest.mark.parametrize("args", [("build",), ("search", "问题"), ("unknown",)])
def test_invalid_command_arguments_exit_two_without_resources(cli, monkeypatch, args) -> None:
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    with pytest.raises(SystemExit) as error:
        cli(args)
    assert error.value.code == 2


def test_build_reads_completed_chunks_saves_snapshot_and_closes_local_client(
    corpus, cli, monkeypatch, capsys,
) -> None:
    manifest, chunks, manifest_path, output = corpus
    client = QdrantClient(":memory:")
    client.create_collection(
        manifest.spec.collection_name,
        vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)},
    )
    client.upsert(manifest.spec.collection_name, points=[
        models.PointStruct(
            id=str(chunk.metadata.chunk_id), vector={"dense": [1.0, 0.0]},
            payload={"text": chunk.text, "metadata": chunk.metadata.model_dump(mode="json"),
                     "dense_index_version": manifest.spec.index_version},
        ) for chunk in chunks
    ])
    calls = []
    closed = []
    original_close = client.close

    def factory(**kwargs):
        calls.append(kwargs)
        return client

    def close():
        closed.append(True)
        original_close()

    monkeypatch.setattr(client, "close", close)
    monkeypatch.setitem(cli.__globals__, "QdrantClient", factory)
    assert cli(["build", "--manifest", str(manifest_path), "--output", str(output)]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert calls == [{"url": "http://127.0.0.1:6433", "check_compatibility": False,
                      "trust_env": False, "timeout": 10, "prefer_grpc": False}]
    assert closed == [True]
    assert result["mode"] == "local_bm25"
    assert result["model_calls"] == 0
    assert result["documents"] == result["chunks"] == 3
    assert result["collection"] == manifest.spec.collection_name
    assert result["dense_index_version"] == manifest.spec.index_version
    loaded = BM25Index.load(output, manifest)
    assert loaded.fingerprint == result["fingerprint"]
    assert loaded.snapshot.chunks == tuple(sorted(chunks, key=lambda chunk: str(chunk.metadata.chunk_id)))
    assert result["config"]["algorithm"] == "BM25L"


def test_search_real_snapshot_in_subprocess_needs_no_environment_or_service(corpus) -> None:
    manifest, chunks, manifest_path, output = corpus
    BM25Index.build(manifest, chunks).save(output)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "search", "Qdrant", "--manifest", str(manifest_path),
         "--index", str(output), "--top-k", "1"],
        cwd=ROOT, capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["query_tokens"] == ["qdrant"]
    assert result["hit_count"] == result["top_k"] == 1
    assert result["hits"][0]["text"] == chunks[0].text
    assert result["hits"][0]["metadata"] == chunks[0].metadata.model_dump(mode="json")
    assert result["model_calls"] == 0
    assert "不是Dense分数或答案置信度" in result["score_note"]


@pytest.mark.parametrize("query", ["！！！", "unknownxyz987"])
def test_search_empty_or_unmatched_query_returns_no_fabricated_hits(
    corpus, cli, monkeypatch, capsys, query,
) -> None:
    manifest, chunks, manifest_path, output = corpus
    BM25Index.build(manifest, chunks).save(output)
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    assert cli(["search", query, "--manifest", str(manifest_path), "--index", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hit_count"] == 0
    assert result["hits"] == []
    assert result["model_calls"] == 0


def test_build_existing_snapshot_is_rejected_before_qdrant_and_keeps_bytes(
    corpus, cli, monkeypatch, capsys,
) -> None:
    _manifest, _chunks, manifest_path, output = corpus
    output.write_bytes(b"existing snapshot must survive")
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    assert cli(["build", "--manifest", str(manifest_path), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["status"] == "error"
    assert output.read_bytes() == b"existing snapshot must survive"


@pytest.mark.parametrize("contents", [None, '{"secret":"should-never-leak"}', "not json"])
def test_missing_or_invalid_manifest_fails_before_resources_without_echoing_input(
    tmp_path, cli, monkeypatch, capsys, contents,
) -> None:
    manifest = tmp_path / "manifest.json"
    output = tmp_path / "output.json"
    if contents is not None:
        manifest.write_text(contents, encoding="utf-8")
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    assert cli(["build", "--manifest", str(manifest), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["model_calls"] == 0
    assert "should-never-leak" not in captured.err
    assert "Traceback" not in captured.err
    assert not output.exists()


def test_qdrant_failure_redacts_credentials_and_closes_client(corpus, cli, monkeypatch, capsys) -> None:
    _manifest, _chunks, manifest_path, output = corpus
    closed = []

    class FailedClient:
        def close(self):
            closed.append(True)

        def get_collection(self, **_kwargs):
            raise RuntimeError("http://user:private-token@example.invalid body=private-response")

    monkeypatch.setitem(cli.__globals__, "QdrantClient", lambda **_kwargs: FailedClient())
    assert cli(["build", "--manifest", str(manifest_path), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["status"] == "error"
    assert all(secret not in captured.err for secret in ("private-token", "private-response", "Traceback"))
    assert closed == [True]
    assert not output.exists()


def test_changed_manifest_cannot_search_old_snapshot(corpus, cli, monkeypatch, capsys) -> None:
    manifest, chunks, manifest_path, output = corpus
    BM25Index.build(manifest, chunks).save(output)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["spec"]["splitter_version"] = "different-splitter-version"
    manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    assert cli(["search", "Qdrant", "--manifest", str(manifest_path), "--index", str(output)]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"


def test_invalid_top_k_is_rejected_without_service(corpus, cli, monkeypatch, capsys) -> None:
    manifest, chunks, manifest_path, output = corpus
    BM25Index.build(manifest, chunks).save(output)
    monkeypatch.setitem(cli.__globals__, "QdrantClient", forbidden_client)
    assert cli(["search", "Qdrant", "--manifest", str(manifest_path), "--index", str(output),
                "--top-k", "0"]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == "error"
