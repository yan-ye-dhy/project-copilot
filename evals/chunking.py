"""D11单因素切分边界检查；只读冻结语料和V0归档，不生成向量或排名。"""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import statistics

from evals.baseline_config import validate_baseline_config
from evals.metrics import EvaluationError, LocatedChunk, build_chunk_catalog
from evals.run import _json, _read, _rows
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig


CANDIDATE_FIELDS = {
    "schema_version", "candidate_id", "baseline_run_id", "baseline_archive_sha256",
    "baseline_configuration_fingerprint", "changed_factor", "splitter", "focus_question_ids",
}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationError(message)


def _stats(catalog: dict[str, LocatedChunk]) -> dict:
    lengths = [len(item.chunk.text) for item in catalog.values()]
    return {
        "chunk_count": len(lengths), "total_chunk_chars": sum(lengths),
        "min_chunk_chars": min(lengths), "max_chunk_chars": max(lengths),
        "median_chunk_chars": statistics.median(lengths),
    }


def _boundary(unit: dict, catalog: dict[str, LocatedChunk]) -> dict:
    """列出原文交集与单块完整包含，不把部分交集当作完整gold或真实命中。"""
    intersections, containing = [], []
    for chunk_id, located in catalog.items():
        metadata = located.chunk.metadata
        if (str(metadata.document_id) != unit["document_id"]
                or metadata.content_hash != unit["document_version"]
                or metadata.page != unit["page"]):
            continue
        if max(located.start_char, unit["start_char"]) >= min(located.end_char, unit["end_char"]):
            continue
        intersections.append({
            "chunk_id": chunk_id, "source_name": metadata.source_name, "page": metadata.page,
            "start_char": located.start_char, "end_char": located.end_char,
            "chars": len(located.chunk.text), "text": located.chunk.text,
        })
        if located.start_char <= unit["start_char"] and located.end_char >= unit["end_char"]:
            containing.append(chunk_id)
    return {"intersecting_chunks": intersections, "single_chunk_covering_ids": containing}


def inspect_chunking(dataset_dir: Path, baseline_dir: Path, candidate_path: Path) -> dict:
    """复用D3切分/D9原文定位；D8冻结校验由D10配置校验复用，不读取.env。"""
    dataset, baseline = Path(dataset_dir), Path(baseline_dir)
    candidate = _json(_read(candidate_path))
    _check(set(candidate) == CANDIDATE_FIELDS
           and type(candidate["schema_version"]) is int and candidate["schema_version"] == 1,
           "候选配置字段或版本无效")
    _check(isinstance(candidate["candidate_id"], str) and bool(candidate["candidate_id"].strip()),
           "候选必须有独立名称")
    archive_bytes = (baseline / "archive_manifest.json").read_bytes()
    _check(sha256(archive_bytes).hexdigest() == candidate["baseline_archive_sha256"],
           "V0归档清单与候选绑定的版本不一致")
    archive = _json(archive_bytes.decode("utf-8"))
    _check(archive.get("run_id") == candidate["baseline_run_id"], "V0运行身份不一致")
    # 只核验本工具实际消费的三个归档输入；完整基线核验仍使用D10工具。
    for name in ("baseline_config.json", "bad_cases.jsonl", "retrieval/retrieval.jsonl"):
        _check(sha256((baseline / name).read_bytes()).hexdigest() == archive["files"][name],
               "V0边界检查输入已变化")
    config = validate_baseline_config(_json(_read(baseline / "baseline_config.json")), dataset)
    _check(config["configuration_fingerprint"] == candidate["baseline_configuration_fingerprint"],
           "候选绑定的V0配置指纹不一致")
    original = SplitConfig.model_validate(config["evaluation_config"]["splitter"])
    _check(isinstance(candidate["splitter"], dict)
           and set(candidate["splitter"]) == set(original.model_dump()), "候选须记录完整切分参数")
    proposed = SplitConfig.model_validate(candidate["splitter"])
    changed = [key for key in original.model_dump()
               if original.model_dump()[key] != proposed.model_dump()[key]]
    _check(candidate["changed_factor"] == "chunk_size" and changed == ["chunk_size"],
           "D11候选只允许改变chunk_size，不得同时改变overlap或分隔符")
    _check(original.index_version == config["index"]["splitter_version"], "V0切分实现版本已漂移")

    split = _json(_read(dataset / "split_manifest.json"))
    ids = candidate["focus_question_ids"]
    _check(isinstance(ids, list) and bool(ids) and all(isinstance(qid, str) for qid in ids)
           and len(ids) == len(set(ids)) and set(ids) <= set(split["dev"]),
           "关注题必须是非空不重复的开发集ID，不能使用保留集")
    bad_ids = {row["question_id"] for row in _rows(baseline / "bad_cases.jsonl")
               if row["stage"] == "retrieval"}
    _check(set(ids) <= bad_ids, "候选须从V0真实检索Bad Case出发")
    questions = {row["question_id"]: row for row in _rows(dataset / "questions.jsonl")
                 if row["question_id"] in ids}
    labels = {row["question_id"]: row for row in _rows(dataset / "labels.draft.jsonl")
              if row["question_id"] in ids}
    records = {row["question_id"]: row for row in _rows(baseline / "retrieval/retrieval.jsonl")
               if row["question_id"] in ids}
    documents = [load_document(dataset / source["path"])
                 for source in _rows(dataset / "sources.jsonl")]
    catalogs = {"baseline": build_chunk_catalog(documents, original),
                "candidate": build_chunk_catalog(documents, proposed)}
    cases = []
    for qid in ids:
        question, record = questions[qid], records[qid]
        cases.append({
            "question_id": qid, "query": question["standalone_question"] or question["question"],
            "baseline_evidence_completion_ranks": record["metrics"]["evidence_completion_ranks"],
            "evidence": [{"gold": unit, **{name: _boundary(unit, catalog)
                         for name, catalog in catalogs.items()}} for unit in labels[qid]["evidence"]],
        })
    return {
        "status": "ok", "mode": "offline_boundary_inspection", "candidate_id": candidate["candidate_id"],
        "candidate_file_sha256": sha256(Path(candidate_path).read_bytes()).hexdigest(),
        "dataset_fingerprint": config["dataset_fingerprint"], "split": "dev",
        "baseline_run_id": candidate["baseline_run_id"],
        "baseline_configuration_fingerprint": config["configuration_fingerprint"],
        "changed_factor": "chunk_size", "model_calls": 0, "qdrant_requests": 0,
        "retrieval_evaluation": None, "answer_evaluation": None,
        "splitters": {"baseline": original.model_dump(mode="json"), "candidate": proposed.model_dump(mode="json")},
        "splitter_versions": {"baseline": original.index_version, "candidate": proposed.index_version},
        "corpus": {"documents": len(documents),
                   "original_chars": sum(len(page.text) for doc in documents for page in doc.pages)},
        "stats": {name: _stats(catalog) for name, catalog in catalogs.items()}, "cases": cases,
        "limitation": "仅原文边界检查；单块包含gold不代表会被检索到，V1排名、Recall/MRR、延迟及费用待D12真实对照。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--baseline-dir", type=Path, required=True, help="D10公开归档目录")
    parser.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect_chunking(args.dataset, args.baseline_dir, args.candidate)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "无法读取完整的边界检查输入"
        print(json.dumps({"status": "error", "message": message}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
