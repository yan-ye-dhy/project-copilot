"""D16公开候选对照：使用真实冻结语料与离线假评分，不运行模型或服务。"""

import builtins
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from shutil import copytree
import socket
import sys

import pytest

from evals import rerank_run
from evals.comparison import compare_retrieval
from evals.metrics import EvaluationError
from evals.run import _aggregate
from project_copilot.rag import answering, rerank


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PARENT_RUN = "hybrid-v2-20261022-01"
PHASES = {"embedding", "search", "fusion", "rerank", "total"}


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_rows(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def reseal_parent(case: dict) -> None:
    """只重封临时公开副本，让非法输入不只停在单个文件hash检查。"""
    parent = case["parent"]
    receipt = read_json(parent / "manifest.json")
    receipt["files"] = {name: digest(parent / name) for name in receipt["files"]}
    write_json(parent / "manifest.json", receipt)


def reseal_result(case: dict) -> None:
    """保留完整字节哈希，但不替实现修正伪造的业务记录。"""
    output = case["output"]
    receipt = read_json(output / "manifest.json")
    receipt["files"] = {name: digest(output / name) for name in receipt["files"]}
    write_json(output / "manifest.json", receipt)


@pytest.fixture(autouse=True)
def no_network_dotenv_or_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    original_open, original_import = Path.open, builtins.__import__

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "D16离线测试不得读取真实.env"
        return original_open(path, *args, **kwargs)

    def guarded_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"torch", "transformers"}, "D16离线测试不得导入模型依赖"
        return original_import(name, *args, **kwargs)

    def no_connect(*args, **kwargs):
        raise AssertionError("D16离线测试不得访问外部服务")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket.socket, "connect", no_connect)


@pytest.fixture
def case(tmp_path: Path, monkeypatch, bind_current_sources) -> dict:
    dataset, parent = tmp_path / "dataset", tmp_path / PARENT_RUN
    copytree(PROJECT_ROOT / "evals/datasets/copilot_v0", dataset)
    copytree(PROJECT_ROOT / "evals/results" / PARENT_RUN, parent)
    # 本文件使用fake scorer；只给临时父凭据绑定当前源码，不改正式V2/GPU证据。
    write_json(parent / "manifest.json", bind_current_sources(read_json(parent / "manifest.json")))
    parent_sha = digest(parent / "manifest.json")
    monkeypatch.setattr(rerank_run, "PARENT_SHA256", parent_sha)
    evidence = tmp_path / "gpu-provenance.json"
    write_json(evidence, bind_current_sources(read_json(PROJECT_ROOT / rerank_run.GPU_EVIDENCE)))
    monkeypatch.setattr(rerank_run, "GPU_EVIDENCE", str(evidence))
    monkeypatch.setattr(rerank_run, "GPU_EVIDENCE_SHA256", digest(evidence))
    config = tmp_path / "rerank.json"
    descriptor = read_json(PROJECT_ROOT / "evals/configs/rerank_v3.json")
    descriptor["parent_manifest_sha256"] = parent_sha
    write_json(config, descriptor)
    return {"dataset": dataset, "parent": parent, "config": config,
            "output": tmp_path / "rerank-v3-test"}


def paths(case: dict) -> tuple[Path, ...]:
    return case["dataset"], case["parent"], case["config"]


class RecordingScorer:
    """由片段文本决定logit，避免只记录调用却返回随调用次数变化的排名。"""

    def __init__(self, *, fail_at=(), ties=False) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_at, self.ties = set(fail_at), ties
        self.load_ms = None
        self.last_load_ms = self.last_inference_ms = self.last_total_ms = 0.0
        self.last_diagnostics = []

    def value(self, text: str) -> float:
        return -2.5 if self.ties else float(int(sha256(text.encode("utf-8")).hexdigest()[:8], 16) % 101 - 50)

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, list(passages)))
        self.last_load_ms = 1500.0 if self.load_ms is None else 0.0
        self.load_ms = 1500.0
        self.last_inference_ms = 8000.0 if len(self.calls) == 1 else 2.0
        self.last_total_ms = self.last_load_ms + self.last_inference_ms
        self.last_diagnostics = [{"original_pair_tokens": 600 if index == 9 else 100 + index,
                                  "input_tokens": 512 if index == 9 else 100 + index,
                                  "passage_truncated": index == 9}
                                 for index in range(len(passages))]
        if len(self.calls) in self.fail_at:
            raise rerank.RerankError("离线合成评分失败；不得回退、重试或漏题")
        return [self.value(text) for text in passages]


def collect(case: dict, scorer: RecordingScorer | None = None) -> dict:
    return rerank_run.collect_experiment(*paths(case), case["output"], scorer=scorer or RecordingScorer())


def test_plan_is_offline_and_declares_fixed_budget_without_creating_output(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_model(*args, **kwargs):
        pytest.fail("plan不应创建模型或检查GPU缓存")

    monkeypatch.setattr(rerank, "LocalCrossEncoder", forbidden_model)
    monkeypatch.setattr(rerank_run, "LocalCrossEncoder", forbidden_model, raising=False)
    plan = rerank_run.plan_experiment(*paths(case))
    assert plan["status"] == "ok" and plan["mode"] == "offline_plan"
    assert plan["local_score_calls"] == 31 and plan["scored_pairs"] == 310
    assert plan["qdrant_requests"] == plan["model_api_calls"] == 0
    assert not case["output"].exists()


def test_collect_scores_exact_original_candidates_once_after_one_excluded_warmup(case: dict) -> None:
    scorer = RecordingScorer()
    result = collect(case, scorer)
    original = read_rows(case["parent"] / "hybrid_rankings.jsonl")
    expected = [(row["query"], [hit["text"] for hit in row["hits"]]) for row in original]
    assert scorer.calls == [expected[0], *expected]
    assert len(scorer.calls) == 31 and sum(len(passages) for _, passages in scorer.calls) == 310
    assert result["status"] == "ok" and result["complete"] is True
    assert result["local_score_calls"] == result["successful_score_calls"] == 31
    assert result["attempted_pairs"] == result["scored_pairs"] == 310
    rows = read_rows(case["output"] / "rerank_rankings.jsonl")
    assert [row["question_id"] for row in rows] == [row["question_id"] for row in original]
    assert len(rows) == 30


@pytest.mark.parametrize("field,value", [("ranking_depth", 5), ("answer_top_k", 10),
                                         ("warmup_calls", 0), ("smoke_questions", 3),
                                         ("rerank.device", "cpu"), ("rerank.dtype", "float32"),
                                         ("rerank.batch_size", 4), ("rerank.max_length", 256),
                                         ("rerank.max_candidates", 20), ("rerank.top_k", 5)])
def test_experiment_contract_rejects_changes_to_fixed_budget_and_model_parameters(
    case: dict, field: str, value: object,
) -> None:
    config = read_json(case["config"])
    if field.startswith("rerank."):
        config["rerank"][field.split(".")[1]] = value
    else:
        config[field] = value
    write_json(case["config"], config)
    scorer = RecordingScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert scorer.calls == [] and not case["output"].exists()


def test_baseline_preserves_exact_historical_bytes_scores_and_timings(case: dict) -> None:
    collect(case)
    source = case["parent"] / "hybrid_rankings.jsonl"
    snapshot = case["output"] / "baseline_rankings.jsonl"
    assert snapshot.read_bytes() == source.read_bytes()
    baseline = read_json(case["output"] / "baseline_retrieval/summary.json")
    historical = read_json(case["parent"] / "hybrid_retrieval/summary.json")
    for key in ("at_k", "question_count", "answerable_count", "unanswerable_count", "timings_ms"):
        assert baseline[key] == historical[key]


def test_reranked_hits_retain_all_original_text_metadata_and_original_scores(case: dict) -> None:
    scorer = RecordingScorer()
    collect(case, scorer)
    original = read_rows(case["parent"] / "hybrid_rankings.jsonl")
    ranked = read_rows(case["output"] / "rerank_rankings.jsonl")
    for before, after in zip(original, ranked, strict=True):
        expected = sorted(before["hits"], key=lambda hit: -scorer.value(hit["text"]))
        assert after["error"] is None
        assert after["hits"] == expected
        assert len(after["hits"]) == 10
        assert {hit["metadata"]["chunk_id"] for hit in after["hits"]} == {
            hit["metadata"]["chunk_id"] for hit in before["hits"]}


def test_score_details_save_original_hits_separate_logits_all_ten_ranks_and_five_contexts(case: dict) -> None:
    scorer = RecordingScorer()
    collect(case, scorer)
    original = read_rows(case["parent"] / "hybrid_rankings.jsonl")
    details = read_rows(case["output"] / "score_details.jsonl")
    rankings = read_rows(case["output"] / "rerank_rankings.jsonl")
    assert len(details) == 30
    for before, detail, ranking in zip(original, details, rankings, strict=True):
        assert detail["question_id"] == before["question_id"] and detail["query"] == before["query"]
        assert [item["hit"] for item in detail["candidates"]] == before["hits"]
        assert [item["candidate_rank"] for item in detail["candidates"]] == list(range(1, 11))
        assert [item["raw_logit"] for item in detail["candidates"]] == [
            scorer.value(hit["text"]) for hit in before["hits"]]
        assert sorted(item["rerank_rank"] for item in detail["candidates"]) == list(range(1, 11))
        assert detail["selected_hits"] == ranking["hits"][:5]
        assert len(detail["selected_hits"]) == 5
        assert detail["candidates"][-1]["original_pair_tokens"] == 600
        assert detail["candidates"][-1]["input_tokens"] == 512
        assert detail["candidates"][-1]["passage_truncated"] is True


def test_first_load_and_inference_are_saved_in_warmup_separately_from_hot_dev(case: dict) -> None:
    collect(case)
    original = read_rows(case["parent"] / "hybrid_rankings.jsonl")[0]
    warmup = read_json(case["output"] / "warmup.json")
    assert warmup["question_id"] == original["question_id"] and warmup["query"] == original["query"]
    assert [item["hit"] for item in warmup["candidates"]] == original["hits"]
    assert warmup["timings_ms"]["model_load"] == 1500.0
    assert warmup["timings_ms"]["model_inference"] == 8000.0
    assert "excluded-from-dev-metrics" in warmup["purpose"]
    details = read_rows(case["output"] / "score_details.jsonl")
    assert all(detail["timings_ms"]["model_load"] == 0.0 for detail in details)
    assert all(detail["timings_ms"]["model_inference"] == 2.0 for detail in details)
    runtime = read_json(case["output"] / "runtime.json")
    assert runtime["injected_scorer"] is True
    assert runtime["execution_mode"] == "injected_offline_scorer"
    assert runtime["precision"] == "float16" and runtime["requested_device"] == "cuda"


def test_tied_logits_keep_candidate_order_without_mixing_original_scores(case: dict) -> None:
    collect(case, RecordingScorer(ties=True))
    original = read_rows(case["parent"] / "hybrid_rankings.jsonl")
    ranked = read_rows(case["output"] / "rerank_rankings.jsonl")
    assert [row["hits"] for row in ranked] == [row["hits"] for row in original]


def test_v3_timings_only_measure_rerank_and_exclude_historical_search_and_warmup(case: dict) -> None:
    collect(case)
    rows = read_rows(case["output"] / "rerank_rankings.jsonl")
    for row in rows:
        assert set(row["timings_ms"]) == PHASES
        assert row["timings_ms"]["rerank"] >= 0
        assert all(row["timings_ms"][phase] is None for phase in PHASES - {"rerank"})
    summary = read_json(case["output"] / "rerank_retrieval/summary.json")
    assert summary["timings_ms"]["rerank"]["samples"] == 30
    for phase in PHASES - {"rerank"}:
        assert summary["timings_ms"][phase] == {"samples": 0, "median": None, "p95": None}
    assert summary["question_count"] == 30


def test_reranking_cannot_add_missing_evidence_at_full_candidate_depth(case: dict) -> None:
    collect(case)
    baseline = read_rows(case["output"] / "baseline_retrieval/retrieval.jsonl")
    candidate = read_rows(case["output"] / "rerank_retrieval/retrieval.jsonl")
    for before, after in zip(baseline, candidate, strict=True):
        for field in ("document_recall", "evidence_recall", "all_evidence_covered"):
            assert before["metrics"]["at_k"]["10"][field] == after["metrics"]["at_k"]["10"][field]


def test_four_unanswerable_questions_remain_separate_without_refusal_claims(case: dict) -> None:
    collect(case)
    for branch in ("baseline", "rerank"):
        summary = read_json(case["output"] / f"{branch}_retrieval/summary.json")
        assert summary["question_count"] == 30 and summary["answerable_count"] == 26
        assert summary["unanswerable_count"] == summary["unanswerable"]["count"] == 4
        assert summary["unanswerable"]["refusal_accuracy"] is None
        rows = read_rows(case["output"] / f"{branch}_retrieval/retrieval.jsonl")
        no_answer = [row for row in rows if not row["metrics"]["answerable"]]
        assert len(no_answer) == 4
        assert all(value is None for row in no_answer for values in row["metrics"]["at_k"].values()
                   for value in values.values())


@pytest.mark.parametrize("fail_at", [1, 2, 3], ids=["warmup", "first-dev", "second-dev"])
def test_warmup_and_first_two_dev_failures_abort_without_a_seal(case: dict, fail_at: int) -> None:
    scorer = RecordingScorer(fail_at={fail_at})
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert len(scorer.calls) == fail_at
    assert not (case["output"] / "manifest.json").exists()
    status = read_json(case["output"] / "status.json")
    assert status["complete"] is False
    assert status["local_score_calls"] == fail_at and status["attempted_pairs"] == fail_at * 10
    assert status["successful_score_calls"] == fail_at - 1
    assert status["scored_pairs"] == (fail_at - 1) * 10


def test_later_failures_keep_complete_dev_denominator_and_never_fallback(case: dict) -> None:
    # 调用4对应第3道开发题；调用8对应第7道，二者都在可回答题分母中。
    scorer = RecordingScorer(fail_at={4, 8})
    result = collect(case, scorer)
    assert len(scorer.calls) == 31
    assert result["local_score_calls"] == 31 and result["attempted_pairs"] == 310
    assert result["successful_score_calls"] == 29 and result["scored_pairs"] == 290
    rows = read_rows(case["output"] / "rerank_retrieval/retrieval.jsonl")
    failed = [row for row in rows if row["error"] is not None]
    assert len(rows) == 30 and len(failed) == 2
    assert all(row["hits"] == [] for row in failed)
    assert all(not value for row in failed if row["metrics"]["answerable"]
               for values in row["metrics"]["at_k"].values() for value in values.values())
    summary = read_json(case["output"] / "rerank_retrieval/summary.json")
    assert summary["question_count"] == 30 and summary["answerable_count"] == 26
    assert summary["retrieval_error_count"] == 2
    assert rerank_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


@pytest.mark.parametrize("scores", [[1.0] * 9, [float("nan")] * 10,
                                     [float("inf")] * 10, [True] * 10])
def test_invalid_score_results_stop_in_warmup_without_a_fallback_or_seal(case: dict, scores: list) -> None:
    class InvalidScorer(RecordingScorer):
        def score(self, query, passages):
            super().score(query, passages)
            return scores

    scorer = InvalidScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert len(scorer.calls) == 1
    assert not (case["output"] / "manifest.json").exists()
    status = read_json(case["output"] / "status.json")
    assert status["complete"] is False
    assert status["local_score_calls"] == 1 and status["attempted_pairs"] == 10
    assert status["successful_score_calls"] == status["scored_pairs"] == 0


def test_existing_output_is_rejected_before_scoring_without_changing_contents(case: dict) -> None:
    case["output"].mkdir()
    marker = case["output"] / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    scorer = RecordingScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert scorer.calls == [] and marker.read_text(encoding="utf-8") == "keep"
    assert list(case["output"].iterdir()) == [marker]


@pytest.mark.parametrize("root", ["dataset", "parent"])
def test_output_must_not_be_inside_frozen_inputs(case: dict, root: str) -> None:
    case["output"] = case[root] / "illegal-output"
    scorer = RecordingScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert scorer.calls == [] and not case["output"].exists()


def test_changed_frozen_source_is_rejected_without_scoring(case: dict) -> None:
    source = read_rows(case["dataset"] / "sources.jsonl")[0]["path"]
    path = case["dataset"] / source
    path.write_bytes(path.read_bytes() + b"\n")
    scorer = RecordingScorer()
    with pytest.raises(ValueError):
        collect(case, scorer)
    assert scorer.calls == [] and not case["output"].exists()


def test_parent_byte_tampering_is_rejected_without_scoring(case: dict) -> None:
    path = case["parent"] / "hybrid_rankings.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    scorer = RecordingScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert scorer.calls == [] and not case["output"].exists()


@pytest.mark.parametrize("change", ["holdout", "duplicate-question", "query", "missing-candidate",
                                    "duplicate-chunk", "text", "metadata", "rank-order"])
def test_resealed_parent_cannot_change_fixed_dev_identity_or_candidates(case: dict, change: str) -> None:
    path = case["parent"] / "hybrid_rankings.jsonl"
    rows = read_rows(path)
    first = rows[0]
    if change == "holdout":
        first["question_id"] = read_json(case["dataset"] / "split_manifest.json")["holdout"][0]
    elif change == "duplicate-question":
        rows[1]["question_id"] = first["question_id"]
    elif change == "query":
        first["query"] += " changed"
    elif change == "missing-candidate":
        first["hits"].pop()
    elif change == "duplicate-chunk":
        first["hits"][1] = deepcopy(first["hits"][0])
    elif change == "text":
        first["hits"][0]["text"] += " changed"
    elif change == "metadata":
        first["hits"][0]["metadata"]["source_name"] = "foreign.md"
    else:
        first["hits"][0], first["hits"][1] = first["hits"][1], first["hits"][0]
    write_rows(path, rows)
    reseal_parent(case)
    scorer = RecordingScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer)
    assert scorer.calls == [] and not case["output"].exists()


@pytest.mark.parametrize("change", ["changed", "missing", "extra"])
def test_verify_rejects_tampered_missing_and_unlisted_artifacts(case: dict, change: str) -> None:
    collect(case)
    target = case["output"] / "score_details.jsonl"
    if change == "changed":
        target.write_bytes(target.read_bytes() + b"\n")
    elif change == "missing":
        target.unlink()
    else:
        (case["output"] / "unlisted.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(EvaluationError):
        rerank_run.verify_experiment(case["dataset"], case["output"])


@pytest.mark.parametrize("change", ["original-score", "raw-logit", "candidate-rank", "missing-candidate",
                                    "diagnostic-flag", "diagnostic-max", "diagnostic-type", "query",
                                    "answer-context", "warmup-counted"])
def test_resealed_results_still_enforce_score_identity_diagnostics_and_budget(case: dict, change: str) -> None:
    collect(case)
    path = case["output"] / "score_details.jsonl"
    rows = read_rows(path)
    candidates = rows[0]["candidates"]
    if change == "original-score":
        candidates[0]["hit"]["score"] += 0.25
    elif change == "raw-logit":
        next(item for item in candidates if item["rerank_rank"] != 1)["raw_logit"] = 1000000.0
    elif change == "candidate-rank":
        candidates[1]["candidate_rank"] = 1
    elif change == "missing-candidate":
        candidates.pop()
    elif change == "diagnostic-flag":
        candidates[-1]["passage_truncated"] = False
    elif change == "diagnostic-max":
        candidates[-1]["input_tokens"] = 513
    elif change == "diagnostic-type":
        candidates[-1]["passage_truncated"] = 1
    elif change == "query":
        rows[0]["query"] += " changed"
    elif change == "answer-context":
        rows[0]["selected_hits"].append(candidates[0]["hit"])
    else:
        status = read_json(case["output"] / "status.json")
        status["question_count"] = 31
        write_json(case["output"] / "status.json", status)
        receipt = read_json(case["output"] / "manifest.json")
        receipt["status"] = status
        write_json(case["output"] / "manifest.json", receipt)
    write_rows(path, rows)
    reseal_result(case)
    with pytest.raises(EvaluationError):
        rerank_run.verify_experiment(case["dataset"], case["output"])


def test_resealed_self_consistent_metrics_must_still_match_rankings_and_frozen_gold(case: dict) -> None:
    collect(case)
    output = case["output"]
    scored = read_rows(output / "rerank_retrieval/retrieval.jsonl")
    values = scored[0]["metrics"]["at_k"]["1"]
    values["evidence_recall"] = 0.0 if values["evidence_recall"] else 1.0
    write_rows(output / "rerank_retrieval/retrieval.jsonl", scored)
    summary = read_json(output / "rerank_retrieval/summary.json")
    summary.update(_aggregate(scored, [1, 3, 5, 10]))
    write_json(output / "rerank_retrieval/summary.json", summary)
    categories = {row["question_id"]: row["category"] for row in scored}
    # 用现有D12比较器生成自洽的summary/comparison，不能只因普通汇总不一致而拒绝。
    comparison = compare_retrieval(output / "baseline_retrieval", output / "rerank_retrieval", categories)
    write_json(output / "comparison.json", comparison)
    cases = read_rows(output / "case_details.jsonl")
    for index, row in enumerate(cases):
        row["branch_metrics"]["rerank"] = scored[index]["metrics"]
        row["comparison"] = comparison["per_question"][index]
    write_rows(output / "case_details.jsonl", cases)
    reseal_result(case)
    with pytest.raises(EvaluationError, match="gold"):
        rerank_run.verify_experiment(case["dataset"], output)


def test_public_verify_needs_no_parent_cache_models_or_current_prompt(
    case: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect(case)
    public = case["output"].parent / "standalone-public"
    copytree(case["output"], public)

    def forbidden_current_state(*args, **kwargs):
        pytest.fail("公开verify必须读取已保存合同，不得依赖当前默认Prompt或模型")

    monkeypatch.setattr(rerank, "LocalCrossEncoder", forbidden_current_state)
    monkeypatch.setattr(rerank, "RerankConfig", forbidden_current_state)
    monkeypatch.setattr(rerank_run, "LocalCrossEncoder", forbidden_current_state, raising=False)
    monkeypatch.setattr(rerank_run, "RerankConfig", forbidden_current_state, raising=False)
    monkeypatch.setattr(answering, "SYSTEM_MESSAGE", "future prompt", raising=False)
    monkeypatch.setattr(answering, "prompt_info", forbidden_current_state)
    original_open = Path.open

    def only_public_inputs(path, *args, **kwargs):
        resolved = path.resolve()
        # 切分器身份计算需要已安装包的版本元数据；这不是模型缓存或运行输入。
        package_metadata = resolved.name == "METADATA" and resolved.parent.name.endswith(".dist-info")
        assert package_metadata or any(resolved.is_relative_to(root.resolve())
                                       for root in (case["dataset"], public)), (
            "公开verify不得读取原D14、模型缓存或当前源码")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", only_public_inputs)
    result = rerank_run.verify_experiment(case["dataset"], public)
    assert result["status"] == "ok" and result["state"] == "verified"
    assert result["manual_review"] == "pending" and result["day16_accepted"] is False


def test_recorded_automatic_experiment_does_not_claim_manual_acceptance(case: dict) -> None:
    collect(case)
    status = read_json(case["output"] / "status.json")
    assert status["complete"] is True and status["manual_review"] == "pending"
    assert status["day16_accepted"] is False
    assert status["qdrant_requests"] == status["model_api_calls"] == 0


def test_pair_scorer_without_model_diagnostics_still_collects_offline(case: dict) -> None:
    class MinimalScorer:
        def __init__(self):
            self.calls = 0

        def score(self, query, passages):
            self.calls += 1
            return [-1.0] * len(passages)

    scorer = MinimalScorer()
    result = rerank_run.collect_experiment(*paths(case), case["output"], scorer=scorer,
                                           cache_dir=case["output"].parent / "no-model-cache")
    assert result["complete"] is True and scorer.calls == 31
    details = read_rows(case["output"] / "score_details.jsonl")
    assert all(detail["timings_ms"]["model_load"] is None
               and detail["timings_ms"]["model_inference"] is None for detail in details)
    assert all(item[field] is None for detail in details for item in detail["candidates"]
               for field in ("original_pair_tokens", "input_tokens", "passage_truncated"))
    assert rerank_run.verify_experiment(case["dataset"], case["output"])["state"] == "verified"


def test_cli_plan_prints_one_offline_json_result(
    case: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["rerank_run", "plan", "--dataset", str(case["dataset"]),
                                    "--hybrid-dir", str(case["parent"]), "--config", str(case["config"])])
    assert rerank_run.main() == 0
    captured = capsys.readouterr()
    assert captured.err == "" and len(captured.out.splitlines()) == 1
    result = json.loads(captured.out)
    assert result["mode"] == "offline_plan" and result["scored_pairs"] == 310


def test_cli_collect_without_local_rejects_before_inputs_or_model(
    case: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("无--local门禁必须先于冻结输入和模型访问")

    monkeypatch.setattr(rerank_run, "prepare_experiment", forbidden)
    monkeypatch.setattr(rerank, "LocalCrossEncoder", forbidden)
    monkeypatch.setattr(sys, "argv", ["rerank_run", "collect", "--dataset", str(case["dataset"]),
                                    "--hybrid-dir", str(case["parent"]), "--config", str(case["config"]),
                                    "--output-dir", str(case["output"])])
    assert rerank_run.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "--local" in captured.err
    assert not case["output"].exists()
