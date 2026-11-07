"""The synthetic benchmark tests the real graph and honest offline evidence accounting."""

import copy
import json
from pathlib import Path

import pytest

from evals.agent_tasks.__main__ import write_new
from evals.agent_tasks.dataset import DEFAULT_DATASET, digest, load_dataset
from evals.agent_tasks.runner import run_contract
from evals.agent_tasks.scoring import compare, score


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


@pytest.fixture(scope="module")
def bundle(dataset):
    cases, sha = dataset
    return run_contract(cases, sha, "all")


def test_fixed_family_split_and_frozen_fingerprint(dataset):
    cases, sha = dataset
    manifest = json.loads(DEFAULT_DATASET.with_name("frozen_manifest.json").read_text())
    assert len(cases) == 30
    assert sum(case.split == "dev" for case in cases) == 20
    assert sum(case.split == "holdout" for case in cases) == 10
    assert {case.family for case in cases if case.split == "dev"}.isdisjoint(
        case.family for case in cases if case.split == "holdout")
    assert {case.category for case in cases} == {
        "knowledge", "ticket", "joint", "conditional_proposal", "insufficient", "exception"}
    assert sha == manifest["dataset_sha256"]
    assert manifest["human_gold_review"] == "pending"


def test_real_compiled_graph_contract_denominators(dataset, bundle):
    result = score(*dataset, bundle)
    assert result["contract_success"] == {"numerator": 30, "denominator": 30, "rate": 1.0}
    assert result["terminal_failures"]["numerator"] == 6
    assert result["terminal_failures"]["denominator"] == 30
    assert result["counts"]["external_model_requests"]["total"] == 0
    assert result["counts"]["real_service_executions"]["total"] == 0
    assert result["counts"]["tool_attempts"] > result["counts"]["tool_handler_executions"]
    assert result["usage"] is None
    assert result["usage_coverage"]["numerator"] == 0
    assert result["model_decision_success"] is None
    assert result["human_factual_support"]["pending"] > 0
    assert result["human_factual_support"]["supported"]["rate"] is None
    assert result["prompt_characters"]["observation_coverage"]["rate"] == 1
    assert result["prompt_characters"]["known_tool_observation_chars"] > 0
    assert result["groups"]["split"]["dev"]["denominator"] == 20
    assert result["groups"]["split"]["holdout"]["denominator"] == 10


def test_script_repeatability_and_real_execution_ledger(dataset, bundle):
    again = run_contract(*dataset, "all")
    assert score(*dataset, bundle) == score(*dataset, again)
    repeated = next(row for row in bundle["records"] if row["case_id"] == "repeated_read-1")
    assert len(repeated["backend_executions"]) == 1
    assert repeated["result"]["tool_calls"] == 2
    assert repeated["result"]["events"][1]["executed"] is False
    same_content = next(row for row in bundle["records"] if row["case_id"] == "knowledge_comparison-1")
    observed = same_content["prompt_observations"][-1]
    assert observed["tool_message_count"] == 2
    assert observed["tool_observation_chars"] >= observed["distinct_tool_observation_chars"]
    proposal = next(row for row in bundle["records"] if row["case_id"] == "conditional_create-1")
    assert proposal["result"]["status"] == "pending_confirmation"
    assert all(row["name"] != "create_ticket" for row in proposal["backend_executions"])


def test_missing_record_is_failure_without_denominator_shrink(dataset, bundle):
    partial = copy.deepcopy(bundle)
    partial["records"].pop()
    result = score(*dataset, partial)
    assert result["contract_success"]["numerator"] == 29
    assert result["contract_failure"]["denominator"] == 30
    assert result["record_coverage"]["numerator"] == 29
    assert result["cases"][-1]["failed_checks"] == ["missing_record"]


@pytest.mark.parametrize("mutation", ["duplicate", "unknown", "dataset", "config", "counter", "usage", "observation"])
def test_invalid_recorded_evidence_rejected(dataset, bundle, mutation):
    broken = copy.deepcopy(bundle)
    first = broken["records"][0]
    if mutation == "duplicate":
        broken["records"].append(copy.deepcopy(first))
    elif mutation == "unknown":
        first["case_id"] = "not-in-fixed-dataset"
    elif mutation == "dataset":
        broken["provenance"]["dataset_sha256"] = "wrong"
    elif mutation == "config":
        broken["provenance"]["config"]["model"] = "different"
    elif mutation == "counter":
        first["result"]["tool_executions"] += 1
    elif mutation == "usage":
        first["result"]["usage"] = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 0}
    elif mutation == "observation":
        first["prompt_observations"] = []
    with pytest.raises(ValueError):
        score(*dataset, broken)


def test_recorded_import_offline_unknown_usage_stays_unknown(dataset, bundle, monkeypatch):
    recorded = copy.deepcopy(bundle)
    recorded["provenance"].update(execution_mode="recorded_external", model_decisions="external_claim_unverified")
    for row in recorded["records"]:
        row["external_model_requests"] = None
        row["real_service_executions"] = None
        row["prompt_observations"] = None
        row["backend_executions"] = None
    def no_execute(*args, **kwargs):
        raise AssertionError("scoring must not execute a graph or a model")
    monkeypatch.setattr("evals.agent_tasks.runner.run_contract", no_execute)
    result = score(*dataset, recorded)
    assert result["contract_success"]["numerator"] == 30
    assert result["counts"]["external_model_requests"]["total"] is None
    assert result["counts"]["real_service_executions"]["total"] is None
    assert result["prompt_characters"]["observation_coverage"]["numerator"] == 0
    assert result["usage"] is None


def test_human_support_is_explicit_bound_to_answer(dataset, bundle):
    row = bundle["records"][0]
    review = {"case_id": row["case_id"], "dataset_sha256": dataset[1],
              "answer_sha256": digest(row["result"]["answer"]),
              "evidence_sha256": digest(row["result"]["sources"]), "reviewer_kind": "human",
              "reviewer": "test-fixture-only-not-a-real-review", "verdict": "supported"}
    result = score(*dataset, bundle, reviews=[review])
    assert result["human_factual_support"]["reviewed"]["numerator"] == 1
    assert result["human_factual_support"]["supported"]["denominator"] == 1
    changed = copy.deepcopy(bundle)
    changed["records"][0]["result"]["sources"][0]["data"]["text"] = "相反证据"
    with pytest.raises(ValueError, match="evidence"):
        score(*dataset, changed, reviews=[review])
    review["answer_sha256"] = "stale-answer"
    with pytest.raises(ValueError, match="exact answer"):
        score(*dataset, bundle, reviews=[review])


def test_factual_hallucination_is_not_hidden_by_valid_source_ids(dataset, bundle):
    changed = copy.deepcopy(bundle)
    changed["records"][0]["result"]["answer"] = "完全捏造的事实，但引用编号合法。"
    result = score(*dataset, changed)
    assert result["contract_success"]["numerator"] == 30
    assert result["cases"][0]["human_factual_support"] is None
    assert result["model_decision_success"] is None


def test_failed_expectation_and_comparison_guards(dataset, bundle):
    before = score(*dataset, bundle)
    changed = copy.deepcopy(bundle)
    changed["records"][0]["result"]["status"] = "insufficient_evidence"
    after = score(*dataset, changed)
    assert after["contract_failure"]["numerator"] == 1
    delta = compare(before, after)
    assert delta["contract_success_delta"] == -1
    assert compare(before, before)["prompt_character_deltas"]["known_tool_observation_chars"] == 0
    after["provenance"]["dataset_sha256"] = "other"
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare(before, after)


def test_output_never_overwrites_archive(tmp_path):
    path = tmp_path / "summary.json"
    write_new(path, {"old": True})
    with pytest.raises(FileExistsError):
        write_new(path, {"new": True})
    assert json.loads(path.read_text()) == {"old": True}


def test_scenario_family_leak_rejected(tmp_path):
    payload = json.loads(DEFAULT_DATASET.read_text())
    payload["cases"][0]["split"] = "holdout"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="leaks"):
        load_dataset(path)


def test_comparison_rejects_missing_observations(dataset, bundle):
    before = score(*dataset, bundle)
    after = copy.deepcopy(before)
    after["prompt_characters"]["observation_coverage"]["rate"] = 0
    with pytest.raises(ValueError, match="every model attempt"):
        compare(before, after)


def test_frozen_dataset_change_rejected(tmp_path):
    payload = json.loads(DEFAULT_DATASET.read_text())
    payload["cases"][0]["question"] += "改动"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload))
    path.with_name("frozen_manifest.json").write_text(DEFAULT_DATASET.with_name("frozen_manifest.json").read_text())
    with pytest.raises(ValueError, match="fingerprint"):
        load_dataset(path)


def test_missing_cases_keep_total_usage_and_requests_unknown(dataset, bundle):
    partial = copy.deepcopy(bundle)
    partial["provenance"].update(execution_mode="recorded_external", model_decisions="unverified")
    partial["records"] = partial["records"][:1]
    row = partial["records"][0]
    row["external_model_requests"] = 2
    row["result"]["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    result = score(*dataset, partial)
    assert result["usage"] is None
    assert result["usage_coverage"]["numerator"] == 1
    assert result["usage_coverage"]["denominator"] == 30
    assert result["counts"]["external_model_requests"] == {
        "total": None, "known_sum": 2, "measured_records": 1, "record_denominator": 30}
