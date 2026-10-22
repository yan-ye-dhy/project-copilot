"""D14三路实验编排：临时合成向量、官方内存Qdrant，不代表真实效果。"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from shutil import copyfile, copytree
import socket

import pytest
from qdrant_client import QdrantClient

from evals import hybrid_run
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantRequestError
from project_copilot.rag import answering


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_RUN = "dense-v0-20261019-01"
BRANCHES = ("dense", "bm25", "hybrid")


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


def reseal(case: dict) -> None:
    """只封存临时输入；让异常用例检验内容合同而非仅检验文件哈希。"""
    private, public = case["private"], case["public"]
    receipt = read_json(private / "manifest.json")
    receipt["files"] = {
        str(path.relative_to(private)): digest(path)
        for path in sorted(private.rglob("*"))
        if path.is_file() and path.name not in ("manifest.json", "review.jsonl")
    }
    write_json(private / "manifest.json", receipt)
    copyfile(private / "manifest.json", public / "run_manifest.json")
    archive = read_json(public / "archive_manifest.json")
    archive["source_manifest_sha256"] = digest(private / "manifest.json")
    archive["files"] = {name: digest(public / name) for name in archive["files"]}
    archive["excluded_large_files"] = {
        name: digest(private / name) for name in archive["excluded_large_files"]
    }
    write_json(public / "archive_manifest.json", archive)


@pytest.fixture(autouse=True)
def no_network_or_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "离线D14测试不得读取真实.env"
        return original_open(path, *args, **kwargs)

    def no_connect(*args, **kwargs):
        raise AssertionError("离线D14测试不得请求外部服务")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture
def case(tmp_path: Path) -> dict:
    dataset = tmp_path / "dataset"
    copytree(PROJECT_ROOT / "evals/datasets/copilot_v0", dataset)
    public = tmp_path / "public" / BASELINE_RUN
    copytree(PROJECT_ROOT / "evals/results" / BASELINE_RUN, public)
    private = tmp_path / "private" / BASELINE_RUN
    private.mkdir(parents=True)
    receipt = read_json(public / "run_manifest.json")
    for name in receipt["files"]:
        if (public / name).is_file():
            target = private / name
            target.parent.mkdir(parents=True, exist_ok=True)
            copyfile(public / name, target)
    target = private / "index_manifests/pc_eval_dense_v0.json"
    target.parent.mkdir(parents=True)
    copyfile(public / "index_manifest.json", target)
    baseline = read_json(public / "baseline_config.json")
    by_id = {row["question_id"]: row for row in read_rows(dataset / "questions.jsonl")}
    dev = read_json(dataset / "split_manifest.json")["dev"]
    vectors = [{
        "question_id": question_id,
        "query": by_id[question_id]["standalone_question"] or by_id[question_id]["question"],
        # 各题向量不同，才能发现静默重排、错配或重新生成问题向量。
        "vector": [1.0, index / 100, *([0.0] * 2046)],
        "embedding_contract": baseline["embedding"],
    } for index, question_id in enumerate(dev)]
    write_rows(private / "query_vectors.jsonl", vectors)
    write_json(private / "manifest.json", receipt)
    config = tmp_path / "hybrid.json"
    write_json(config, {
        "schema_version": 1,
        "experiment_id": "hybrid-v2-rrf60",
        "baseline_configuration_fingerprint": baseline["configuration_fingerprint"],
        "dataset_fingerprint": baseline["dataset_fingerprint"],
        "dense_candidates": 10,
        "bm25_candidates": 10,
        "ranking_depth": 10,
        "answer_top_k": 5,
        "rank_constant": 60,
    })
    result = {
        "dataset": dataset, "public": public, "private": private,
        "config": config, "output": tmp_path / "hybrid-v2-run", "clients": [],
    }
    reseal(result)
    yield result
    for client in result["clients"]:
        client.close()


def paths(case: dict) -> tuple[Path, ...]:
    return case["dataset"], case["public"], case["private"], case["config"]


class RecordingClient(QdrantClient):
    def __init__(self) -> None:
        super().__init__(":memory:")
        self.query_calls: list[dict] = []
        self.fail_at: set[int] = set()

    def query_points(self, *args, **kwargs):
        self.query_calls.append(deepcopy(kwargs))
        if len(self.query_calls) in self.fail_at:
            raise QdrantRequestError("合成Dense检索失败，没有外部请求")
        return super().query_points(*args, **kwargs)


def make_client(case: dict) -> RecordingClient:
    inputs = hybrid_run.prepare_experiment(*paths(case))
    client = RecordingClient()
    case["clients"].append(client)
    spec = inputs.manifest.spec
    store = DenseQdrantStore(client, collection_name=spec.collection_name,
                             dimensions=spec.dimensions, vector_name=spec.vector_name)
    store.ensure_collection()
    chunks = [item.chunk for item in inputs.catalog.values()]
    store.upsert(chunks, [[1.0, *([0.0] * 2047)] for _ in chunks], dense_index_version=spec.index_version)
    return client


def collect(case: dict, client: QdrantClient | None = None) -> dict:
    return hybrid_run.collect_experiment(*paths(case), case["output"], client=client or make_client(case))


def test_plan_reuses_complete_original_vectors_without_clients_or_output(case: dict) -> None:
    inputs = hybrid_run.prepare_experiment(*paths(case))
    assert len(inputs.catalog) == 16
    assert len(inputs.manifest.documents) == 5
    assert len(inputs.questions) == len(inputs.vectors) == 30
    original = {row["question_id"]: row["vector"] for row in read_rows(case["private"] / "query_vectors.jsonl")}
    assert inputs.vectors == original
    plan = hybrid_run.plan_experiment(*paths(case))
    assert plan["mode"] == "offline_plan"
    assert plan["model_calls"] == 0
    assert not case["output"].exists()


def test_collect_keeps_three_complete_dev_branches_and_original_vectors(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(case)
    inputs = hybrid_run.prepare_experiment(*paths(case))
    spec = inputs.manifest.spec
    before = client.retrieve(spec.collection_name, ids=list(inputs.catalog), with_payload=True, with_vectors=True)

    def forbidden_write(*args, **kwargs):
        raise AssertionError("D14采集必须只读已有Qdrant集合")

    for name in ("upsert", "delete", "create_collection", "delete_collection", "set_payload", "overwrite_payload"):
        monkeypatch.setattr(client, name, forbidden_write)
    result = collect(case, client)
    assert result["status"] == "ok"
    assert result["model_calls"] == {"embedding": 0, "query_embedding": 0, "chat": 0}
    assert result["qdrant_writes"] == 0
    assert result["complete"] is True
    assert result["retrieval_errors"] == dict.fromkeys(BRANCHES, 0)
    assert len(client.query_calls) == 30
    expected = [row["vector"] for row in read_rows(case["private"] / "query_vectors.jsonl")]
    assert [call["query"] for call in client.query_calls] == expected
    allowed = {str(doc.document_id) for doc in inputs.manifest.documents}
    for call in client.query_calls:
        assert call["collection_name"] == spec.collection_name
        assert call["limit"] == 10
        conditions = {condition.key: condition.match for condition in call["query_filter"].must}
        assert set(conditions["metadata.document_id"].any) == allowed
        assert conditions["dense_index_version"].value == spec.index_version
    after = client.retrieve(spec.collection_name, ids=list(inputs.catalog), with_payload=True, with_vectors=True)
    assert before == after
    rankings = [row for path in case["output"].glob("*_rankings.jsonl") for row in read_rows(path)]
    assert len(rankings) == 90
    assert {row["question_id"] for row in rankings} == {q["question_id"] for q in inputs.questions}
    for row in rankings:
        assert row["error"] is None
        assert len(row["hits"]) <= 10
        assert len({hit["metadata"]["chunk_id"] for hit in row["hits"]}) == len(row["hits"])
        for hit in row["hits"]:
            canonical = inputs.catalog[hit["metadata"]["chunk_id"]].chunk
            assert (hit["text"], hit["metadata"]) == (canonical.text, canonical.metadata.model_dump(mode="json"))
    summaries = [read_json(path) for path in case["output"].rglob("summary.json")]
    assert len(summaries) == 3
    for summary in summaries:
        assert summary["question_count"] == 30
        assert summary["answerable_count"] == 26
        assert summary["unanswerable_count"] == 4
        assert summary["retrieval_error_count"] == 0
        assert summary["unanswerable"]["refusal_accuracy"] is None
        assert list(summary["at_k"]) == ["1", "3", "5", "10"]
    config = read_json(case["output"] / "experiment_config.json")
    assert config["descriptor"]["answer_top_k"] == 5
    sources = read_rows(case["output"] / "candidate_sources.jsonl")
    assert [row["order"] for row in sources] == [
        ["dense", "bm25"] if index % 2 == 0 else ["bm25", "dense"] for index in range(30)
    ]
    for source in sources:
        assert len(source["dense"]) <= 10
        assert len(source["bm25"]) <= 10
        assert source["candidate_union"] <= 20
        for hit in source["fused"]:
            expected = sum(1 / (60 + hit[key]) for key in ("dense_rank", "bm25_rank") if hit[key] is not None)
            assert hit["score"] == pytest.approx(expected)
    assert hybrid_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


def test_existing_output_cannot_be_overwritten(case: dict) -> None:
    client = make_client(case)
    case["output"].mkdir()
    marker = case["output"] / "keep.txt"
    marker.write_text("已有结果", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        collect(case, client)
    assert marker.read_text(encoding="utf-8") == "已有结果"
    assert client.query_calls == []


@pytest.mark.parametrize("field,value", [
    ("rank_constant", True), ("rank_constant", 0), ("dense_candidates", 9),
    ("bm25_candidates", 11), ("ranking_depth", 5), ("answer_top_k", 10),
    ("dataset_fingerprint", "wrong"), ("baseline_configuration_fingerprint", "wrong"),
])
def test_config_drift_or_invalid_budget_is_refused_before_queries(case: dict, field: str, value: object) -> None:
    config = read_json(case["config"])
    config[field] = value
    write_json(case["config"], config)
    with pytest.raises((ValueError, RuntimeError)):
        hybrid_run.prepare_experiment(*paths(case))
    assert not case["output"].exists()


@pytest.mark.parametrize("kind", ["missing", "duplicate", "holdout", "query", "dimension", "bool", "contract"])
def test_resealed_query_vectors_still_require_full_dev_identity(case: dict, kind: str) -> None:
    rows = read_rows(case["private"] / "query_vectors.jsonl")
    if kind == "missing":
        rows.pop()
    elif kind == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif kind == "holdout":
        rows[0]["question_id"] = read_json(case["dataset"] / "split_manifest.json")["holdout"][0]
    elif kind == "query":
        rows[0]["query"] += "改写"
    elif kind == "dimension":
        rows[0]["vector"].pop()
    elif kind == "bool":
        rows[0]["vector"][0] = True
    else:
        rows[0]["embedding_contract"]["model"] = "another-model"
    write_rows(case["private"] / "query_vectors.jsonl", rows)
    reseal(case)
    with pytest.raises((ValueError, RuntimeError)):
        hybrid_run.prepare_experiment(*paths(case))
    assert not case["output"].exists()


def test_unsealed_private_vectors_are_rejected(case: dict) -> None:
    path = case["private"] / "query_vectors.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        hybrid_run.prepare_experiment(*paths(case))


def test_changed_public_archive_bytes_are_rejected(case: dict) -> None:
    path = case["public"] / "rankings.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        hybrid_run.prepare_experiment(*paths(case))


@pytest.mark.parametrize("problem", ["missing_collection", "missing_chunk", "wrong_payload"])
def test_canonical_collection_must_match_all_sixteen_completed_chunks(case: dict, problem: str) -> None:
    if problem == "missing_collection":
        client = RecordingClient()
        case["clients"].append(client)
    else:
        client = make_client(case)
        inputs = hybrid_run.prepare_experiment(*paths(case))
        chunk_id = next(iter(inputs.catalog))
        if problem == "missing_chunk":
            client.delete(inputs.manifest.spec.collection_name, points_selector=[chunk_id])
        else:
            client.set_payload(inputs.manifest.spec.collection_name, {"dense_index_version": "changed"}, points=[chunk_id])
    with pytest.raises((ValueError, RuntimeError)):
        collect(case, client)
    assert client.query_calls == []
    assert not case["output"].exists()


def test_dense_failure_after_smoke_keeps_full_failure_denominator(case: dict) -> None:
    client = make_client(case)
    client.fail_at = {3}
    collect(case, client)
    summaries = [read_json(path) for path in case["output"].rglob("summary.json")]
    assert len(summaries) == 3
    for summary in summaries:
        assert summary["question_count"] == 30
        assert summary["answerable_count"] == 26
        assert summary["unanswerable_count"] == 4
    errors = [row for path in case["output"].rglob("retrieval.jsonl") for row in read_rows(path)
              if row["error"] is not None]
    # Hybrid依赖两路，Dense异常不能伪装为空路并算作正常融合；BM25独立成功。
    assert len(errors) == 2
    assert len(client.query_calls) == 30
    for row in errors:
        assert row["status"] == "retrieval_error"
        assert row["hits"] == []
        assert set(row["metrics"]["at_k"]["10"].values()) <= {0.0, False}


def test_bm25_failure_after_smoke_is_not_a_successful_empty_route(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(case)
    original = hybrid_run.BM25Index.search
    calls = 0

    def fail_third_query(index, query, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ValueError("合成BM25失败，不调用外部服务")
        return original(index, query, **kwargs)

    monkeypatch.setattr(hybrid_run.BM25Index, "search", fail_third_query)
    result = collect(case, client)
    assert result["retrieval_errors"] == {"dense": 0, "bm25": 1, "hybrid": 1}
    assert result["fusion_calls"] == 29
    assert calls == len(client.query_calls) == 30
    for branch in BRANCHES:
        summary = read_json(case["output"] / f"{branch}_retrieval/summary.json")
        assert summary["question_count"] == 30
        assert summary["answerable_count"] == 26
        assert summary["unanswerable_count"] == 4
    for branch in ("bm25", "hybrid"):
        rows = read_rows(case["output"] / f"{branch}_retrieval/retrieval.jsonl")
        failed = [row for row in rows if row["error"] is not None]
        assert len(failed) == 1
        assert set(failed[0]["metrics"]["at_k"]["5"].values()) <= {0.0, False}


def test_smoke_failure_stops_later_queries_and_does_not_seal_partial_run(case: dict) -> None:
    client = make_client(case)
    client.fail_at = {1}
    with pytest.raises((ValueError, RuntimeError), match="smoke"):
        collect(case, client)
    assert len(client.query_calls) <= 2
    assert not (case["output"] / "manifest.json").exists()
    status = read_json(case["output"] / "status.json")
    assert status["state"] == "failed"
    assert status["complete"] is False


@pytest.mark.parametrize("change", ["append", "missing", "extra"])
def test_verify_refuses_tampered_missing_or_unsealed_files(case: dict, change: str) -> None:
    collect(case)
    path = case["output"] / "dense_rankings.jsonl"
    if change == "append":
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    elif change == "missing":
        path.unlink()
        receipt = read_json(case["output"] / "manifest.json")
        receipt["files"].pop("dense_rankings.jsonl")
        write_json(case["output"] / "manifest.json", receipt)
    else:
        (case["output"] / "unsealed.txt").write_text("未封存", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        hybrid_run.verify_experiment(case["dataset"], case["output"])


def test_verifying_saved_history_does_not_rebuild_current_bm25_runtime(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect(case)

    def forbidden(*args, **kwargs):
        raise AssertionError("历史归档校验不应重新加载当前分词或BM25依赖")

    monkeypatch.setattr(hybrid_run.BM25Index, "load", forbidden)
    monkeypatch.setattr(hybrid_run, "ChineseTokenizer", forbidden)
    assert hybrid_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


def test_future_prompt_change_does_not_invalidate_archived_experiment(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect(case)
    archived = read_json(case["output"] / "experiment_config.json")
    original_prompt = archived["baseline"]["prompt"]["system_message"]
    monkeypatch.setattr(answering, "SYSTEM_MESSAGE", original_prompt + "\n后续版本的提示词变更。")
    # 旧运行由保存的Prompt及其指纹描述，校验不得用当前Prompt重新定义历史合同。
    assert answering.SYSTEM_MESSAGE != original_prompt
    assert hybrid_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"
    assert read_json(case["output"] / "experiment_config.json")["baseline"]["prompt"]["system_message"] == original_prompt
