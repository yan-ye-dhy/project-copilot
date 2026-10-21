"""比较两个已评分的真实开发集排名；只读归档，不执行检索或生成回答。"""

import math
from pathlib import Path
import statistics

from evals.metrics import EvaluationError
from evals.run import PHASES, _aggregate, _check, _json, _read, _rows


KS = (1, 3, 5, 10)
FIELDS = ("document_recall", "document_mrr", "evidence_recall", "evidence_mrr", "all_evidence_covered")


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _load(directory: Path) -> tuple[dict, list[dict], dict]:
    receipt = _json(_read(directory / "config.json"))
    summary = _json(_read(directory / "summary.json"))
    rows = _rows(directory / "retrieval.jsonl")
    config = receipt.get("evaluation_config", {})
    _check(isinstance(config, dict) and config.get("ks") == list(KS)
           and all(type(k) is int for k in config["ks"]), "D12对照须完整包含k=1、3、5、10")
    for key in ("ranking_origin", "dataset_fingerprint", "split"):
        _check(receipt.get(key) == config.get(key) == summary.get(key), "评分归档与配置身份不一致")
    _check(config.get("ranking_origin") == "recorded_retrieval" and config.get("split") == "dev",
           "只能对照真实开发集检索，不能使用模拟排名或保留集")
    _check(isinstance(config.get("dataset_fingerprint"), str) and bool(config["dataset_fingerprint"])
           and receipt.get("mapping_rule") == summary.get("mapping_rule")
           and isinstance(receipt.get("mapping_rule"), str), "缺少冻结指纹或gold映射规则")
    ids = receipt.get("effective_question_ids")
    _check(isinstance(ids, list) and bool(ids) and all(isinstance(qid, str) for qid in ids)
           and len(set(ids)) == len(ids) and [row.get("question_id") for row in rows] == ids,
           "评分记录必须按原顺序完整覆盖问题，不能漏掉失败题")
    for row in rows:
        metrics = row.get("metrics", {})
        _check(isinstance(metrics, dict) and type(metrics.get("answerable")) is bool
               and isinstance(metrics.get("at_k"), dict) and set(metrics["at_k"]) == set(map(str, KS)),
               "逐题评分缺少可回答状态或完整k指标")
        for values in metrics["at_k"].values():
            _check(isinstance(values, dict) and set(values) == set(FIELDS), "逐题指标字段不完整")
            for field, value in values.items():
                valid = value is None if not metrics["answerable"] else (
                    type(value) is bool if field == "all_evidence_covered" else _number(value) and 0 <= value <= 1)
                _check(valid, "可回答题指标须有效，无答案题指标须为null")
        _check(isinstance(row.get("hits"), list) and "error" in row
               and (row["error"] is None or (isinstance(row["error"], dict) and not row["hits"]))
               and row.get("status") == ("ok" if row["error"] is None else "retrieval_error"),
               "检索错误状态与命中记录冲突")
        times = row.get("timings_ms", {})
        _check(isinstance(times, dict) and set(times) == set(PHASES)
               and all(v is None or (_number(v) and v >= 0) for v in times.values()), "阶段耗时无效")
        if row["error"] is not None and metrics["answerable"]:
            _check(all(not v for values in metrics["at_k"].values() for v in values.values()),
                   "失败题必须保留在分母且所有检索指标计零")
    aggregate = _aggregate(rows, list(KS))
    _check(all(summary.get(key) == value for key, value in aggregate.items()), "汇总与逐题记录不一致")
    return receipt, rows, aggregate


def _outcome(deltas: list[float]) -> str:
    positive, negative = any(v > 0 for v in deltas), any(v < 0 for v in deltas)
    # 不同指标或k一升一降时保留mixed，不能人为选一个指标掩盖退化。
    return "mixed" if positive and negative else "improved" if positive else "regressed" if negative else "same"


def _reasons(row: dict) -> list[str]:
    if row["error"] is not None:
        return ["retrieval_error"]
    if not row["metrics"]["answerable"]:
        return ["unanswerable_nonempty_hits"] if row["hits"] else []
    return [f"incomplete_evidence_at_{k}" for k in KS
            if not row["metrics"]["at_k"][str(k)]["all_evidence_covered"]]


def compare_retrieval(baseline_dir: Path, candidate_dir: Path, categories: dict[str, str]) -> dict:
    """保持失败分母和无答案口径，输出候选减基线；模型/向量身份由采集器另行核验。"""
    base_receipt, baseline, base_summary = _load(Path(baseline_dir))
    cand_receipt, candidate, cand_summary = _load(Path(candidate_dir))
    base_config, cand_config = base_receipt["evaluation_config"], cand_receipt["evaluation_config"]
    _check({k: v for k, v in base_config.items() if k != "splitter"}
           == {k: v for k, v in cand_config.items() if k != "splitter"}, "对照配置只允许切分因素不同")
    _check(base_receipt["effective_question_ids"] == cand_receipt["effective_question_ids"]
           and base_receipt["mapping_rule"] == cand_receipt["mapping_rule"], "问题顺序或gold映射规则不同")
    per_question, bad_cases, category_results = [], {}, {}
    for before, after in zip(baseline, candidate, strict=True):
        qid = before["question_id"]
        category = categories.get(qid)
        _check(isinstance(category, str) and bool(category)
               and before.get("category") == after.get("category") == category, "问题分类缺失或不一致")
        _check(isinstance(before.get("query"), str) and before["query"] == after.get("query"), "对照查询文本不同")
        _check(all(before["metrics"].get(k) == after["metrics"].get(k)
                   for k in ("answerable", "gold_document_count", "gold_evidence_count")), "gold统计或可回答状态不同")
        answerable = before["metrics"]["answerable"]
        deltas = {str(k): {field: float(after["metrics"]["at_k"][str(k)][field])
                                   - float(before["metrics"]["at_k"][str(k)][field]) for field in FIELDS}
                  for k in KS} if answerable else None
        outcome = _outcome([v for values in deltas.values() for v in values.values()]) if answerable else "unanswerable"
        entry = {"question_id": qid, "category": category, "outcome": outcome, "delta_at_k": deltas,
                 "baseline_error": before["error"], "candidate_error": after["error"],
                 "baseline_reasons": _reasons(before), "candidate_reasons": _reasons(after)}
        per_question.append(entry)
        if entry["baseline_reasons"] or entry["candidate_reasons"]:
            bad_cases.setdefault(category, []).append(entry)
        group = category_results.setdefault(category, {"question_count": 0, "outcomes": {},
                                                       "baseline_errors": 0, "candidate_errors": 0})
        group["question_count"] += 1
        group["outcomes"][outcome] = group["outcomes"].get(outcome, 0) + 1
        group["baseline_errors"] += before["error"] is not None
        group["candidate_errors"] += after["error"] is not None
    timings = {}
    for phase in PHASES:
        pairs = [(b["timings_ms"][phase], c["timings_ms"][phase]) for b, c in zip(baseline, candidate, strict=True)
                 if b["timings_ms"][phase] is not None and c["timings_ms"][phase] is not None]
        changes = sorted(c - b for b, c in pairs)
        timings[phase] = {"paired_samples": len(pairs), "excluded_pairs": len(baseline) - len(pairs),
                          "baseline_median_ms": statistics.median(b for b, _ in pairs) if pairs else None,
                          "candidate_median_ms": statistics.median(c for _, c in pairs) if pairs else None,
                          "median_delta_ms": statistics.median(changes) if pairs else None,
                          "p95_delta_ms": changes[math.ceil(.95 * len(changes)) - 1] if pairs else None}
    return {"schema_version": 1, "dataset_fingerprint": base_config["dataset_fingerprint"], "split": "dev",
            "ks": list(KS), "delta_definition": "candidate_minus_baseline", "baseline": base_summary,
            "candidate": cand_summary,
            "delta_at_k": {str(k): {f: cand_summary["at_k"][str(k)][f] - base_summary["at_k"][str(k)][f]
                                      if base_summary["at_k"][str(k)][f] is not None else None for f in FIELDS} for k in KS},
            "retrieval_error_count_delta": cand_summary["retrieval_error_count"] - base_summary["retrieval_error_count"],
            "per_question": per_question, "category_results": category_results, "bad_cases": bad_cases,
            "paired_timings_ms": timings,
            "timing_note": "paired_recorded_samples; different_collection_times_are_not_a_controlled_latency_benchmark",
            "unanswerable": {"baseline": base_summary["unanswerable"], "candidate": cand_summary["unanswerable"],
                             "refusal_accuracy": None}}
