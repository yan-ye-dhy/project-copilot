"""D14：复用原问题向量，同一批候选比较Dense、BM25及应用层RRF，无模型调用。"""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import re
import sys
import time

from qdrant_client import QdrantClient

from evals.baseline import _line, _write, verify_baseline
from evals.baseline_config import fingerprint, validate_baseline_config
from evals.comparison import compare_retrieval
from evals.freeze_dataset import verify_freeze
from evals.metrics import EvaluationError, build_chunk_catalog, evaluate_ranking
from evals.run import PHASES, _code_state, _json, _read, _rows, run_evaluation
from project_copilot.config import PROJECT_ROOT
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.bm25 import BM25Config, BM25Index, ChineseTokenizer, read_completed_chunks
from project_copilot.rag.fusion import reciprocal_rank_fusion
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import IndexManifest


BRANCHES = ("dense", "bm25", "hybrid")
COMPARISONS = (("dense", "bm25"), ("dense", "hybrid"), ("bm25", "hybrid"))
PREFIX = "hybrid-experiment-v1-"


def _check(value: bool, message: str) -> None:
    if not value:
        raise EvaluationError(message)


@dataclass
class Inputs:
    config: dict
    manifest: IndexManifest
    catalog: dict
    questions: list[dict]
    labels: dict
    vectors: dict


def prepare_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path,
                       config_path: Path) -> Inputs:
    """全部离线；公开归档→私有manifest→原问题向量，禁止请求回退。"""
    dataset, baseline, private = map(Path, (dataset_dir, baseline_dir, baseline_private_dir))
    frozen = verify_freeze(dataset)
    archive = _json(_read(baseline / "archive_manifest.json"))
    for name, digest in archive["files"].items():
        path = baseline / name
        _check(path.resolve().is_relative_to(baseline.resolve())
               and sha256(path.read_bytes()).hexdigest() == digest, "公开V0归档缺失或已变化")
    _check(sha256((private / "manifest.json").read_bytes()).hexdigest()
           == archive["source_manifest_sha256"], "私有V0不属于该公开基线")
    verify_baseline(dataset, private)
    original = validate_baseline_config(_json(_read(baseline / "baseline_config.json")), dataset)
    historical = _json(_read(private / "manifest.json"))
    for name, digest in historical["source_sha256"].items():
        path = PROJECT_ROOT / name
        _check(path.resolve().is_relative_to(PROJECT_ROOT)
               and sha256(path.read_bytes()).hexdigest() == digest, "V0原始运行源码已变化")
    descriptor = _json(_read(config_path))
    expected = {
        "schema_version": 1, "experiment_id": "hybrid-v2-rrf60",
        "baseline_configuration_fingerprint": original["configuration_fingerprint"],
        "dataset_fingerprint": frozen["fingerprint"], "dense_candidates": 10,
        "bm25_candidates": 10, "ranking_depth": 10, "answer_top_k": 5, "rank_constant": 60,
    }
    _check(descriptor == expected and all(type(descriptor[k]) is int for k in (
        "schema_version", "dense_candidates", "bm25_candidates", "ranking_depth", "answer_top_k", "rank_constant"
    )), "D14本轮固定750/150、两路各10候选、最终5块及RRF常数60")
    documents = [load_document(dataset / row["path"]) for row in _rows(dataset / "sources.jsonl")]
    catalog = build_chunk_catalog(documents, SplitConfig.model_validate(original["evaluation_config"]["splitter"]))
    manifest = IndexManifest.model_validate_json(_read(baseline / "index_manifest.json"))
    _check({**manifest.spec.model_dump(mode="json"), "index_version": manifest.spec.index_version}
           == original["index"], "成功清单不是冻结V0索引")
    BM25Index.build(manifest, [item.chunk for item in catalog.values()])
    ids = _json(_read(dataset / "split_manifest.json"))["dev"]
    by_id = {q["question_id"]: q for q in _rows(dataset / "questions.jsonl")}
    questions = [by_id[qid] for qid in ids]
    labels = {q["question_id"]: q for q in _rows(dataset / "labels.draft.jsonl") if q["question_id"] in ids}
    vector_path = private / "query_vectors.jsonl"
    rows = _rows(vector_path)
    _check([r.get("question_id") for r in rows] == ids, "须完整同序复用全部dev原问题向量")
    vectors = {}
    for row, question in zip(rows, questions, strict=True):
        _check(set(row) == {"question_id", "query", "vector", "embedding_contract"}
               and row["query"] == (question["standalone_question"] or question["question"])
               and row["embedding_contract"] == original["embedding"], "问题向量文本或Embedding合同不匹配")
        vector = row["vector"]
        _check(isinstance(vector, list) and len(vector) == original["embedding"]["dimensions"]
               and all(type(v) in (int, float) and math.isfinite(v) for v in vector), "问题向量维度或数值无效")
        vectors[row["question_id"]] = vector
    config = {
        "schema_version": 1, "mode": "shared_candidates_three_way", "descriptor": descriptor,
        "baseline": original, "evaluation_config": original["evaluation_config"],
        "dataset_fingerprint": frozen["fingerprint"], "split": "dev",
        "baseline_archive_sha256": sha256((baseline / "archive_manifest.json").read_bytes()).hexdigest(),
        "source_manifest_sha256": archive["source_manifest_sha256"],
        "query_vectors_sha256": sha256(vector_path.read_bytes()).hexdigest(),
        "index_manifest_sha256": sha256((baseline / "index_manifest.json").read_bytes()).hexdigest(),
        "bm25": BM25Config().model_dump(mode="json"), "tokenizer": ChineseTokenizer().spec,
        "query_policy": "exact-original-dev-vectors; no-embedding/no-chat/no-holdout",
        "timing_policy": "shared-candidates; alternating-dense/bm25-first; hybrid-total=sum-sequential-components; no-independent-hybrid-request; no-explicit-warmup; preparation-separate",
    }
    config["configuration_fingerprint"] = PREFIX + fingerprint(config)
    return Inputs(config, manifest, catalog, questions, labels, vectors)


def plan_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path, config_path: Path) -> dict:
    data = prepare_experiment(dataset_dir, baseline_dir, baseline_private_dir, config_path)
    return {"status": "ok", "mode": "offline_plan", "configuration_fingerprint": data.config["configuration_fingerprint"],
            "documents": len(data.manifest.documents), "chunks": len(data.catalog),
            "reused_query_vectors": len(data.vectors), "qdrant_queries": len(data.questions),
            "bm25_queries": len(data.questions), "fusion_calls": len(data.questions),
            "parameters": data.config["descriptor"], "model_calls": 0, "qdrant_writes": 0}


def _required_files() -> set[str]:
    files = {"experiment_config.json", "evaluation_config.json", "index_manifest.json", "bm25_index.json",
             "candidate_sources.jsonl", "case_details.jsonl", "historical_v0_recheck.json", "status.json"}
    for branch in BRANCHES:
        files.add(f"{branch}_rankings.jsonl")
        files.update(f"{branch}_retrieval/{name}" for name in ("config.json", "retrieval.jsonl", "summary.json"))
    files.update(f"{left}_vs_{right}.json" for left, right in COMPARISONS)
    return files


def collect_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path, config_path: Path,
                       output_dir: Path, *, client: QdrantClient) -> dict:
    started = time.monotonic()
    data = prepare_experiment(dataset_dir, baseline_dir, baseline_private_dir, config_path)
    output, dataset = Path(output_dir), Path(dataset_dir)
    _check(not output.exists() and not output.is_symlink()
           and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name)), "结果目录已存在或run_id无效")
    for protected in (dataset, Path(baseline_dir), Path(baseline_private_dir)):
        _check(not output.resolve().is_relative_to(protected.resolve())
               and not protected.resolve().is_relative_to(output.resolve()), "输出目录不得覆盖或包围冻结输入")
    chunks = read_completed_chunks(client, data.manifest)
    _check({str(c.metadata.chunk_id): c for c in chunks} == {cid: item.chunk for cid, item in data.catalog.items()},
           "实际Qdrant原文与冻结语料不一致")
    _check(client.count(data.manifest.spec.collection_name, exact=True).count == len(chunks), "集合含非V0点或点数不完整")
    server_version = client.info().version
    bm25 = BM25Index.build(data.manifest, chunks)
    spec = data.manifest.spec
    store = DenseQdrantStore(client, collection_name=spec.collection_name, dimensions=spec.dimensions)
    preparation_ms = (time.monotonic() - started) * 1000
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "experiment_config.json", data.config)
    _write(output / "evaluation_config.json", data.config["evaluation_config"])
    _write(output / "index_manifest.json", data.manifest.model_dump(mode="json"))
    bm25.save(output / "bm25_index.json")
    _write(output / "status.json", {"state": "running", "complete": False})
    errors = dict.fromkeys(BRANCHES, 0)
    order = []
    fusion_calls = 0

    def retrieve(branch, question):
        begin = time.monotonic()
        hits, error = [], None
        try:
            query = question["standalone_question"] or question["question"]
            if branch == "dense":
                hits = store.query(data.vectors[question["question_id"]],
                                   document_ids=[d.document_id for d in data.manifest.documents],
                                   dense_index_version=spec.index_version, limit=10)
            else:
                hits = bm25.search(query, top_k=10)
            elapsed = (time.monotonic() - begin) * 1000
            _check(len(hits) <= 10 and len({h.metadata.chunk_id for h in hits}) == len(hits), "候选超预算或重复")
            evaluate_ranking(hits, data.labels[question["question_id"]], data.catalog)
        except (RuntimeError, ValueError):
            elapsed = (time.monotonic() - begin) * 1000
            hits, error = [], {"type": "retrieval_failed", "message": "检索或原文身份校验失败；不自动重试"}
            errors[branch] += 1
        return hits, error, elapsed

    try:
        for i, question in enumerate(data.questions):
            qid, query = question["question_id"], question["standalone_question"] or question["question"]
            sides = ("dense", "bm25") if i % 2 == 0 else ("bm25", "dense")
            result = {branch: retrieve(branch, question) for branch in sides}
            dense, bm = result["dense"], result["bm25"]
            fused, error = [], None
            fusion_start = time.monotonic()
            if dense[1] or bm[1]:
                error = {"type": "dependency_failed", "message": "候选路异常，不能把Hybrid登记为完整成功"}
                fusion_ms = None
            else:
                try:
                    fusion_calls += 1
                    fused = reciprocal_rank_fusion([dense[0], bm[0]], rank_constant=60, top_k=10)
                    fusion_ms = (time.monotonic() - fusion_start) * 1000
                    evaluate_ranking(fused, data.labels[qid], data.catalog)
                except (RuntimeError, ValueError):
                    fused, fusion_ms = [], (time.monotonic() - fusion_start) * 1000
                    error = {"type": "fusion_failed", "message": "融合或身份校验失败；不自动重试"}
            if error:
                errors["hybrid"] += 1
            result["hybrid"] = (fused, error, dense[2] + bm[2] + (fusion_ms or 0))
            for branch in BRANCHES:
                hits, failure, elapsed = result[branch]
                timings = dict.fromkeys(PHASES)
                timings["search"] = dense[2] + bm[2] if branch == "hybrid" else elapsed
                timings["fusion"] = fusion_ms if branch == "hybrid" else None
                timings["total"] = elapsed
                _line(output / f"{branch}_rankings.jsonl", {"question_id": qid, "query": query,
                      "hits": [h.model_dump(mode="json") for h in hits], "error": failure, "timings_ms": timings})
            ranks = [{str(h.metadata.chunk_id): rank for rank, h in enumerate(hits, 1)} for hits in (dense[0], bm[0])]
            _line(output / "candidate_sources.jsonl", {"question_id": qid, "query": query, "order": list(sides),
                  "dense": [{"chunk_id": str(h.metadata.chunk_id), "score": h.score} for h in dense[0]],
                  "bm25": [{"chunk_id": str(h.metadata.chunk_id), "score": h.score} for h in bm[0]],
                  "candidate_union": len(set(ranks[0]) | set(ranks[1])),
                  "fused": [{"chunk_id": str(h.metadata.chunk_id), "score": h.score,
                             "dense_rank": ranks[0].get(str(h.metadata.chunk_id)),
                             "bm25_rank": ranks[1].get(str(h.metadata.chunk_id))} for h in fused]})
            order.append({"question_id": qid, "order": list(sides)})
            if i < 2 and any(result[b][1] for b in BRANCHES):
                raise EvaluationError("前两题本地smoke失败，停止后续检索，不自动重试")
        summaries = {branch: run_evaluation(dataset, output / "evaluation_config.json",
                                           output / f"{branch}_rankings.jsonl", output / f"{branch}_retrieval")
                     for branch in BRANCHES}
        categories = {q["question_id"]: q["category"] for q in data.questions}
        comparisons = {}
        for left, right in COMPARISONS:
            comparisons[left + "_vs_" + right] = compare_retrieval(output / f"{left}_retrieval", output / f"{right}_retrieval", categories)
            _write(output / f"{left}_vs_{right}.json", comparisons[left + "_vs_" + right])
        _write(output / "historical_v0_recheck.json", compare_retrieval(Path(baseline_dir) / "retrieval", output / "dense_retrieval", categories))
        records = {b: {r["question_id"]: r for r in _rows(output / f"{b}_retrieval/retrieval.jsonl")} for b in BRANCHES}
        for question in data.questions:
            qid = question["question_id"]
            _line(output / "case_details.jsonl", {"question_id": qid, "query": question["standalone_question"] or question["question"],
                  "category": question["category"], "gold": data.labels[qid]["evidence"],
                  "branch_metrics": {b: records[b][qid]["metrics"] for b in BRANCHES},
                  "result_files": {b: f"{b}_retrieval/retrieval.jsonl" for b in BRANCHES}})
        status = {"state": "recorded", "complete": True, "retrieval_errors": errors,
                  "reused_query_vectors": len(data.vectors), "qdrant_queries": len(data.questions),
                  "bm25_queries": len(data.questions), "fusion_calls": fusion_calls,
                  "model_calls": {"embedding": 0, "query_embedding": 0, "chat": 0}, "qdrant_writes": 0,
                  "manual_review": "pending", "day14_accepted": False}
        _write(output / "status.json", status)
        sources = [*_json(_read(Path(baseline_private_dir) / "manifest.json"))["source_sha256"],
                   "evals/hybrid_run.py", "evals/comparison.py", "evals/freeze_dataset.py", "evals/validate_dataset.py",
                   "src/project_copilot/rag/bm25.py", "src/project_copilot/rag/fusion.py", "src/project_copilot/rag/hybrid.py"]
        receipt = {"schema_version": 1, "run_id": output.name, "mode": "shared_candidates_three_way",
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "code": _code_state(),
                   "configuration_fingerprint": data.config["configuration_fingerprint"],
                   "dataset_fingerprint": data.config["dataset_fingerprint"],
                   "source_sha256": {name: sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() for name in sources},
                   "runtime": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
                               "qdrant_server": server_version,
                               **{name: version(name) for name in ("qdrant-client", "rank-bm25", "jieba", "numpy")}},
                   "preparation_ms": preparation_ms, "query_order": order, "status": status,
                   "files": {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
                             for p in sorted(output.rglob("*")) if p.is_file()}}
        _check(set(receipt["files"]) == _required_files(), "采集产物不完整")
        _write(output / "manifest.json", receipt)
        return {"status": "ok", "run_id": output.name, **status,
                "at_5": {b: summaries[b]["at_k"]["5"] for b in BRANCHES}}
    except Exception:
        _write(output / "status.json", {"state": "failed", "complete": False, "retrieval_errors": errors})
        raise


def verify_experiment(dataset_dir: Path, output_dir: Path) -> dict:
    """只需公开产物及冻结数据；未来源码改变不使历史结果失效。"""
    output = Path(output_dir)
    receipt = _json(_read(output / "manifest.json"))
    config = _json(_read(output / "experiment_config.json"))
    frozen = verify_freeze(dataset_dir)
    baseline = config["baseline"]
    _check(baseline["configuration_fingerprint"] == "baseline-config-v1-" + fingerprint(
        {k: v for k, v in baseline.items() if k != "configuration_fingerprint"}
    ) and baseline["dataset_fingerprint"] == frozen["fingerprint"]
        and config["evaluation_config"] == baseline["evaluation_config"], "保存的基线合同或评分配置不一致")
    content = {k: v for k, v in config.items() if k != "configuration_fingerprint"}
    _check(PREFIX + fingerprint(content) == config["configuration_fingerprint"] == receipt["configuration_fingerprint"]
           and frozen["fingerprint"] == config["dataset_fingerprint"] == receipt["dataset_fingerprint"], "实验配置或冻结数据指纹不匹配")
    files = {str(p.relative_to(output)) for p in output.rglob("*") if p.is_file() and p != output / "manifest.json"}
    _check(files == set(receipt["files"]) == _required_files(), "封存文件缺失或多出未登记产物")
    for name, digest in receipt["files"].items():
        path = output / name
        _check(path.resolve().is_relative_to(output.resolve()) and sha256(path.read_bytes()).hexdigest() == digest,
               "实验产物字节已变化")
    status = _json(_read(output / "status.json"))
    _check(status == receipt["status"] and status["complete"] is True and status["state"] == "recorded", "采集尚未完整完成")
    manifest = IndexManifest.model_validate_json(_read(output / "index_manifest.json"))
    _check({**manifest.spec.model_dump(mode="json"), "index_version": manifest.spec.index_version}
           == baseline["index"], "保存的Dense索引身份与基线不同")
    saved = _json(_read(output / "bm25_index.json"))
    snapshot = saved["snapshot"]
    _check(saved["fingerprint"] == "bm25-v1-" + fingerprint(snapshot)
           and snapshot["manifest"] == manifest.model_dump(mode="json")
           and snapshot["config"] == config["bm25"] and snapshot["tokenizer"] == config["tokenizer"],
           "BM25身份或内容指纹与实验配置不同")
    return {"status": "ok", "state": "verified", "run_id": receipt["run_id"], "files": len(files),
            "manual_review": status["manual_review"], "day14_accepted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "collect", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        if name != "verify":
            sub.add_argument("--baseline-dir", type=Path, required=True)
            sub.add_argument("--baseline-private-dir", type=Path, required=True)
            sub.add_argument("--config", type=Path, required=True)
        if name != "plan":
            sub.add_argument("--output-dir", type=Path, required=True)
        if name == "collect":
            sub.add_argument("--qdrant-url", default="http://127.0.0.1:6433")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            result = verify_experiment(args.dataset, args.output_dir)
        elif args.command == "plan":
            result = plan_experiment(args.dataset, args.baseline_dir, args.baseline_private_dir, args.config)
        else:
            with closing(QdrantClient(url=args.qdrant_url, check_compatibility=False,
                                      trust_env=False, prefer_grpc=False, timeout=10)) as client:
                result = collect_experiment(args.dataset, args.baseline_dir, args.baseline_private_dir,
                                            args.config, args.output_dir, client=client)
    except Exception:
        print(json.dumps({"status": "error", "message": "本地三路实验失败；检查冻结输入、Qdrant和局部status，无自动重试"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
