"""D12比较器使用归档副本；变动指标仅为测试，不代表候选真实改善。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from evals.comparison import FIELDS, KS, compare_retrieval
from evals.metrics import EvaluationError
from evals.run import _aggregate


ARCHIVE = Path(__file__).resolve().parents[1] / "evals/results/dense-v0-20261019-01/retrieval"
IDS = ["pc-d7-001", "pc-d7-002", "pc-d7-004", "pc-d7-013", "pc-d7-017"]


def write_archive(path, receipt, rows):
    path.mkdir(exist_ok=True)
    summary = {key: receipt[key] for key in ("ranking_origin", "dataset_fingerprint", "split", "mapping_rule")}
    summary.update(_aggregate(rows, list(KS)))
    (path / "config.json").write_text(json.dumps(receipt), encoding="utf-8")
    (path / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (path / "retrieval.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def pair(tmp_path):
    receipt = json.loads((ARCHIVE / "config.json").read_text())
    receipt["effective_question_ids"] = IDS.copy()
    receipt["evaluation_config"]["question_ids"] = IDS.copy()
    by_id = {r["question_id"]: r for r in map(json.loads, (ARCHIVE / "retrieval.jsonl").read_text().splitlines())}
    rows = [by_id[qid] for qid in IDS]
    candidate = deepcopy(receipt)
    candidate["evaluation_config"]["splitter"]["chunk_size"] = 500
    return {"baseline_dir": tmp_path / "baseline", "candidate_dir": tmp_path / "candidate",
            "baseline_config": receipt, "candidate_config": candidate,
            "baseline": rows, "candidate": deepcopy(rows), "categories": {r["question_id"]: r["category"] for r in rows}}


def compare(pair):
    for side in ("baseline", "candidate"):
        write_archive(pair[f"{side}_dir"], pair[f"{side}_config"], pair[side])
    return compare_retrieval(pair["baseline_dir"], pair["candidate_dir"], pair["categories"])


def score(row, value):
    for k in KS:
        row["metrics"]["at_k"][str(k)] = {field: bool(value) if field == "all_evidence_covered" else value
                                         for field in FIELDS}


def test_identical_archive_is_same_and_does_not_write_source():
    receipt_before = (ARCHIVE / "config.json").read_bytes()
    rows = [json.loads(line) for line in (ARCHIVE / "retrieval.jsonl").read_text().splitlines()]
    result = compare_retrieval(ARCHIVE, ARCHIVE, {r["question_id"]: r["category"] for r in rows})
    assert result["baseline"]["question_count"] == 30
    assert result["baseline"]["answerable_count"] == 26
    assert all(r["outcome"] == ("same" if source["metrics"]["answerable"] else "unanswerable")
               for r, source in zip(result["per_question"], rows, strict=True))
    assert all(v == 0 for metrics in result["delta_at_k"].values() for v in metrics.values())
    assert (ARCHIVE / "config.json").read_bytes() == receipt_before


def test_improvement_regression_mixed_and_same_preserve_metric_deltas(pair):
    score(pair["baseline"][0], 0)
    score(pair["candidate"][0], 1)
    score(pair["baseline"][1], 1)
    score(pair["candidate"][1], 0)
    score(pair["baseline"][2], 1)
    score(pair["candidate"][2], 1)
    for side in ("baseline", "candidate"):
        score(pair[side][3], 1)
    pair["baseline"][3]["metrics"]["at_k"]["1"]["document_recall"] = 0.5
    pair["candidate"][3]["metrics"]["at_k"]["1"]["evidence_recall"] = 0.5
    result = compare(pair)
    assert [r["outcome"] for r in result["per_question"]] == ["improved", "regressed", "same", "mixed", "unanswerable"]
    assert result["delta_at_k"]["1"]["document_recall"] == pytest.approx(0.125)
    assert result["delta_at_k"]["1"]["evidence_recall"] == pytest.approx(-0.125)
    assert result["baseline"]["answerable_count"] == 4
    assert result["unanswerable"]["refusal_accuracy"] is None
    assert result["per_question"][-1]["delta_at_k"] is None
    bad = [r for group in result["bad_cases"].values() for r in group]
    assert any(r["question_id"] == IDS[1] and "incomplete_evidence_at_3" in r["candidate_reasons"] for r in bad)


def test_errors_are_reported_separately_from_equal_zero_scores_and_no_answer(pair):
    for side in ("baseline", "candidate"):
        pair[side][0]["hits"] = []
        score(pair[side][0], 0)
    pair["candidate"][0].update(status="retrieval_error", error={"type": "timeout", "message": "test"})
    pair["candidate"][-1].update(status="retrieval_error", hits=[], error={"type": "timeout", "message": "test"})
    result = compare(pair)
    assert result["per_question"][0]["outcome"] == "same"
    assert result["retrieval_error_count_delta"] == 2
    assert result["candidate"]["answerable_count"] == 4
    assert result["unanswerable"]["candidate"]["retrieval_errors"] == 1
    assert result["candidate"]["at_k"]["10"]["evidence_recall"] == pytest.approx(0.75)


def test_paired_timing_uses_each_pair_delta_and_keeps_missing_explicit(pair):
    for side in ("baseline", "candidate"):
        for row in pair[side]:
            row["timings_ms"] = dict.fromkeys(row["timings_ms"])
    for row, elapsed in zip(pair["baseline"][:3], [0, 100, 101]):
        row["timings_ms"]["search"] = elapsed
    for row, elapsed in zip(pair["candidate"][:4], [99, 0, 100, 20]):
        row["timings_ms"]["search"] = elapsed
    result = compare(pair)
    assert result["paired_timings_ms"]["search"] == {
        "paired_samples": 3, "excluded_pairs": 2, "baseline_median_ms": 100,
        "candidate_median_ms": 99, "median_delta_ms": -1, "p95_delta_ms": 99,
    }
    assert result["paired_timings_ms"]["rerank"]["median_delta_ms"] is None
    assert result["paired_timings_ms"]["rerank"]["paired_samples"] == 0
    assert "not_a_controlled_latency_benchmark" in result["timing_note"]


@pytest.mark.parametrize("defect", ["synthetic", "holdout", "fingerprint", "ks", "mapping", "order",
                                     "missing", "query", "category", "gold-count", "two-config-factors"])
def test_incomparable_inputs_are_rejected(pair, defect):
    receipt = pair["candidate_config"]
    config = receipt["evaluation_config"]
    if defect in ("synthetic", "holdout", "fingerprint"):
        key, value = {"synthetic": ("ranking_origin", "synthetic_fixture"), "holdout": ("split", "holdout"),
                      "fingerprint": ("dataset_fingerprint", "other-dataset")}[defect]
        receipt[key] = config[key] = value
    elif defect == "ks":
        config["ks"] = [1, 3, 5]
    elif defect == "mapping":
        receipt["mapping_rule"] = "different-mapping"
    elif defect == "order":
        receipt["effective_question_ids"].reverse()
        pair["candidate"].reverse()
    elif defect == "missing":
        pair["candidate"].pop()
    elif defect == "query":
        pair["candidate"][0]["query"] = "different query"
    elif defect == "category":
        pair["candidate"][0]["category"] = "different category"
    elif defect == "gold-count":
        pair["candidate"][0]["metrics"]["gold_evidence_count"] += 1
    else:
        config["ranking_origin"] = "other"
    with pytest.raises(EvaluationError):
        compare(pair)


@pytest.mark.parametrize("defect", ["metric-nan", "negative-time", "failed-positive-metrics", "missing-category"])
def test_invalid_scores_timings_and_failure_contract_are_rejected(pair, defect):
    row = pair["candidate"][0]
    if defect == "metric-nan":
        row["metrics"]["at_k"]["1"]["evidence_recall"] = float("nan")
    elif defect == "negative-time":
        row["timings_ms"]["search"] = -1
    elif defect == "failed-positive-metrics":
        row.update(status="retrieval_error", error={"type": "timeout", "message": "test"}, hits=[])
    else:
        pair["categories"].pop(IDS[0])
    with pytest.raises(EvaluationError):
        compare(pair)


def test_summary_tampering_is_rejected(pair):
    compare(pair)
    path = pair["candidate_dir"] / "summary.json"
    summary = json.loads(path.read_text())
    summary["at_k"]["1"]["evidence_recall"] = 0.12345
    path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(EvaluationError, match="汇总与逐题"):
        compare_retrieval(pair["baseline_dir"], pair["candidate_dir"], pair["categories"])
