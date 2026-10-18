"""D9离线回放集成契约；排名由测试构造，不代表真实检索效果。"""

import json
from pathlib import Path
from shutil import copytree
import socket
import subprocess
import sys

import pytest

from evals.metrics import EvaluationError, build_chunk_catalog
from evals.run import run_evaluation
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SELECTED = ["pc-d7-001", "pc-d7-013", "pc-d7-017", "pc-d8-023"]
PHASES = ("embedding", "search", "fusion", "rerank", "total")


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8",
    )


@pytest.fixture
def replay(tmp_path):
    """只复制已冻结输入；测试快照固定命中、漏检及无答案情况。"""
    dataset = tmp_path / "dataset"
    copytree(PROJECT_ROOT / "evals/datasets/copilot_v0", dataset)
    sources = read_jsonl(dataset / "sources.jsonl")
    labels = {row["question_id"]: row for row in read_jsonl(dataset / "labels.draft.jsonl")}
    questions = {row["question_id"]: row for row in read_jsonl(dataset / "questions.jsonl")}
    catalog = build_chunk_catalog(
        [load_document(dataset / source["path"]) for source in sources], SplitConfig(),
    )

    def covering_hit(question_id, score):
        # 本夹具中001的单证据、013的两个证据各由一个片段完整覆盖。
        evidence = labels[question_id]["evidence"]
        located = next(
            item for item in catalog.values()
            if all(
                str(item.chunk.metadata.document_id) == unit["document_id"]
                and item.start_char <= unit["start_char"]
                and item.end_char >= unit["end_char"]
                for unit in evidence
            )
        )
        return {**located.chunk.model_dump(mode="json"), "score": score}

    distractor = next(
        item for item in catalog.values()
        if item.chunk.metadata.source_name == "compose_environment.md"
    )
    irrelevant = {**distractor.chunk.model_dump(mode="json"), "score": 0.9}
    null_times = dict.fromkeys(PHASES)
    rows = [
        {"question_id": SELECTED[0], "hits": [covering_hit(SELECTED[0], 0.1), irrelevant],
         "timings_ms": {**null_times, "embedding": 2.0, "search": 4.0, "total": 8.0}},
        {"question_id": SELECTED[1], "hits": [irrelevant, covering_hit(SELECTED[1], 0.8)],
         "timings_ms": {**null_times, "embedding": 4.0, "search": 6.0, "total": 12.0}},
        {"question_id": SELECTED[2], "hits": [irrelevant], "timings_ms": null_times.copy()},
        {"question_id": SELECTED[3], "hits": [],
         "timings_ms": {**null_times, "embedding": 6.0, "search": 8.0, "total": 16.0}},
    ]
    for row in rows:
        question = questions[row["question_id"]]
        row.update(query=question["standalone_question"] or question["question"], error=None)
    fingerprint = json.loads((dataset / "frozen_manifest.json").read_text())["fingerprint"]
    config = {
        "schema_version": 1, "dataset_fingerprint": fingerprint, "split": "dev",
        "question_ids": SELECTED.copy(), "ks": [1, 3, 5, 10],
        "splitter": SplitConfig().model_dump(mode="json"), "ranking_origin": "synthetic_fixture",
    }
    config_path, rankings_path = tmp_path / "config.json", tmp_path / "rankings.jsonl"
    write_json(config_path, config)
    write_jsonl(rankings_path, rows)
    return {
        "dataset": dataset, "config": config, "config_path": config_path,
        "rows": rows, "rankings_path": rankings_path, "output": tmp_path / "known-run",
    }


def evaluate(replay):
    return run_evaluation(
        replay["dataset"], replay["config_path"], replay["rankings_path"], replay["output"],
    )


def test_replay_writes_known_macro_metrics_raw_order_and_explicit_offline_identity(replay):
    summary = evaluate(replay)
    assert (summary["question_count"], summary["answerable_count"], summary["unanswerable_count"]) == (4, 3, 1)
    assert summary["retrieval_error_count"] == 0
    assert summary["mode"] == "offline_replay"
    assert summary["ranking_origin"] == "synthetic_fixture"
    assert summary["run_id"] == replay["output"].name
    assert summary["at_k"]["1"] == pytest.approx({
        "document_recall": 1 / 3, "document_mrr": 1 / 3,
        "evidence_recall": 1 / 3, "evidence_mrr": 1 / 3, "all_evidence_covered": 1 / 3,
    })
    assert summary["at_k"]["3"] == pytest.approx({
        "document_recall": 2 / 3, "document_mrr": 0.5,
        "evidence_recall": 2 / 3, "evidence_mrr": 0.5, "all_evidence_covered": 2 / 3,
    })
    assert {path.name for path in replay["output"].iterdir()} == {"config.json", "retrieval.jsonl", "summary.json"}
    assert json.loads((replay["output"] / "summary.json").read_text()) == summary
    rows = read_jsonl(replay["output"] / "retrieval.jsonl")
    first = next(row for row in rows if row["question_id"] == SELECTED[0])
    assert [hit["rank"] for hit in first["hits"]] == [1, 2]
    # 高分干扰片段仍在第2位，评测不得自行按score重排输入排名。
    assert [hit["score"] for hit in first["hits"]] == [0.1, 0.9]
    assert first["hits"][0]["metadata"] == replay["rows"][0]["hits"][0]["metadata"]


def test_retrieval_failure_counts_as_zero_and_unanswerable_is_separate(replay):
    replay["rows"][0].update(hits=[], error={"type": "timeout", "message": "synthetic timeout"})
    write_jsonl(replay["rankings_path"], replay["rows"])
    summary = evaluate(replay)
    assert (summary["answerable_count"], summary["retrieval_error_count"]) == (3, 1)
    assert summary["at_k"]["3"]["evidence_recall"] == pytest.approx(1 / 3)
    assert summary["at_k"]["3"]["evidence_mrr"] == pytest.approx(1 / 6)
    assert summary["unanswerable"] == {
        "count": 1, "empty_hits": 0, "nonempty_hits": 1, "refusal_accuracy": None,
        "retrieval_errors": 0,
    }
    rows = {row["question_id"]: row for row in read_jsonl(replay["output"] / "retrieval.jsonl")}
    assert rows[SELECTED[0]]["status"] == "retrieval_error"
    assert rows[SELECTED[0]]["error"]["type"] == "timeout"
    assert all(value is None for value in rows[SELECTED[2]]["metrics"]["at_k"]["3"].values())


def test_timings_use_recorded_samples_nearest_rank_p95_and_null_for_missing(replay):
    summary = evaluate(replay)
    assert summary["timings_ms"]["embedding"] == {"samples": 3, "median": 4.0, "p95": 6.0}
    assert summary["timings_ms"]["search"] == {"samples": 3, "median": 6.0, "p95": 8.0}
    assert summary["timings_ms"]["total"] == {"samples": 3, "median": 12.0, "p95": 16.0}
    for phase in ("fusion", "rerank"):
        assert summary["timings_ms"][phase] == {"samples": 0, "median": None, "p95": None}
    rows = read_jsonl(replay["output"] / "retrieval.jsonl")
    unknown = next(row for row in rows if row["question_id"] == SELECTED[2])
    assert unknown["timings_ms"] == dict.fromkeys(PHASES)
    assert unknown["evaluation_ms"] >= 0


@pytest.mark.parametrize("defect", ["holdout", "holdout-question", "fingerprint", "unknown-field", "ks-bool"])
def test_invalid_config_is_rejected_before_output_creation(replay, defect):
    if defect == "holdout":
        replay["config"]["split"] = "holdout"
    elif defect == "holdout-question":
        replay["config"]["question_ids"] = ["pc-d7-005"]
    elif defect == "fingerprint":
        replay["config"]["dataset_fingerprint"] = "dataset-v1-" + "0" * 64
    elif defect == "unknown-field":
        replay["config"]["silently_use_model"] = True
    else:
        replay["config"]["ks"] = [True, 3]
    write_json(replay["config_path"], replay["config"])
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()


def test_changed_frozen_gold_is_rejected_before_output_creation(replay):
    path = replay["dataset"] / "labels.draft.jsonl"
    rows = read_jsonl(path)
    rows[0]["reference_answer"] += "（临时副本变化）"
    write_jsonl(path, rows)
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()


@pytest.mark.parametrize("defect", ["missing", "duplicate", "extra", "wrong-query"])
def test_snapshot_requires_exactly_one_matching_query_per_selected_question(replay, defect):
    rows = replay["rows"]
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows.append(rows[0].copy())
    elif defect == "extra":
        rows.append({**rows[0], "question_id": "pc-d7-002"})
    else:
        rows[0]["query"] = "与冻结题目不同的问题"
    write_jsonl(replay["rankings_path"], rows)
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()


@pytest.mark.parametrize("defect", ["text", "metadata", "unknown-chunk"])
def test_rankings_must_match_current_catalog_content_and_identity(replay, defect):
    hit = replay["rows"][0]["hits"][0]
    if defect == "text":
        hit["text"] += "伪造文本"
    elif defect == "metadata":
        hit["metadata"]["source_name"] = "substituted.md"
    else:
        hit["metadata"]["chunk_id"] = "00000000-0000-0000-0000-000000000000"
    write_jsonl(replay["rankings_path"], replay["rows"])
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()


@pytest.mark.parametrize("defect", ["negative-time", "nonfinite-time", "hits-with-error"])
def test_snapshot_rejects_invalid_measurements_and_conflicting_failure_state(replay, defect):
    if defect == "negative-time":
        replay["rows"][0]["timings_ms"]["total"] = -1.0
    elif defect == "nonfinite-time":
        replay["rows"][0]["timings_ms"]["total"] = float("nan")
    else:
        replay["rows"][0]["error"] = {"type": "timeout", "message": "fixture conflict"}
    write_jsonl(replay["rankings_path"], replay["rows"])
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()


def test_existing_output_cannot_be_overwritten(replay):
    replay["output"].mkdir()
    marker = replay["output"] / "keep.txt"
    marker.write_text("已有实验记录", encoding="utf-8")
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert marker.read_text(encoding="utf-8") == "已有实验记录"
    assert list(replay["output"].iterdir()) == [marker]


def test_null_selection_runs_every_dev_question_with_missing_timings_uncollected(replay):
    split = json.loads((replay["dataset"] / "split_manifest.json").read_text())
    questions = {row["question_id"]: row for row in read_jsonl(replay["dataset"] / "questions.jsonl")}
    replay["config"]["question_ids"] = None
    write_json(replay["config_path"], replay["config"])
    write_jsonl(replay["rankings_path"], [
        {"question_id": question_id,
         "query": questions[question_id]["standalone_question"] or questions[question_id]["question"],
         "hits": [], "error": None, "timings_ms": dict.fromkeys(PHASES)}
        for question_id in split["dev"]
    ])
    summary = evaluate(replay)
    assert (summary["question_count"], summary["answerable_count"], summary["unanswerable_count"]) == (30, 26, 4)
    assert summary["at_k"]["10"]["evidence_recall"] == 0.0
    assert summary["unanswerable"]["empty_hits"] == 4
    assert summary["timings_ms"]["total"] == {"samples": 0, "median": None, "p95": None}


def test_follow_up_requires_the_frozen_standalone_query(replay):
    question = next(
        row for row in read_jsonl(replay["dataset"] / "questions.jsonl")
        if row["question_id"] == "pc-d7-020"
    )
    assert question["standalone_question"] != question["question"]
    replay["config"]["question_ids"] = [question["question_id"]]
    write_json(replay["config_path"], replay["config"])
    row = {"question_id": question["question_id"], "query": question["question"],
           "hits": [], "error": None, "timings_ms": dict.fromkeys(PHASES)}
    write_jsonl(replay["rankings_path"], [row])
    with pytest.raises(EvaluationError):
        evaluate(replay)
    assert not replay["output"].exists()
    row["query"] = question["standalone_question"]
    write_jsonl(replay["rankings_path"], [row])
    assert evaluate(replay)["question_count"] == 1


def test_evaluation_is_read_only_and_does_not_open_env_or_network(replay, monkeypatch):
    paths = list(replay["dataset"].rglob("*")) + [replay["config_path"], replay["rankings_path"]]
    before = {path: path.read_bytes() for path in paths if path.is_file()}
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "离线评测不能读取运行时.env"
        return original_open(path, *args, **kwargs)

    def deny_connection(*args, **kwargs):
        raise AssertionError("离线回放不能发起网络请求")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(socket.socket, "connect", deny_connection)
    evaluate(replay)
    assert {path: path.read_bytes() for path in before} == before


def test_module_cli_reports_success_and_safe_failure_without_partial_output(replay):
    command = [
        sys.executable, "-m", "evals.run", "--dataset", str(replay["dataset"]),
        "--config", str(replay["config_path"]), "--rankings", str(replay["rankings_path"]),
        "--output-dir", str(replay["output"]),
    ]
    success = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
    assert success.returncode == 0, success.stdout + success.stderr
    assert json.loads(success.stdout)["status"] == "ok"
    original = (replay["output"] / "summary.json").read_bytes()
    failure = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
    assert failure.returncode == 1
    assert json.loads(failure.stdout)["status"] == "error"
    assert (replay["output"] / "summary.json").read_bytes() == original
