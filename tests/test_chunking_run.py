"""D12真实编排的离线验收；合成向量和内存Qdrant不代表真实检索效果。"""

from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile, copytree
import socket
import sys

import pytest
from qdrant_client import QdrantClient

from evals import chunking_run
from project_copilot.config import Settings
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantRequestError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "dense-v0-20261019-01"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def reseal(case) -> None:
    """为合成私有输入重建两层清单；故意畸形的向量仍须被内容校验拒绝。"""
    private, public = case["private"], case["public"]
    manifest = read_json(private / "manifest.json")
    manifest["files"] = {
        str(path.relative_to(private)): digest(path)
        for path in sorted(private.rglob("*"))
        if path.is_file() and path.name not in ("manifest.json", "review.jsonl")
    }
    write_json(private / "manifest.json", manifest)
    copyfile(private / "manifest.json", public / "run_manifest.json")
    archive = read_json(public / "archive_manifest.json")
    archive["source_manifest_sha256"] = digest(private / "manifest.json")
    archive["files"] = {name: digest(public / name) for name in archive["files"]}
    archive["excluded_large_files"] = {
        name: digest(private / name) for name in archive["excluded_large_files"]
    }
    write_json(public / "archive_manifest.json", archive)
    candidate = read_json(case["candidate"])
    candidate["baseline_archive_sha256"] = digest(public / "archive_manifest.json")
    write_json(case["candidate"], candidate)


class FakeEmbeddings:
    model = "embedding-3"
    expected_dimensions = 2048
    batch_size = 64

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_documents(self, texts) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[1.0, *([0.0] * 2047)] for _ in texts]

    def embed_query(self, text):
        raise AssertionError("D12必须逐值复用D10问题向量，不能重新Embedding问题")


class RecordingMemoryStore(DenseQdrantStore):
    """复用官方SDK及项目适配器，仅将存储放在内存并记录调用。"""

    def __init__(self, spec) -> None:
        super().__init__(QdrantClient(":memory:"), collection_name=spec.collection_name,
                         dimensions=spec.dimensions, vector_name=spec.vector_name)
        self.query_calls = 0
        self.query_vectors = []
        self.upsert_calls = 0
        self.fail_queries: set[int] = set()

    def upsert(self, chunks, vectors, *, dense_index_version):
        self.upsert_calls += 1
        return super().upsert(chunks, vectors, dense_index_version=dense_index_version)

    def query(self, vector, **kwargs):
        self.query_calls += 1
        self.query_vectors.append(list(vector))
        if self.query_calls in self.fail_queries:
            raise QdrantRequestError("合成检索失败；无真实Qdrant请求")
        return super().query(vector, **kwargs)


@pytest.fixture(autouse=True)
def no_network_or_dotenv(monkeypatch):
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "离线D12测试不得读取真实.env"
        return original_open(path, *args, **kwargs)

    def no_connect(*args, **kwargs):
        raise AssertionError("离线D12测试不得请求外部服务")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture
def case(tmp_path, bind_current_sources):
    dataset = tmp_path / "dataset"
    copytree(PROJECT_ROOT / "evals/datasets/copilot_v0", dataset)
    public = tmp_path / "public" / RUN_ID
    copytree(PROJECT_ROOT / "evals/results" / RUN_ID, public)
    private = tmp_path / "private" / RUN_ID
    private.mkdir(parents=True)
    receipt = bind_current_sources(read_json(public / "run_manifest.json"))
    for name in receipt["files"]:
        if (public / name).is_file():
            target = private / name
            target.parent.mkdir(parents=True, exist_ok=True)
            copyfile(public / name, target)
    target = private / "index_manifests/pc_eval_dense_v0.json"
    target.parent.mkdir(parents=True)
    copyfile(public / "index_manifest.json", target)
    config = read_json(public / "baseline_config.json")
    questions = {row["question_id"]: row for row in read_rows(dataset / "questions.jsonl")}
    ids = read_json(dataset / "split_manifest.json")["dev"]
    rows = [{"question_id": qid,
             "query": questions[qid]["standalone_question"] or questions[qid]["question"],
             "vector": [1.0, *([0.0] * 2047)], "embedding_contract": config["embedding"]}
            for qid in ids]
    write_rows(private / "query_vectors.jsonl", rows)
    write_json(private / "manifest.json", receipt)
    candidate = tmp_path / "candidate.json"
    copyfile(PROJECT_ROOT / "evals/configs/chunk_size500.json", candidate)
    value = {
        "dataset": dataset, "public": public, "private": private, "candidate": candidate,
        "cache": tmp_path / "cache", "output": tmp_path / "chunk-size500-run",
        "settings": Settings(_env_file=None, _env_prefix="D12_OFFLINE_ONLY_",
                             zhipu_api_key=None, deepseek_api_key=None,
                             qdrant_collection_name="pc_eval_dense_v0"),
        "embeddings": FakeEmbeddings(), "stores": [],
    }
    reseal(value)
    yield value
    for store in value["stores"]:
        store.close()


def paths(case) -> tuple:
    return case["dataset"], case["public"], case["private"], case["candidate"], case["cache"]


def make_stores(case):
    inputs = chunking_run.prepare_experiment(*paths(case))
    baseline = RecordingMemoryStore(inputs.specs["baseline"])
    candidate = RecordingMemoryStore(inputs.specs["candidate"])
    case["stores"].extend((baseline, candidate))
    baseline.ensure_collection()
    chunks = [item.chunk for item in inputs.catalogs["baseline"].values()]
    baseline.upsert(chunks, [[1.0, *([0.0] * 2047)] for _ in chunks],
                    dense_index_version=inputs.specs["baseline"].index_version)
    baseline.upsert_calls = 0
    return baseline, candidate


def collect(case, stores=None):
    baseline, candidate = stores or make_stores(case)
    return chunking_run.collect_experiment(
        *paths(case), case["output"], case["settings"], embeddings=case["embeddings"],
        baseline_store=baseline, candidate_store=candidate,
    )


def test_plan_and_prepare_reuse_all_query_vectors_without_factories(case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("plan不得构造真实客户端或加载.env")

    for name in ("load_settings", "create_embedding_client", "create_qdrant_store"):
        monkeypatch.setattr(chunking_run, name, forbidden)
    inputs = chunking_run.prepare_experiment(*paths(case))
    assert {side: len(catalog) for side, catalog in inputs.catalogs.items()} == {"baseline": 16, "candidate": 27}
    assert len(inputs.questions) == len(inputs.vectors) == 30
    original = {row["question_id"]: row["vector"] for row in read_rows(case["private"] / "query_vectors.jsonl")}
    assert inputs.vectors == original
    result = chunking_run.plan_experiment(*paths(case))
    assert result["mode"] == "offline_plan"
    assert result["model_calls"] == 0
    assert (result["documents"], result["baseline_chunks"], result["candidate_chunks"]) == (5, 16, 27)
    assert result["reused_query_vectors"] == 30
    assert result["max_query_embedding_calls"] == result["max_chat_calls"] == 0
    assert (result["cache"]["missing_texts"], result["cache"]["embedding_requests"]) == (27, 1)
    assert result["qdrant_queries"] == 60
    assert not case["cache"].exists()
    assert not case["output"].exists()


def test_cold_collect_embeds_only_27_candidate_chunks_and_preserves_baseline(case):
    baseline, candidate = make_stores(case)
    result = collect(case, (baseline, candidate))
    assert result["status"] == "ok"
    assert [len(batch) for batch in case["embeddings"].calls] == [27]
    assert baseline.upsert_calls == 0
    assert candidate.upsert_calls > 0
    assert baseline.query_calls == candidate.query_calls == 30
    expected_vectors = [row["vector"] for row in read_rows(case["private"] / "query_vectors.jsonl")]
    assert baseline.query_vectors == candidate.query_vectors == expected_vectors
    assert result["model_calls"] == {"embedding": 1, "query_embedding": 0, "chat": 0}
    manifest = read_json(case["output"] / "manifest.json")
    assert manifest["query_vectors_sha256"] == digest(case["private"] / "query_vectors.jsonl")
    assert [row["order"] for row in manifest["query_order"]] == [
        ["baseline", "candidate"] if i % 2 == 0 else ["candidate", "baseline"] for i in range(30)
    ]
    assert baseline._client.count(baseline.collection_name, exact=True).count == 16
    assert candidate._client.count(candidate.collection_name, exact=True).count == 27
    assert chunking_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


def test_warm_new_run_reuses_document_cache_with_no_model_request(case):
    collect(case)
    case["embeddings"] = FakeEmbeddings()
    case["output"] = case["output"].with_name("warm-chunk-size500-run")
    collect(case)
    assert case["embeddings"].calls == []
    assert chunking_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


@pytest.mark.parametrize("kind", ("missing_question", "bool_vector", "wrong_query"))
def test_query_vector_content_is_checked_before_any_paid_work_even_when_resealed(case, kind):
    rows = read_rows(case["private"] / "query_vectors.jsonl")
    if kind == "missing_question":
        rows.pop()
    elif kind == "bool_vector":
        rows[0]["vector"][0] = True
    else:
        rows[0]["query"] += "被修改"
    write_rows(case["private"] / "query_vectors.jsonl", rows)
    reseal(case)
    with pytest.raises((ValueError, RuntimeError)):
        chunking_run.prepare_experiment(*paths(case))
    assert case["embeddings"].calls == []
    assert not case["output"].exists()


def test_private_query_vector_bytes_must_match_sealed_manifest(case):
    path = case["private"] / "query_vectors.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        chunking_run.prepare_experiment(*paths(case))


def test_d10_source_drift_is_refused_even_with_updated_archive_hash(case):
    manifest = read_json(case["private"] / "manifest.json")
    manifest["source_sha256"]["evals/metrics.py"] = "0" * 64
    write_json(case["private"] / "manifest.json", manifest)
    reseal(case)
    with pytest.raises((ValueError, RuntimeError)):
        chunking_run.prepare_experiment(*paths(case))
    assert not case["cache"].exists()


def test_existing_output_is_refused_without_overwriting_or_embedding(case):
    stores = make_stores(case)
    case["output"].mkdir()
    marker = case["output"] / "keep.txt"
    marker.write_text("保留原实验", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        collect(case, stores)
    assert marker.read_text(encoding="utf-8") == "保留原实验"
    assert case["embeddings"].calls == []
    assert stores[1].upsert_calls == 0


def test_preexisting_candidate_collection_is_refused_before_embedding(case):
    stores = make_stores(case)
    stores[1].ensure_collection()
    with pytest.raises((ValueError, RuntimeError)):
        collect(case, stores)
    assert case["embeddings"].calls == []
    assert stores[1].upsert_calls == 0


@pytest.mark.parametrize("problem", ("missing", "wrong_payload"))
def test_original_v0_collection_must_exist_and_match_sealed_chunks_before_embedding(case, problem):
    if problem == "missing":
        inputs = chunking_run.prepare_experiment(*paths(case))
        stores = (RecordingMemoryStore(inputs.specs["baseline"]),
                  RecordingMemoryStore(inputs.specs["candidate"]))
        case["stores"].extend(stores)
    else:
        stores = make_stores(case)
        first_id = next(iter(chunking_run.prepare_experiment(*paths(case)).catalogs["baseline"]))
        stores[0]._client.set_payload(stores[0].collection_name,
                                     {"dense_index_version": "tampered"}, points=[first_id])
    with pytest.raises((ValueError, RuntimeError)):
        collect(case, stores)
    assert case["embeddings"].calls == []
    assert stores[1].upsert_calls == 0
    assert not case["output"].exists()


def test_authorized_new_text_limit_is_checked_before_storage_or_embedding(case):
    baseline, candidate = make_stores(case)
    with pytest.raises((ValueError, RuntimeError), match="授权"):
        chunking_run.collect_experiment(
            *paths(case), case["output"], case["settings"], embeddings=case["embeddings"],
            baseline_store=baseline, candidate_store=candidate, new_text_limit=26,
        )
    assert case["embeddings"].calls == []
    assert not candidate._client.collection_exists(candidate.collection_name)
    assert not case["output"].exists()


def test_gold_mapping_failure_is_refused_before_embedding_or_candidate_collection_creation(case, monkeypatch):
    baseline, candidate = make_stores(case)
    monkeypatch.setattr(chunking_run, "_fully_covered", lambda *_: False)
    with pytest.raises((ValueError, RuntimeError), match="gold|原文"):
        chunking_run.prepare_experiment(*paths(case))
    with pytest.raises((ValueError, RuntimeError), match="gold|原文"):
        collect(case, (baseline, candidate))
    assert case["embeddings"].calls == []
    assert candidate.upsert_calls == baseline.upsert_calls == 0
    assert candidate.query_calls == baseline.query_calls == 0
    assert not candidate._client.collection_exists(candidate.collection_name)
    assert not case["output"].exists()
    assert not case["cache"].exists()


def test_smoke_query_failure_aborts_later_queries_and_keeps_partial_status(case):
    stores = make_stores(case)
    stores[1].fail_queries = {1}
    with pytest.raises((ValueError, RuntimeError), match="smoke|冒烟"):
        collect(case, stores)
    assert stores[1].query_calls <= 2
    assert stores[0].query_calls <= 2
    status = read_json(case["output"] / "status.json")
    assert status["state"] == "failed"
    assert status["complete"] is False
    assert not (case["output"] / "manifest.json").exists()
    assert [len(batch) for batch in case["embeddings"].calls] == [27]


def test_query_failure_after_smoke_keeps_all_30_questions_and_error_denominator(case):
    stores = make_stores(case)
    stores[1].fail_queries = {3}
    collect(case, stores)
    summaries = [read_json(path) for path in case["output"].rglob("summary.json")]
    summary = next(item for item in summaries if item.get("retrieval_error_count") == 1)
    assert summary["question_count"] == 30
    assert summary["answerable_count"] == 26
    scored = [row for path in case["output"].rglob("retrieval.jsonl") for row in read_rows(path)]
    failed = [row for row in scored if row.get("status") == "retrieval_error"]
    assert len(failed) == 1
    assert set(failed[0]["metrics"]["at_k"]["10"].values()) == {0.0, False}


def test_verify_refuses_tampered_experiment_file(case):
    collect(case)
    path = case["output"] / "candidate_rankings.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        chunking_run.verify_experiment(case["dataset"], case["output"])


def test_verify_requires_scoring_artifact_even_if_removed_from_manifest(case):
    collect(case)
    name = "candidate_retrieval/summary.json"
    (case["output"] / name).unlink()
    manifest = read_json(case["output"] / "manifest.json")
    manifest["files"].pop(name)
    write_json(case["output"] / "manifest.json", manifest)
    with pytest.raises((ValueError, RuntimeError), match="遗漏|未封存"):
        chunking_run.verify_experiment(case["dataset"], case["output"])


def test_collect_cli_requires_live_before_settings_or_factories(case, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("缺少--live时必须在配置加载前拒绝")

    monkeypatch.setattr(chunking_run, "load_settings", forbidden)
    monkeypatch.setattr(sys, "argv", [
        "chunking_run.py", "collect", "--dataset", str(case["dataset"]),
        "--baseline-dir", str(case["public"]), "--baseline-private-dir", str(case["private"]),
        "--candidate", str(case["candidate"]), "--cache-dir", str(case["cache"]),
        "--output-dir", str(case["output"]),
    ])
    with pytest.raises(SystemExit) as exc:
        chunking_run.main()
    assert exc.value.code == 2
    assert "--live" in capsys.readouterr().err
    assert not case["output"].exists()
