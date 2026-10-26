"""D17保留集验收：先验证dev选型冻结，再比较V0与唯一最终版本；不选参。"""

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
import json
import math
import re

from evals.baseline_config import fingerprint
from evals.comparison import FIELDS, KS, _outcome
from evals.final_selection import verify_selection
from evals.metrics import build_chunk_catalog, evaluate_ranking
from evals.rerank_run import (
    RecordingScorer, _digest, _line, _model_snapshot, _packages, _pass,
    _verify_detail, _write,
)
from evals.run import PHASES, _aggregate, _check, _code_state, _json, _read, _rows, _snapshot_row
from project_copilot.config import PROJECT_ROOT
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.bm25 import BM25Index, read_completed_chunks
from project_copilot.rag.fusion import reciprocal_rank_fusion
from project_copilot.rag.loaders import load_document
from project_copilot.rag.rerank import LocalCrossEncoder, RerankConfig, Reranker
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import IndexManifest


BRANCHES = ("baseline", "bm25", "hybrid", "final")
TIMING_POLICY = "shared-candidates; sequential-component-sums; batch-embedding-separate; warmup-excluded; not-independent-online-requests"


def prepare_inputs(dataset_dir: Path, config_path: Path) -> tuple:
    dataset = Path(dataset_dir)
    frozen = verify_selection(dataset, config_path)
    selected = frozen["selection"]
    split = _json(_read(dataset / "split_manifest.json"))
    ids = split["holdout"]
    _check(len(ids) == len(set(ids)) == 10 and not set(ids) & set(split["dev"]), "保留集必须为冻结同序10题")
    by_id = {q["question_id"]: q for q in _rows(dataset / "questions.jsonl")}
    questions = [by_id[qid] for qid in ids]
    labels = {r["question_id"]: r for r in _rows(dataset / "labels.draft.jsonl") if r["question_id"] in ids}
    documents = [load_document(dataset / s["path"]) for s in _rows(dataset / "sources.jsonl")]
    catalog = build_chunk_catalog(documents, SplitConfig.model_validate(selected["splitter"]))
    index_path = PROJECT_ROOT / "evals/results/dense-v0-20261019-01/index_manifest.json"
    manifest = IndexManifest.model_validate_json(_read(index_path))
    return frozen, questions, labels, catalog, manifest


def plan_holdout(dataset_dir: Path, config_path: Path) -> dict:
    frozen, questions, _, _, _ = prepare_inputs(dataset_dir, config_path)
    return {"status": "ok", "mode": "offline_holdout_plan", "selected": frozen["selection"]["selected"],
            "selection_fingerprint": frozen["fingerprint"], "questions": len(questions),
            "max_embedding_api_calls": 1, "max_input_utf8_bytes": 28000,
            "dense_queries": 10, "bm25_queries": 10, "fusion_calls": 10,
            "local_score_calls": 11, "pairs": 110, "warmup": "first-dev-question; excluded",
            "chat_calls": 0, "model_loaded": False, "retuning": False}


def score_rows(questions: list, labels: dict, catalog: dict, rows: list) -> tuple[list, dict]:
    _check([r["question_id"] for r in rows] == [q["question_id"] for q in questions], "必须完整同序保留所有保留题，包括失败题")
    records = []
    for question, row in zip(questions, rows, strict=True):
        hits, times, error = _snapshot_row(row, question)
        metrics = evaluate_ranking(hits, labels[question["question_id"]], catalog, KS)
        records.append({"question_id": row["question_id"], "query": row["query"], "category": question["category"],
                        "hits": row["hits"], "timings_ms": times, "error": error, "metrics": metrics})
    return records, {"split": "holdout", "mode": "frozen_v0_vs_final", **_aggregate(records, list(KS))}


def compare_rows(baseline: list, final: list) -> dict:
    _check([r["question_id"] for r in baseline] == [r["question_id"] for r in final], "两侧题目必须同序完整")
    pairs = []
    for before, after in zip(baseline, final, strict=True):
        _check(before["query"] == after["query"] and before["category"] == after["category"], "两侧问题不一致")
        deltas = [float(after["metrics"]["at_k"][str(k)][f]) - float(before["metrics"]["at_k"][str(k)][f])
                  for k in KS for f in FIELDS] if before["metrics"]["answerable"] else None
        pairs.append({"question_id": before["question_id"], "category": before["category"],
                      "outcome": _outcome(deltas) if deltas is not None else "unanswerable",
                      "baseline_metrics": before["metrics"], "final_metrics": after["metrics"],
                      "baseline_error": before["error"], "final_error": after["error"]})
    categories = {}
    for pair in pairs:
        group = categories.setdefault(pair["category"], {"questions": 0, "outcomes": {}})
        group["questions"] += 1
        group["outcomes"][pair["outcome"]] = group["outcomes"].get(pair["outcome"], 0) + 1
    return {"split": "holdout", "per_question": pairs, "outcomes": dict(Counter(p["outcome"] for p in pairs)),
            "categories": categories, "refusal_accuracy": None, "timing_policy": TIMING_POLICY,
            "selection_changed_after_holdout": False}


def _required_files() -> set:
    names = {"selection.json", "index_manifest.json", "bm25_index.json", "runtime.json", "warmup.json",
             "query_vector_manifest.json", "embedding_batch.json", "status.json", "score_details.jsonl",
             "comparison.json", "case_details.jsonl"}
    names.update(f"{b}_{name}" for b in BRANCHES for name in ("rankings.jsonl", "scored.jsonl", "summary.json"))
    return names


def _output_check(output: Path, dataset: Path, config: Path) -> None:
    _check(not output.exists() and not output.is_symlink()
           and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name)), "输出目录已存在或名称非法")
    for protected in (dataset, config, PROJECT_ROOT / "evals/results", PROJECT_ROOT / "evals/datasets"):
        _check(not output.resolve().is_relative_to(protected.resolve())
               and not protected.resolve().is_relative_to(output.resolve()), "输出不得覆盖冻结输入或原公开归档")


def collect_holdout(dataset_dir: Path, config_path: Path, output_dir: Path, *, client, embeddings,
                    scorer=None, cache_dir: Path | None = None) -> dict:
    frozen, questions, labels, catalog, manifest = prepare_inputs(dataset_dir, config_path)
    output, dataset = Path(output_dir), Path(dataset_dir)
    _output_check(output, dataset, Path(config_path))
    chunks = read_completed_chunks(client, manifest)
    _check({str(c.metadata.chunk_id): c for c in chunks} == {cid: c.chunk for cid, c in catalog.items()}
           and client.count(manifest.spec.collection_name, exact=True).count == len(chunks), "实际索引不是原V0完整语料")
    bm25 = BM25Index.build(manifest, chunks)
    selection = frozen["selection"]
    parent_config = _json(_read(PROJECT_ROOT / "evals/results/hybrid-v2-20261022-01/experiment_config.json"))
    _check(bm25.snapshot.config.model_dump(mode="json") == parent_config["bm25"]
           and bm25.snapshot.tokenizer == parent_config["tokenizer"], "BM25参数或分词器发生变化")
    config = RerankConfig(**selection["rerank"])
    runtime = {"packages": _packages(), "injected_scorer": scorer is not None, "timing_policy": TIMING_POLICY,
               "embedding_contract": selection["baseline"]["embedding"], "server_version": client.info().version,
               "tokenizer": bm25.snapshot.tokenizer}
    output.mkdir(parents=True, exist_ok=False)
    (output / "selection.json").write_bytes(Path(config_path).read_bytes())
    _write(output / "index_manifest.json", manifest.model_dump(mode="json"))
    bm25.save(output / "bm25_index.json")
    status = {"state": "running", "complete": False, "questions": 0, "embedding_api_calls": 0,
              "dense_queries": 0, "local_score_calls": 0, "attempted_pairs": 0,
              "successful_score_calls": 0, "scored_pairs": 0, "chat_calls": 0,
              "manual_review": "pending", "day17_accepted": False}
    _write(output / "status.json", status)
    recording, counts_before = None, None
    try:
        torch = None
        if scorer is None:
            cache = Path(cache_dir) if cache_dir else PROJECT_ROOT / "models/huggingface"
            runtime["model_artifact"] = _model_snapshot(cache, config)
            import torch as torch_runtime
            torch = torch_runtime
            _check(torch.cuda.is_available(), "CUDA不可用，不回退CPU")
            torch.cuda.init()
            runtime.update({"cuda_build": torch.version.cuda, "gpu": torch.cuda.get_device_name()})
            scorer = LocalCrossEncoder(config, cache_dir=cache)
        _write(output / "runtime.json", runtime)
        recording = RecordingScorer(scorer)
        reranker = Reranker(recording, config=config)
        warmup_row = _rows(PROJECT_ROOT / "evals/results/hybrid-v2-20261022-01/hybrid_rankings.jsonl")[0]
        _, warmup = _pass(reranker, warmup_row, torch=torch)
        _write(output / "warmup.json", warmup)
        _check(warmup["error"] is None, "开发题预热失败，未进行保留集Embedding")
        queries = [q["standalone_question"] or q["question"] for q in questions]
        _check(all(len(q.encode("utf-8")) <= 3000 for q in queries)
               and sum(len(q.encode("utf-8")) for q in queries) <= 28000, "问题超出本轮单批授权输入预算")
        counts_before, started = embeddings.stats(), perf_counter()
        vectors = embeddings.embed_documents(queries)
        elapsed = (perf_counter() - started) * 1000
        delta = {k: embeddings.stats()[k] - counts_before[k] for k in counts_before}
        status["embedding_api_calls"] = delta["api_calls"]
        _check(delta["api_calls"] in (0, 1) and len(vectors) == len(queries)
               and all(isinstance(v, list) and len(v) == manifest.spec.dimensions
                       and all(type(x) in (int, float) and math.isfinite(x) for x in v) for v in vectors),
               "问题向量或单批调用预算不符")
        _write(output / "embedding_batch.json", {"texts": 10, "utf8_bytes": sum(len(q.encode("utf-8")) for q in queries),
               "elapsed_ms": elapsed, "cache_delta": delta, "usage": None, "billed_cost": None,
               "max_budget_tokens": 30000, "estimated_max_cny": 0.015,
               "price_checked_on": "2026-10-26", "price_source": "https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3"})
        _write(output / "query_vector_manifest.json", [{"question_id": q["question_id"],
               "query_sha256": fingerprint(query), "vector_sha256": fingerprint(vector)}
               for q, query, vector in zip(questions, queries, vectors, strict=True)])
        store = DenseQdrantStore(client, collection_name=manifest.spec.collection_name, dimensions=manifest.spec.dimensions)
        for i, (question, query, vector) in enumerate(zip(questions, queries, vectors, strict=True)):
            qid = question["question_id"]
            sides = {}
            for branch in ("baseline", "bm25"):
                started = perf_counter()
                elapsed = None
                try:
                    if branch == "baseline":
                        status["dense_queries"] += 1
                        hits = store.query(vector, document_ids=[d.document_id for d in manifest.documents],
                                           dense_index_version=manifest.spec.index_version, limit=10)
                    else:
                        hits = bm25.search(query, top_k=10)
                    elapsed = (perf_counter() - started) * 1000
                    # gold映射只做身份验证，不混进检索组件耗时。
                    evaluate_ranking(hits, labels[qid], catalog)
                    error = None
                except (ValueError, RuntimeError):
                    hits, error = [], {"type": "retrieval_failed", "message": "候选检索或原文校验失败，无重试"}
                if elapsed is None:
                    elapsed = (perf_counter() - started) * 1000
                sides[branch] = {"question_id": qid, "query": query, "hits": [h.model_dump(mode="json") for h in hits],
                                 "error": error, "timings_ms": {**dict.fromkeys(PHASES), "search": elapsed, "total": elapsed}}
            started = perf_counter()
            dependency_error = sides["baseline"]["error"] or sides["bm25"]["error"]
            fused = []
            if not dependency_error:
                dense_hits, _, _ = _snapshot_row(sides["baseline"], question)
                bm_hits, _, _ = _snapshot_row(sides["bm25"], question)
                fused = reciprocal_rank_fusion([dense_hits, bm_hits], rank_constant=60, top_k=10)
                _check(len(fused) == 10, "融合候选须保持冻结10个预算")
            fusion_ms = (perf_counter() - started) * 1000
            search_ms = sum(sides[b]["timings_ms"]["search"] for b in ("baseline", "bm25"))
            sides["hybrid"] = {"question_id": qid, "query": query, "hits": [h.model_dump(mode="json") for h in fused],
                "error": dependency_error, "timings_ms": {**dict.fromkeys(PHASES), "search": search_ms,
                "fusion": fusion_ms, "total": search_ms + fusion_ms}}
            if dependency_error:
                sides["final"] = {**sides["hybrid"], "hits": []}
                detail = {"question_id": qid, "query": query, "error": dependency_error, "candidates": [], "score_succeeded": False}
            else:
                sides["final"], detail = _pass(reranker, sides["hybrid"], torch=torch)
                rerank_ms = sides["final"]["timings_ms"]["rerank"]
                sides["final"]["timings_ms"].update({"search": search_ms, "fusion": fusion_ms,
                                                      "total": search_ms + fusion_ms + rerank_ms})
            for branch, row in sides.items():
                _line(output / f"{branch}_rankings.jsonl", row)
            _line(output / "score_details.jsonl", detail)
            status["questions"] += 1
            if i < 2:
                _check(not sides["baseline"]["error"] and not sides["final"]["error"], "前两题smoke失败，停止剩余保留集，不重试")
        records = {}
        for branch in BRANCHES:
            records[branch], summary = score_rows(questions, labels, catalog, _rows(output / f"{branch}_rankings.jsonl"))
            _write(output / f"{branch}_summary.json", summary)
            for row in records[branch]:
                _line(output / f"{branch}_scored.jsonl", row)
        comparison = compare_rows(records["baseline"], records["final"])
        _write(output / "comparison.json", comparison)
        for question, before, after in zip(questions, records["baseline"], records["final"], strict=True):
            _line(output / "case_details.jsonl", {"question_id": question["question_id"], "query": before["query"],
                  "gold": labels[question["question_id"]]["evidence"], "baseline_selected_hits": before["hits"][:5],
                  "final_selected_hits": after["hits"][:5], "baseline_metrics": before["metrics"], "final_metrics": after["metrics"]})
        status.update({"state": "recorded", "complete": True})
        _update_counts(status, recording)
        _write(output / "status.json", status)
        files = {str(p.relative_to(output)): _digest(p) for p in output.rglob("*") if p.is_file()}
        _check(set(files) == _required_files(), "保留集产物不完整")
        sources = ["evals/final_selection.py", "evals/holdout.py", "evals/metrics.py", "evals/rerank_run.py",
                   "evals/cache.py", "src/project_copilot/rag/bm25.py", "src/project_copilot/rag/fusion.py",
                   "src/project_copilot/rag/rerank.py", "src/project_copilot/infrastructure/qdrant.py", "pyproject.toml", "uv.lock"]
        _write(output / "manifest.json", {"schema_version": 1, "run_id": output.name, "split": "holdout",
               "recorded_at": datetime.now(timezone.utc).isoformat(), "code": _code_state(),
               "selection_sha256": _digest(config_path), "status": status,
               "source_sha256": {p: _digest(PROJECT_ROOT / p) for p in sources}, "files": files})
        return {"status": "ok", **status, "outcomes": comparison["outcomes"]}
    except Exception:
        status.update({"state": "partial", "complete": False})
        if counts_before is not None:
            status["embedding_api_calls"] = embeddings.stats()["api_calls"] - counts_before["api_calls"]
        if recording is not None:
            _update_counts(status, recording)
        _write(output / "status.json", status)
        raise


def _update_counts(status: dict, recording: RecordingScorer) -> None:
    for key in ("local_score_calls", "attempted_pairs", "successful_score_calls", "scored_pairs"):
        status[key] = getattr(recording, key)


def verify_holdout(dataset_dir: Path, output_dir: Path) -> dict:
    output = Path(output_dir)
    receipt = _json(_read(output / "manifest.json"))
    actual = {str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()} - {"manifest.json"}
    _check(actual == set(receipt["files"]) == _required_files(), "封存文件缺失或存在额外文件")
    for rel, expected in receipt["files"].items():
        path = output / rel
        _check(path.resolve().is_relative_to(output.resolve()) and _digest(path) == expected, "封存文件字节已变更")
    frozen, questions, labels, catalog, manifest = prepare_inputs(dataset_dir, output / "selection.json")
    _check(receipt["split"] == "holdout" and receipt["selection_sha256"] == _digest(output / "selection.json")
           and _json(_read(output / "index_manifest.json")) == manifest.model_dump(mode="json"), "选型或索引身份不符")
    bm25 = BM25Index.load(output / "bm25_index.json", manifest)
    rows = {b: _rows(output / f"{b}_rankings.jsonl") for b in BRANCHES}
    records = {}
    for branch in BRANCHES:
        records[branch], summary = score_rows(questions, labels, catalog, rows[branch])
        _check(records[branch] == _rows(output / f"{branch}_scored.jsonl")
               and summary == _json(_read(output / f"{branch}_summary.json")), "指标或汇总与原始排名不一致")
    details = _rows(output / "score_details.jsonl")
    _check([r["question_id"] for r in details] == [q["question_id"] for q in questions], "分数详情题目缺失或错序")
    runtime = _json(_read(output / "runtime.json"))
    cases = _rows(output / "case_details.jsonl")
    expected_cases = []
    for i, question in enumerate(questions):
        dense, _, dense_error = _snapshot_row(rows["baseline"][i], question)
        bm, _, bm_error = _snapshot_row(rows["bm25"][i], question)
        _check(len(dense) <= 10 and len(bm) <= 10, "候选超出冻结预算")
        expected_bm = [h.model_dump(mode="json") for h in bm25.search(question["standalone_question"] or question["question"], top_k=10)]
        _check(bm_error is not None or rows["bm25"][i]["hits"] == expected_bm, "词项候选与冻结词项索引不一致")
        if not dense_error and not bm_error:
            fused = [h.model_dump(mode="json") for h in reciprocal_rank_fusion([dense, bm], rank_constant=60, top_k=10)]
            _check(rows["hybrid"][i]["hits"] == fused and not rows["hybrid"][i]["error"], "融合结果与原两路候选不一致")
            rerank_only = {**rows["final"][i], "timings_ms": {
                **dict.fromkeys(PHASES), "rerank": rows["final"][i]["timings_ms"]["rerank"]}}
            _verify_detail(details[i], rows["hybrid"][i], rerank_only, real=not runtime["injected_scorer"])
        else:
            _check(not rows["final"][i]["hits"] and rows["final"][i]["error"] is not None
                   and details[i]["candidates"] == [], "依赖失败不能回退为成功")
        for branch in ("baseline", "final"):
            times = rows[branch][i]["timings_ms"]
            _check(times["embedding"] is None, "批量Embedding耗时不得除以题数冒充逐题实测")
        base_times, final_times = rows["baseline"][i]["timings_ms"], rows["final"][i]["timings_ms"]
        _check(base_times["total"] == base_times["search"], "Dense组件计时不一致")
        if not rows["hybrid"][i]["error"]:
            search = sum(rows[b][i]["timings_ms"]["search"] for b in ("baseline", "bm25"))
            _check(final_times["search"] == search
                   and final_times["fusion"] == rows["hybrid"][i]["timings_ms"]["fusion"]
                   and final_times["total"] == search + final_times["fusion"] + final_times["rerank"],
                   "最终计时须为本轮共享候选组件之和")
        before, after = records["baseline"][i], records["final"][i]
        expected_cases.append({"question_id": question["question_id"], "query": before["query"], "gold": labels[question["question_id"]]["evidence"],
               "baseline_selected_hits": before["hits"][:5], "final_selected_hits": after["hits"][:5],
               "baseline_metrics": before["metrics"], "final_metrics": after["metrics"]})
    _check(cases == expected_cases and _json(_read(output / "comparison.json")) == compare_rows(records["baseline"], records["final"]),
           "原文案例或成对比较不自洽")
    warmup = _json(_read(output / "warmup.json"))
    warmup_row = _rows(PROJECT_ROOT / "evals/results/hybrid-v2-20261022-01/hybrid_rankings.jsonl")[0]
    _verify_detail(warmup, warmup_row, None, real=not runtime["injected_scorer"])
    status = _json(_read(output / "status.json"))
    _check(all(type(status[key]) is int and status[key] >= 0 for key in (
        "questions", "embedding_api_calls", "dense_queries", "local_score_calls", "attempted_pairs",
        "successful_score_calls", "scored_pairs", "chat_calls")), "调用计数必须为非负整数，不接受布尔值")
    scored = [warmup, *details]
    attempts = [d for d in scored if d["candidates"]]
    successful = [d for d in attempts if d["score_succeeded"]]
    _check(status == receipt["status"] and status["complete"] is True and status["state"] == "recorded"
           and status["questions"] == status["dense_queries"] == 10
           and status["local_score_calls"] == len(attempts) <= 11
           and status["attempted_pairs"] == 10 * len(attempts)
           and status["successful_score_calls"] == len(successful)
           and status["scored_pairs"] == 10 * len(successful)
           and status["embedding_api_calls"] in (0, 1) and status["chat_calls"] == 0
           and status["manual_review"] == "pending" and status["day17_accepted"] is False,
           "调用预算、失败分母或人工状态不符")
    vector_manifest = json.loads(_read(output / "query_vector_manifest.json"))
    _check([r["question_id"] for r in vector_manifest] == [q["question_id"] for q in questions]
           and all(r["query_sha256"] == fingerprint(q["standalone_question"] or q["question"])
                   and re.fullmatch(r"[0-9a-f]{64}", r["vector_sha256"])
                   for r, q in zip(vector_manifest, questions, strict=True)), "问题向量身份清单不符")
    batch = _json(_read(output / "embedding_batch.json"))
    _check(batch["texts"] == 10 and batch["cache_delta"]["api_calls"] == status["embedding_api_calls"]
           and batch["utf8_bytes"] == sum(len(r["query"].encode("utf-8")) for r in rows["baseline"])
           and batch["utf8_bytes"] <= 28000, "Embedding批次计数或输入预算不符")
    _check(runtime["embedding_contract"] == frozen["selection"]["baseline"]["embedding"]
           and runtime["timing_policy"] == TIMING_POLICY, "运行合同不匹配")
    if not runtime["injected_scorer"]:
        for detail in attempts:
            if detail["error"] is None:
                gpu = detail["gpu"]
                _check(gpu["actual_model_device"].startswith("cuda:")
                       and gpu["actual_model_dtype"] == "torch.float16"
                       and gpu["all_floating_parameters_verified"] is True
                       and 0 <= gpu["max_allocated_bytes"] <= gpu["max_reserved_bytes"],
                       "保存的GPU参数或显存观察不符合冻结配置")
    return {"status": "ok", "state": "verified", "run_id": receipt["run_id"], "files": len(actual),
            "split": "holdout", "model_loaded": False, "manual_review": "pending", "day17_accepted": False,
            "selection_fingerprint": frozen["fingerprint"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "collect", "verify"])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "data/evals/embedding-cache")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.command == "collect" and not args.live:
        print(json.dumps({"status": "error", "message": "真实保留集验收须显式--live；未创建客户端或加载模型"}, ensure_ascii=False))
        return 1
    try:
        if args.command == "verify":
            _check(args.output_dir is not None, "verify需要结果目录")
            result = verify_holdout(args.dataset, args.output_dir)
        elif args.command == "plan":
            result = plan_holdout(args.dataset, args.config)
        else:
            from contextlib import closing
            from qdrant_client import QdrantClient
            from evals.baseline_config import assert_runtime_matches
            from evals.cache import CachedEmbeddings
            from project_copilot.config import load_settings
            from project_copilot.infrastructure.models import create_embedding_client
            frozen = verify_selection(args.dataset, args.config)
            _check(args.output_dir is not None, "collect需要新目录")
            _output_check(args.output_dir, args.dataset, args.config)
            settings = load_settings()
            assert_runtime_matches(frozen["selection"]["baseline"], settings)
            with create_embedding_client(settings) as delegate, closing(QdrantClient(url=str(settings.qdrant_url), timeout=settings.qdrant_timeout_seconds)) as client:
                embeddings = CachedEmbeddings(delegate, args.cache_dir, base_url=str(settings.zhipu_base_url))
                result = collect_holdout(args.dataset, args.config, args.output_dir, client=client, embeddings=embeddings)
    except Exception:
        print(json.dumps({"status": "error", "message": "保留集校验或执行失败；检查冻结配置、服务、缓存和局部status，无自动重试"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
