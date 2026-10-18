"""D9离线排名重放；不调用模型或向量库，不把固定样本冒充真实Dense基线。"""

import argparse
from importlib.metadata import version
from hashlib import sha256
import json
import math
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time

from pydantic import ValidationError

from evals.freeze_dataset import verify_freeze
from evals.metrics import EvaluationError, build_chunk_catalog, evaluate_ranking
from evals.validate_dataset import DatasetValidationError
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import RetrievedChunk


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHASES = ("embedding", "search", "fusion", "rerank", "total")
CONFIG_FIELDS = {"schema_version", "dataset_fingerprint", "split", "question_ids", "ks", "splitter", "ranking_origin"}
ROW_FIELDS = {"question_id", "query", "hits", "timings_ms", "error"}
MAPPING_RULE = "exact-quote-contiguous-union-v1"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationError(message)


def _unique_object(pairs: list[tuple]) -> dict:
    value = {}
    for key, item in pairs:
        _check(key not in value, "JSON对象含重复字段")
        value[key] = item
    return value


def _json(text: str) -> dict:
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise EvaluationError("评测输入不是有效JSON对象或含重复字段") from None
    _check(isinstance(value, dict), "评测输入必须为JSON对象")
    return value


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise EvaluationError("无法读取UTF-8评测输入文件") from None


def _rows(path: Path) -> list[dict]:
    lines = _read(path).splitlines()
    _check(bool(lines), "排名或数据JSONL不能为空")
    return [_json(line) for line in lines]


def _config(path: Path) -> tuple[dict, SplitConfig]:
    value = _json(_read(path))
    _check(set(value) == CONFIG_FIELDS and type(value["schema_version"]) is int
           and value["schema_version"] == 1, "评测配置字段或版本无效")
    _check(value["split"] == "dev", "D9仅评测开发集，保留集等D17冻结比较")
    _check(value["ranking_origin"] in ("synthetic_fixture", "recorded_retrieval"), "必须明确排名来源")
    ks = value["ks"]
    _check(isinstance(ks, list) and bool(ks)
           and all(type(k) is int and 1 <= k <= 100 for k in ks)
           and ks == sorted(set(ks)), "k须为升序不重复的1至100整数")
    splitter = value["splitter"]
    _check(isinstance(splitter, dict) and set(splitter) == {"chunk_size", "chunk_overlap", "separators"},
           "必须完整记录切分配置")
    try:
        return value, SplitConfig.model_validate(splitter)
    except ValidationError:
        raise EvaluationError("切分配置无效") from None


def _snapshot_row(value: dict, question: dict) -> tuple[list[RetrievedChunk], dict, dict | None]:
    _check(set(value) == ROW_FIELDS, "排名快照字段不完整或含未知字段")
    query = question["standalone_question"] or question["question"]
    _check(value["query"] == query, "快照query必须与冻结问题或追问独立问题一致")
    timings = value["timings_ms"]
    _check(isinstance(timings, dict) and set(timings) == set(PHASES), "必须明确各阶段耗时，未采集记null")
    for elapsed in timings.values():
        _check(elapsed is None or (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0),
               "阶段耗时须为有限非负毫秒或null")
    error = value["error"]
    _check(error is None or (isinstance(error, dict) and set(error) == {"type", "message"}
           and all(isinstance(v, str) and bool(v.strip()) and len(v) <= 2000 for v in error.values())),
           "错误须记录类型及说明，成功时记null")
    _check(isinstance(value["hits"], list), "hits必须为有序列表")
    _check(error is None or not value["hits"], "检索失败快照不得同时声称有成功排名")
    try:
        hits = [RetrievedChunk.model_validate(hit) for hit in value["hits"]]
    except (ValidationError, ValueError):
        raise EvaluationError("排名中的片段字段或分数无效") from None
    return hits, timings, error


def _aggregate(records: list[dict], ks: list[int]) -> dict:
    answerable = [r for r in records if r["metrics"]["answerable"]]
    no_answer = [r for r in records if not r["metrics"]["answerable"]]
    aggregate = {}
    for k in ks:
        fields = ("document_recall", "document_mrr", "evidence_recall", "evidence_mrr", "all_evidence_covered")
        # 失败题已按空排名计0，继续留在可回答题分母；无答案指标为null并排除。
        aggregate[str(k)] = {
            field: statistics.mean(float(row["metrics"]["at_k"][str(k)][field]) for row in answerable)
            if answerable else None for field in fields
        }
    timings = {}
    for phase in PHASES:
        values = sorted(r["timings_ms"][phase] for r in records if r["timings_ms"][phase] is not None)
        timings[phase] = {"samples": len(values), "median": statistics.median(values) if values else None,
                          "p95": values[math.ceil(0.95 * len(values)) - 1] if values else None}
    return {"question_count": len(records), "answerable_count": len(answerable),
            "unanswerable_count": len(no_answer),
            "retrieval_error_count": sum(r["error"] is not None for r in records),
            "at_k": aggregate, "timings_ms": timings,
            "unanswerable": {"count": len(no_answer),
                             "empty_hits": sum(not r["hits"] and r["error"] is None for r in no_answer),
                             "nonempty_hits": sum(bool(r["hits"]) for r in no_answer),
                             "retrieval_errors": sum(r["error"] is not None for r in no_answer),
                             "refusal_accuracy": None},
            "bad_cases": [{"question_id": r["question_id"],
                           "reason": "retrieval_error" if r["error"] else "incomplete_evidence_at_max_k"}
                          for r in records if r["error"] is not None or (r["metrics"]["answerable"]
                          and not r["metrics"]["at_k"][str(max(ks))]["all_evidence_covered"])]}


def _code_state() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT,
                                capture_output=True, text=True, check=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                                   capture_output=True, text=True, check=True, timeout=5).stdout)
        return {"commit": commit, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def run_evaluation(dataset_dir: Path, config_path: Path, rankings_path: Path, output_dir: Path) -> dict:
    """验证冻结身份后重放；缺题或非法输入直接拒绝，不偷偷缩小实验分母。"""
    root = Path(dataset_dir).resolve()
    output = Path(output_dir)
    _check(not output.exists() and not output.is_symlink(), "结果目录已存在，拒绝覆盖")
    _check(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name)), "run_id目录名无效")
    _check(not output.resolve().is_relative_to(root), "结果目录不能写入冻结数据集")
    try:
        frozen = verify_freeze(root)
    except DatasetValidationError:
        raise EvaluationError("数据集未冻结或冻结校验失败") from None
    config, split_config = _config(config_path)
    _check(config["dataset_fingerprint"] == frozen["fingerprint"], "配置与冻结数据指纹不匹配")
    split = _json(_read(root / "split_manifest.json"))
    ids = config["question_ids"]
    if ids is None:
        ids = split["dev"]
    _check(isinstance(ids, list) and bool(ids) and all(isinstance(qid, str) and qid in split["dev"] for qid in ids)
           and len(set(ids)) == len(ids), "所选问题须为不重复的开发集ID")
    questions = {q["question_id"]: q for q in _rows(root / "questions.jsonl")}
    labels = {q["question_id"]: q for q in _rows(root / "labels.draft.jsonl")}
    documents = [load_document(root / s["path"]) for s in _rows(root / "sources.jsonl")]
    catalog = build_chunk_catalog(documents, split_config)
    snapshots = {}
    for row in _rows(rankings_path):
        qid = row.get("question_id")
        _check(isinstance(qid, str) and qid in ids and qid not in snapshots,
               "排名包含重复、未知或未选择的问题ID")
        snapshots[qid] = row
    _check(set(snapshots) == set(ids), "排名快照须完整覆盖所选问题，不能遗漏失败题")

    records = []
    for qid in ids:
        question, label = questions[qid], labels[qid]
        hits, timings, error = _snapshot_row(snapshots[qid], question)
        started = time.monotonic()
        metrics = evaluate_ranking(hits, label, catalog, tuple(config["ks"]))
        records.append({"question_id": qid, "category": question["category"],
                        "question": question["question"], "query": snapshots[qid]["query"],
                        "status": "retrieval_error" if error else "ok", "error": error,
                        "hits": [{"rank": rank, **hit.model_dump(mode="json")} for rank, hit in enumerate(hits, 1)],
                        "timings_ms": timings,
                        "evaluation_ms": (time.monotonic() - started) * 1000, "metrics": metrics})
    shared = {"run_id": output.name, "mode": "offline_replay", "ranking_origin": config["ranking_origin"],
              "dataset_fingerprint": frozen["fingerprint"], "split": "dev",
              "available_dev_questions": len(split["dev"]), "mapping_rule": MAPPING_RULE,
              "timing_source": "snapshot_reported_ms; evaluation_ms_is_local_scoring_only"}
    summary = {**shared, **_aggregate(records, config["ks"])}
    receipt = {**shared, "evaluation_config": config, "effective_question_ids": ids,
               "input_sha256": {"config": sha256(Path(config_path).read_bytes()).hexdigest(),
                                "rankings": sha256(Path(rankings_path).read_bytes()).hexdigest()},
               "splitter_version": split_config.index_version, "catalog_chunks": len(catalog),
               "code": _code_state(), "runtime": {"python": platform.python_version(),
               "system": platform.system(), "machine": platform.machine(),
               "langchain_text_splitters": version("langchain-text-splitters"), "pydantic": version("pydantic")},
               "model_calls": 0, "index_manifest": None, "prompt_config": None,
               "cache_condition": "not_applicable_to_offline_replay", "warmup": None}
    try:
        output.mkdir(parents=True, exist_ok=False)
        (output / "config.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "retrieval.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        raise EvaluationError("结果写入失败，不能将未完整保存的目录视为成功运行") from None
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dataset", "config", "rankings", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_evaluation(args.dataset, args.config, args.rankings, args.output_dir)
    except (EvaluationError, OSError, ValueError):
        # 输入错误不输出原问题、快照正文、凭据或绝对路径。
        print(json.dumps({"status": "error", "message": "评测输入或输出校验失败；请检查冻结、配置和排名快照"}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
