"""D17选型与保留集编排；真实冻结语料、内存Qdrant和假向量/评分，无收费调用。"""

import builtins
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from shutil import copytree
import socket
import sys

import pytest
from qdrant_client import QdrantClient

from evals import final_selection, holdout
from evals.baseline_config import fingerprint
from evals.metrics import EvaluationError
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.rerank import RerankError


ROOT = Path(__file__).resolve().parents[1]
read = lambda p: json.loads(p.read_text())
rows = lambda p: [json.loads(line) for line in p.read_text().splitlines()]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original_open, original_import = Path.open, builtins.__import__

    def guarded_open(path, *args, **kwargs):
        assert path.name != ".env", "D17离线测试不得读取真实密钥"
        return original_open(path, *args, **kwargs)

    def guarded_import(name, *args, **kwargs):
        assert name.split(".")[0] not in {"torch", "transformers"}, "离线测试不得启动模型"
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("离线测试不得访问网络")

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def case(tmp_path):
    dataset = tmp_path / "dataset"
    copytree(ROOT / "evals/datasets/copilot_v0", dataset)
    config = tmp_path / "final.json"
    final_selection.freeze_selection(dataset, config)
    frozen, questions, labels, catalog, manifest = holdout.prepare_inputs(dataset, config)
    client = QdrantClient(":memory:")
    client.info = lambda: type("Info", (), {"version": "in-memory-test"})()
    store = DenseQdrantStore(client, collection_name=manifest.spec.collection_name, dimensions=2048)
    store.ensure_collection()
    chunks = [item.chunk for item in catalog.values()]
    store.upsert(chunks, [[1.0, *([0.0] * 2047)] for _ in chunks], dense_index_version=manifest.spec.index_version)
    yield {"dataset": dataset, "config": config, "output": tmp_path / "holdout-test", "client": client,
           "questions": questions, "labels": labels, "catalog": catalog, "frozen": frozen}
    client.close()


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def stats(self):
        return {"hit_texts": 0, "miss_texts": 10 * len(self.calls), "api_calls": len(self.calls)}

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        return [[1.0, i / 100, *([0.0] * 2046)] for i, _ in enumerate(texts)]


class FakeScorer:
    def __init__(self, fail_at=()):
        self.calls, self.fail_at = [], set(fail_at)
        self.last_load_ms = self.last_total_ms = self.last_inference_ms = 0.0
        self.last_diagnostics = []

    def score(self, query, passages):
        self.calls.append((query, list(passages)))
        self.last_load_ms = 1000.0 if len(self.calls) == 1 else 0.0
        self.last_inference_ms, self.last_total_ms = 2.0, self.last_load_ms + 2.0
        self.last_diagnostics = [{"original_pair_tokens": 100, "input_tokens": 100,
                                  "passage_truncated": False} for _ in passages]
        if len(self.calls) in self.fail_at:
            raise RerankError("合成失败，不回退或重试")
        return [float(int(sha256(p.encode()).hexdigest()[:6], 16) % 31) for p in passages]


def collect(case, scorer=None, embeddings=None):
    return holdout.collect_holdout(case["dataset"], case["config"], case["output"],
           client=case["client"], scorer=scorer or FakeScorer(), embeddings=embeddings or FakeEmbeddings())


def reseal(output):
    receipt = read(output / "manifest.json")
    receipt["files"] = {p: sha256((output / p).read_bytes()).hexdigest() for p in receipt["files"]}
    holdout._write(output / "manifest.json", receipt)


def test_selection_replays_five_dev_choices_and_keeps_prompt(case):
    value = final_selection.verify_selection(case["dataset"], case["config"])["selection"]
    assert value["selected"] == "hybrid_rerank_v3" and len(value["candidates"]) == 5
    assert value["candidates"]["chunk500_v1"]["at_5"]["evidence_recall"] < 1
    assert value["candidates"]["hybrid_rerank_v3"]["at_3"]["evidence_recall"] < 1
    baseline = read(ROOT / "evals/results/dense-v0-20261019-01/baseline_config.json")
    assert value["baseline"] == baseline and value["answer_top_k"] == 5
    assert value["selection_split"] == "dev" and value["answer_evaluation"] is False


def test_freeze_refuses_overwrite(case):
    original = case["config"].read_bytes()
    with pytest.raises(EvaluationError):
        final_selection.freeze_selection(case["dataset"], case["config"])
    assert case["config"].read_bytes() == original


def test_freeze_cannot_write_into_frozen_dataset(case):
    with pytest.raises(EvaluationError):
        final_selection.freeze_selection(case["dataset"], case["dataset"] / "new-config.json")
    assert not (case["dataset"] / "new-config.json").exists()


@pytest.mark.parametrize("field", ["selected", "answer_top_k", "dense_candidates", "rerank"])
def test_resealed_selection_cannot_retune_after_holdout(case, field):
    value = read(case["config"])
    value["selection"][field] = {"selected": "hybrid_v2", "answer_top_k": 3,
                                  "dense_candidates": 20, "rerank": {}}[field]
    value["fingerprint"] = final_selection.PREFIX + fingerprint({k: v for k, v in value.items() if k != "fingerprint"})
    holdout._write(case["config"], value)
    with pytest.raises(EvaluationError):
        final_selection.verify_selection(case["dataset"], case["config"])


def test_plan_does_not_load_model_or_request_embeddings(case):
    plan = holdout.plan_holdout(case["dataset"], case["config"])
    assert plan["questions"] == 10 and plan["local_score_calls"] == 11 and plan["pairs"] == 110
    assert plan["max_embedding_api_calls"] == 1 and plan["model_loaded"] is False
    assert not case["output"].exists()


def test_shared_query_vectors_original_candidates_and_complete_outputs(case, monkeypatch):
    embeddings, scorer = FakeEmbeddings(), FakeScorer()
    queries = []
    original = case["client"].query_points

    def query_points(*args, **kwargs):
        queries.append(deepcopy(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(case["client"], "query_points", query_points)
    result = collect(case, scorer, embeddings)
    assert result["complete"] and result["questions"] == 10
    assert result["local_score_calls"] == 11 and result["scored_pairs"] == 110
    assert len(embeddings.calls) == 1 and len(embeddings.calls[0]) == len(queries) == 10
    assert len(scorer.calls) == 11 and all(len(passages) == 10 for _, passages in scorer.calls)
    assert holdout.verify_holdout(case["dataset"], case["output"])["state"] == "verified"
    split = read(case["dataset"] / "split_manifest.json")
    final = rows(case["output"] / "final_rankings.jsonl")
    assert [r["question_id"] for r in final] == split["holdout"]
    assert not set(r["question_id"] for r in final) & set(split["dev"])
    for fused, after in zip(rows(case["output"] / "hybrid_rankings.jsonl"), final, strict=True):
        key = lambda h: h["metadata"]["chunk_id"]
        assert {key(h): h for h in fused["hits"]} == {key(h): h for h in after["hits"]}
    assert read(case["output"] / "runtime.json")["injected_scorer"] is True


def test_metrics_keep_no_answer_separate_and_do_not_claim_refusal(case):
    collect(case)
    summary = read(case["output"] / "final_summary.json")
    assert summary["question_count"] == 10
    assert summary["answerable_count"] == 9 and summary["unanswerable_count"] == 1
    assert summary["unanswerable"]["refusal_accuracy"] is None
    assert read(case["output"] / "status.json")["day17_accepted"] is False


@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_warmup_and_first_two_smoke_failures_stop_without_sealing(case, fail_at):
    scorer, embeddings = FakeScorer([fail_at]), FakeEmbeddings()
    with pytest.raises(EvaluationError):
        collect(case, scorer, embeddings)
    assert len(scorer.calls) == fail_at
    assert len(embeddings.calls) == (0 if fail_at == 1 else 1)
    assert not (case["output"] / "manifest.json").exists()
    assert read(case["output"] / "status.json")["state"] == "partial"


def test_later_failure_stays_in_denominator_without_fallback(case):
    result = collect(case, FakeScorer([5]))
    assert result["complete"] and result["questions"] == 10
    assert result["successful_score_calls"] == 10 and result["attempted_pairs"] == 110
    failed = rows(case["output"] / "final_scored.jsonl")[3]
    assert failed["error"] is not None and failed["hits"] == []
    assert all(not value for values in failed["metrics"]["at_k"].values() for value in values.values())
    assert holdout.verify_holdout(case["dataset"], case["output"])["state"] == "verified"


def test_failed_embedding_request_is_still_counted_and_never_retried(case):
    class FailingEmbeddings(FakeEmbeddings):
        def embed_documents(self, texts):
            self.calls.append(list(texts))
            raise RuntimeError("合成上游失败")

    embeddings = FailingEmbeddings()
    with pytest.raises(RuntimeError):
        collect(case, embeddings=embeddings)
    status = read(case["output"] / "status.json")
    assert status["embedding_api_calls"] == 1 and status["dense_queries"] == 0
    assert status["state"] == "partial" and not (case["output"] / "manifest.json").exists()
    assert len(embeddings.calls) == 1


@pytest.mark.parametrize("change", ["missing", "extra", "bytes", "metrics", "query", "logit", "budget", "bool_count", "case", "timing"])
def test_verify_rejects_byte_and_resealed_semantic_tampering(case, change):
    collect(case)
    output = case["output"]
    if change == "missing":
        (output / "comparison.json").unlink()
    elif change == "extra":
        (output / "extra.txt").write_text("unregistered")
    elif change == "bytes":
        with (output / "comparison.json").open("a") as stream:
            stream.write("\n")
    else:
        if change == "metrics":
            value = read(output / "final_summary.json"); value["answerable_count"] = 8
            holdout._write(output / "final_summary.json", value)
        elif change in {"query", "timing"}:
            value = rows(output / "final_rankings.jsonl")
            if change == "query": value[0]["query"] += "changed"
            else: value[0]["timings_ms"]["total"] += 1
            (output / "final_rankings.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in value))
        elif change == "logit":
            value = rows(output / "score_details.jsonl"); value[0]["candidates"][0]["raw_logit"] = 1e9
            (output / "score_details.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in value))
        elif change in {"budget", "bool_count"}:
            value = read(output / "status.json")
            if change == "budget": value["attempted_pairs"] = 120
            else: value["embedding_api_calls"] = True
            holdout._write(output / "status.json", value)
        else:
            value = rows(output / "case_details.jsonl"); value[0]["gold"] = []
            (output / "case_details.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in value))
        reseal(output)
    with pytest.raises((EvaluationError, ValueError)):
        holdout.verify_holdout(case["dataset"], output)


def test_existing_output_refused_before_embedding_or_score(case):
    case["output"].mkdir()
    embeddings, scorer = FakeEmbeddings(), FakeScorer()
    with pytest.raises(EvaluationError):
        collect(case, scorer, embeddings)
    assert not embeddings.calls and not scorer.calls


def test_collect_cli_requires_live_before_reading_input_or_settings(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["holdout", "collect", "--dataset", "/missing"])
    assert holdout.main() == 1
    assert "--live" in capsys.readouterr().out
