"""D17只依据冻结开发集选择检索方案；先写入不可覆盖配置，再接触保留集。"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from evals.baseline_config import fingerprint
from evals.freeze_dataset import verify_freeze
from evals.metrics import build_chunk_catalog, evaluate_ranking
from evals.rerank_run import DATASET_FINGERPRINT, _digest, _write
from evals.run import _aggregate, _check, _code_state, _json, _read, _rows, _snapshot_row
from project_copilot.config import PROJECT_ROOT
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig


RUNS = {
    "v0": "dense-v0-20261019-01",
    "v1": "dense-v1-size500-20261021-01",
    "v2": "hybrid-v2-20261022-01",
    "v3": "rerank-v3-20261025-01",
}
OPTIONS = {
    "dense_v0": ("v0", "rankings.jsonl", "retrieval"),
    "chunk500_v1": ("v1", "candidate_rankings.jsonl", "candidate_retrieval"),
    "bm25_v2": ("v2", "bm25_rankings.jsonl", "bm25_retrieval"),
    "hybrid_v2": ("v2", "hybrid_rankings.jsonl", "hybrid_retrieval"),
    "hybrid_rerank_v3": ("v3", "rerank_rankings.jsonl", "rerank_retrieval"),
}
PREFIX = "final-selection-v1-"


def _sealed(directory: Path, name: str) -> dict:
    receipt = _json(_read(directory / name))
    for rel, expected in receipt["files"].items():
        path = directory / rel
        _check(path.resolve().is_relative_to(directory.resolve()) and _digest(path) == expected,
               "开发归档产物缺失或已变化")
    return receipt


def selection_body(dataset_dir: Path) -> dict:
    """读取全部冻结文件以验SHA，但只将dev题的排名/标签用于决策。"""
    dataset = Path(dataset_dir)
    _check(verify_freeze(dataset)["fingerprint"] == DATASET_FINGERPRINT, "不是原D8冻结数据集")
    dev = _json(_read(dataset / "split_manifest.json"))["dev"]
    questions = {q["question_id"]: q for q in _rows(dataset / "questions.jsonl") if q["question_id"] in dev}
    labels = {q["question_id"]: q for q in _rows(dataset / "labels.draft.jsonl") if q["question_id"] in dev}
    _check(len(dev) == 30 and sum(label["answerable"] for label in labels.values()) == 26,
           "必须使用原30道dev及26道可答题")
    documents = [load_document(dataset / s["path"]) for s in _rows(dataset / "sources.jsonl")]
    result_root = PROJECT_ROOT / "evals/results"
    manifests = {}
    for key, run in RUNS.items():
        name = "archive_manifest.json" if key == "v0" else "manifest.json"
        _sealed(result_root / run, name)
        manifests[key] = {"run_id": run, "manifest": name,
                          "sha256": _digest(result_root / run / name)}
    candidates = {}
    for option, (key, ranking_name, scoring_dir) in OPTIONS.items():
        directory = result_root / RUNS[key]
        evaluation = _json(_read(directory / scoring_dir / "config.json"))["evaluation_config"]
        _check(evaluation["split"] == "dev" and evaluation["dataset_fingerprint"] == DATASET_FINGERPRINT
               and evaluation["ks"] == [1, 3, 5, 10], "开发评分合同不一致")
        catalog = build_chunk_catalog(documents, SplitConfig.model_validate(evaluation["splitter"]))
        rankings = _rows(directory / ranking_name)
        _check([r["question_id"] for r in rankings] == dev, "选型不得混入保留题或缩小开发集")
        records = []
        for row in rankings:
            qid = row["question_id"]
            hits, times, error = _snapshot_row(row, questions[qid])
            records.append({"question_id": qid, "hits": hits, "timings_ms": times, "error": error,
                            "metrics": evaluate_ranking(hits, labels[qid], catalog)})
        aggregate = _aggregate(records, [1, 3, 5, 10])
        saved = _json(_read(directory / scoring_dir / "summary.json"))
        _check(all(saved.get(k) == v for k, v in aggregate.items()), "开发汇总与原文gold重评分不一致")
        candidates[option] = {"source_run_id": RUNS[key], "ranking_sha256": _digest(directory / ranking_name),
                              "errors": aggregate["retrieval_error_count"],
                              "at_3": aggregate["at_k"]["3"], "at_5": aggregate["at_k"]["5"]}
    # 规则写于既有dev实验之后、保留集运行之前；不是实验前预注册。
    v3 = result_root / RUNS["v3"]
    latency = _json(_read(v3 / "rerank_retrieval/summary.json"))["timings_ms"]["rerank"]["median"]
    observations = [_json(_read(v3 / "warmup.json")), *_rows(v3 / "score_details.jsonl")]
    memory = max(row["gpu"]["max_reserved_bytes"] for row in observations)
    admissible = [name for name, data in candidates.items() if data["errors"] == 0
                  and (name != "hybrid_rerank_v3" or (latency <= 250 and memory <= 2 * 1024**3))]
    selected = max(admissible, key=lambda name: tuple(candidates[name]["at_5"][field]
                   for field in ("evidence_recall", "all_evidence_covered", "evidence_mrr")) + (name,))
    _check(selected == "hybrid_rerank_v3", "开发结果不再支持已实现的D17候选；不得临时改变保留集路线")
    baseline = _json(_read(result_root / RUNS["v0"] / "baseline_config.json"))
    parameters = _json(_read(PROJECT_ROOT / "evals/configs/rerank_v3.json"))
    return {
        "dataset_fingerprint": DATASET_FINGERPRINT, "selection_split": "dev",
        "selection_rule": "zero-errors; v3-local-rerank-median<=250ms,reserved<=2GiB; lexicographic-max-evidence-recall,all-evidence,mrr-at-fixed-k5; name-breaks-ties",
        "rule_timing": "written-after-existing-dev-experiments; frozen-before-holdout; not-preregistered-before-dev",
        "selected": selected, "candidates": candidates, "dev_sources": manifests,
        "resource_basis": {"rerank_median_ms": latency, "max_reserved_bytes": memory,
                           "scope": "D16 rerank component only; no end-to-end SLA"},
        "baseline": baseline, "splitter": baseline["evaluation_config"]["splitter"],
        "dense_candidates": 10, "bm25_candidates": 10, "ranking_depth": 10,
        "rank_constant": 60, "answer_top_k": 5, "rerank": parameters["rerank"],
        "answer_evaluation": False, "holdout_policy": "compare-v0-vs-selected-once; no-retuning; failures-stay-in-denominator",
        "deployment": "frozen-experiment-profile; existing-HTTP-API-remains-Dense",
    }


def freeze_selection(dataset_dir: Path, output: Path) -> dict:
    output = Path(output)
    _check(not output.exists() and not output.is_symlink(), "冻结配置已存在，拒绝覆盖")
    _check(not output.resolve().is_relative_to(Path(dataset_dir).resolve()), "最终配置不能写入冻结数据集")
    body = selection_body(dataset_dir)
    value = {"schema_version": 1, "frozen_at": datetime.now(timezone.utc).isoformat(),
             "code": _code_state(), "selection": body}
    value["fingerprint"] = PREFIX + fingerprint(value)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write(output, value)
    return {"status": "ok", "state": "frozen", "selected": body["selected"],
            "fingerprint": value["fingerprint"], "holdout_queries": 0, "model_calls": 0}


def verify_selection(dataset_dir: Path, config_path: Path) -> dict:
    value = _json(_read(Path(config_path)))
    _check(set(value) == {"schema_version", "frozen_at", "code", "selection", "fingerprint"}
           and type(value["schema_version"]) is int and value["schema_version"] == 1
           and value["fingerprint"] == PREFIX + fingerprint({k: v for k, v in value.items() if k != "fingerprint"}),
           "最终配置指纹或字段不匹配")
    _check(value["selection"] == selection_body(dataset_dir), "最终配置不匹配冻结开发证据，禁止依保留集改选")
    return value


def main() -> int:
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["freeze", "verify"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze_selection(args.dataset, args.config)
        else:
            value = verify_selection(args.dataset, args.config)
            result = {"status": "ok", "state": "verified", "fingerprint": value["fingerprint"]}
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "error", "message": "最终方案冻结或验证失败，未进行保留集查询"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
