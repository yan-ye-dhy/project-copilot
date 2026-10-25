"""D16：离线计划、显式本地重排、公开产物校验；仅改变冻结V2的候选次序。"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import re
import sys
from time import perf_counter

from evals.baseline_config import fingerprint
from evals.comparison import compare_retrieval
from evals.freeze_dataset import verify_freeze
from evals.hybrid_run import verify_experiment as verify_hybrid
from evals.metrics import build_chunk_catalog, evaluate_ranking
from evals.run import PHASES, _aggregate, _check, _code_state, _json, _read, _rows, _snapshot_row, run_evaluation
from project_copilot.rag.loaders import load_document
from project_copilot.rag.rerank import LocalCrossEncoder, RerankConfig, Reranker, _scores
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import IndexManifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFIX = "rerank-experiment-v1-"
PARENT_SHA256 = "0263150ef53e349415c3a4aad68c5bd218c0fa4bfeb194681338d4b7ce4055ab"
DATASET_FINGERPRINT = "dataset-v1-54806b5174b2f3d2e4bd08b91b57569c5121929731112c3d375d4d42d4306b7e"
PARENT_FINGERPRINT = "hybrid-experiment-v1-5b5039b64d2464e9dfb6e448b54de1c05b61376568f0e83983382c2c0e5a329e"
MODEL_EVIDENCE = "docs/evidence/D15_RERANK_SMOKE.json"
MODEL_EVIDENCE_SHA256 = "7040b1cc02099e0c083a1da588d8fa528ce24d5bd65d0bd7258165aa6f1b2ff6"
GPU_EVIDENCE = "docs/evidence/D15_GPU_RERANK_SMOKE.json"
GPU_EVIDENCE_SHA256 = "0a49591f425a2f2bbbf440fee0608afc44b7c196ca9aab20768b378a6b93c679"
TIMING_POLICY = (
    "baseline=byte-copied-historical-V2; candidate=local-rerank-only-including-tokenization-and-sort; "
    "embedding/search/fusion/total=null; no-live-baseline-or-end-to-end-latency-comparison"
)


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _line(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()


def _digest(path: Path) -> str:
    # 大权重至多在collect前置校验时读一次，不在逐题循环重新计算。
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _descriptor() -> dict:
    return {
        "schema_version": 1, "experiment_id": "rerank-v3-bge-v2-m3",
        "parent_run_id": "hybrid-v2-20261022-01", "parent_manifest_sha256": PARENT_SHA256,
        "parent_configuration_fingerprint": PARENT_FINGERPRINT,
        "dataset_fingerprint": DATASET_FINGERPRINT, "candidate_branch": "hybrid",
        "ranking_depth": 10, "answer_top_k": 5, "warmup_calls": 1, "smoke_questions": 2,
        # 保留10名仅供检索k=1/3/5/10评分；回答上下文合同独立固定为5块。
        "rerank": {"model_name": "BAAI/bge-reranker-v2-m3",
                   "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
                   "device": "cuda", "dtype": "float16", "max_length": 512,
                   "batch_size": 2, "max_candidates": 10, "top_k": 10,
                   "cpu_threads": 4, "local_files_only": True},
    }


def _validate_descriptor(value: dict) -> None:
    _check(value == _descriptor() and all(type(value.get(k)) is int for k in (
        "schema_version", "ranking_depth", "answer_top_k", "warmup_calls", "smoke_questions"
    )) and all(type(value["rerank"].get(k)) is int for k in (
        "max_length", "batch_size", "max_candidates", "top_k", "cpu_threads"
    )) and value["rerank"].get("local_files_only") is True,
        "D16固定父归档、cuda/float16、512 tokens、batch2、10候选及最终5块")


@dataclass
class Inputs:
    config: dict
    parent: dict
    parent_status: dict
    index: dict
    questions: list[dict]
    labels: dict
    catalog: dict
    rows: list[dict]


def _dataset_inputs(dataset_dir: Path, evaluation: dict, rows: list[dict], index: dict) -> tuple:
    frozen = verify_freeze(dataset_dir)
    _check(frozen["fingerprint"] == DATASET_FINGERPRINT == evaluation["dataset_fingerprint"]
           and evaluation["split"] == "dev" and evaluation["question_ids"] is None
           and evaluation["ks"] == [1, 3, 5, 10] and evaluation["ranking_origin"] == "recorded_retrieval"
           and evaluation["splitter"] == {"chunk_size": 750, "chunk_overlap": 150,
                                          "separators": ["\n\n", "\n", " ", ""]},
           "必须使用冻结750/150开发集及完整k评分合同")
    ids = _json(_read(dataset_dir / "split_manifest.json"))["dev"]
    _check(len(ids) == len(set(ids)) == 30 and [row.get("question_id") for row in rows] == ids,
           "候选须完整同序覆盖30道dev题，不能包含重复、缺失或保留集")
    question_map = {q["question_id"]: q for q in _rows(dataset_dir / "questions.jsonl")}
    questions = [question_map[qid] for qid in ids]
    labels = {q["question_id"]: q for q in _rows(dataset_dir / "labels.draft.jsonl") if q["question_id"] in ids}
    _check(sum(labels[qid]["answerable"] for qid in ids) == 26, "开发集须保留26可答及4无答案题")
    documents = [load_document(dataset_dir / row["path"]) for row in _rows(dataset_dir / "sources.jsonl")]
    catalog = build_chunk_catalog(documents, SplitConfig.model_validate(evaluation["splitter"]))
    manifest = IndexManifest.model_validate(index)
    _check(manifest.spec.splitter_version == SplitConfig.model_validate(evaluation["splitter"]).index_version
           and {str(cid) for doc in manifest.documents for cid in doc.chunk_ids} == set(catalog),
           "父索引身份与冻结语料不同")
    for row, question in zip(rows, questions, strict=True):
        hits, _, error = _snapshot_row(row, question)
        _check(error is None and len(hits) == len({h.metadata.chunk_id for h in hits}) == 10,
               "每题必须恰有原序10个完整、不重复的成功Hybrid候选")
        evaluate_ranking(hits, labels[row["question_id"]], catalog)
    return questions, labels, catalog


def prepare_experiment(dataset_dir: Path, hybrid_dir: Path, config_path: Path) -> Inputs:
    """仅校验公开归档和冻结数据；不读.env、缓存、Torch或任何服务。"""
    dataset, parent_dir = Path(dataset_dir), Path(hybrid_dir)
    descriptor = _json(_read(config_path))
    _validate_descriptor(descriptor)
    _check(_digest(parent_dir / "manifest.json") == PARENT_SHA256, "父V2归档不是已冻结的公开运行")
    verify_hybrid(dataset, parent_dir)
    parent = _json(_read(parent_dir / "manifest.json"))
    parent_config = _json(_read(parent_dir / "experiment_config.json"))
    parent_status = _json(_read(parent_dir / "status.json"))
    _check(_digest(PROJECT_ROOT / GPU_EVIDENCE) == GPU_EVIDENCE_SHA256, "D15 GPU来源证据已变化")
    gpu_evidence = _json(_read(PROJECT_ROOT / GPU_EVIDENCE))
    for name, digest in {**parent["source_sha256"], **gpu_evidence["source_sha256"]}.items():
        source = PROJECT_ROOT / name
        _check(source.resolve().is_relative_to(PROJECT_ROOT) and _digest(source) == digest,
               "已冻结的父V2或D15运行源码及依赖已变化")
    evaluation = _json(_read(parent_dir / "evaluation_config.json"))
    index = _json(_read(parent_dir / "index_manifest.json"))
    rows = _rows(parent_dir / "hybrid_rankings.jsonl")
    _check(parent["run_id"] == descriptor["parent_run_id"]
           and parent["configuration_fingerprint"] == descriptor["parent_configuration_fingerprint"]
           and evaluation == parent_config["evaluation_config"] and parent_status == parent["status"]
           and parent_status["retrieval_errors"]["hybrid"] == 0, "父归档运行、配置或状态不一致")
    questions, labels, catalog = _dataset_inputs(dataset, evaluation, rows, index)
    config = {
        "schema_version": 1, "mode": "fixed_hybrid_candidates_rerank", "descriptor": descriptor,
        "dataset_fingerprint": DATASET_FINGERPRINT, "split": "dev",
        "evaluation_config": evaluation, "parent_experiment_config": parent_config,
        "parent_manifest_sha256": PARENT_SHA256,
        "candidate_source_sha256": parent["files"]["hybrid_rankings.jsonl"],
        "query_policy": "exact-30-dev-in-original-order; no-holdout/no-retrieval/no-api",
        "timing_policy": TIMING_POLICY,
        "score_semantics": "cross-encoder-raw-logit; original-RRF-score-text-metadata-preserved",
        "truncation_policy": "only_second; full-query-preserved; reject-query-budget-exhausted",
        "answer_context": {"top_k": 5, "policy": "first-five-ranked-hits; no-answer-generation"},
    }
    config["configuration_fingerprint"] = PREFIX + fingerprint(config)
    return Inputs(config, parent, parent_status, index, questions, labels, catalog, rows)


def plan_experiment(dataset_dir: Path, hybrid_dir: Path, config_path: Path) -> dict:
    data = prepare_experiment(dataset_dir, hybrid_dir, config_path)
    return {"status": "ok", "mode": "offline_plan", "parameters": data.config["descriptor"],
            "configuration_fingerprint": data.config["configuration_fingerprint"],
            "question_count": 30, "answerable_count": 26, "unanswerable_count": 4,
            "local_score_calls": 31, "scored_pairs": 310, "warmup_calls": 1,
            "warmup_question_id": data.questions[0]["question_id"],
            "smoke_question_ids": [q["question_id"] for q in data.questions[:2]],
            "evaluation_score_calls": 30, "answer_top_k": 5, "ranking_depth": 10,
            "model_api_calls": 0, "qdrant_requests": 0, "model_loaded": False,
            "timing_policy": TIMING_POLICY}


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _packages() -> dict:
    result = {}
    for name in ("torch", "transformers", "huggingface-hub", "tokenizers", "safetensors", "pydantic"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def _model_snapshot(cache_dir: Path, config: RerankConfig) -> dict:
    evidence = PROJECT_ROOT / MODEL_EVIDENCE
    _check(_digest(evidence) == MODEL_EVIDENCE_SHA256, "D15模型来源证据已变化")
    preparation = _json(_read(evidence))["model_preparation"]
    _check(preparation["model"] == config.model_name and preparation["revision"] == config.revision,
           "D15权重证据revision与本轮模型不同")
    snapshot = cache_dir / ("models--" + config.model_name.replace("/", "--")) / "snapshots" / config.revision
    _check(snapshot.is_dir(), "本地固定模型快照缺失；不下载或回退")
    for name, record in preparation["files"].items():
        path = snapshot / name
        _check(path.is_file() and path.stat().st_size == record["bytes"] and _digest(path) == record["sha256"],
               "本地模型快照缺失或SHA不符合已验证revision")
    return {"evidence_path": MODEL_EVIDENCE, "evidence_sha256": MODEL_EVIDENCE_SHA256,
            "model": config.model_name, "revision": config.revision, "files": preparation["files"],
            "verification": "all-six-local-files-size-and-sha256-once-before-model-load"}


class RecordingScorer:
    def __init__(self, scorer: object) -> None:
        self.scorer = scorer
        self.local_score_calls = 0
        self.attempted_pairs = 0
        self.successful_score_calls = 0
        self.scored_pairs = 0
        self.last_score_succeeded = False

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.local_score_calls += 1
        self.attempted_pairs += len(passages)
        self.last_score_succeeded = False
        values = _scores(self.scorer.score(query, passages), len(passages))
        self.successful_score_calls += 1
        self.scored_pairs += len(passages)
        self.last_score_succeeded = True
        return values


def _gpu_observation(scorer: object, torch: object) -> dict:
    parameters = [p for p in scorer._model.parameters() if p.is_floating_point()]
    _check(bool(parameters) and all(p.device.type == "cuda" and p.dtype == torch.float16 for p in parameters),
           "实际模型参数未使用cuda/float16；拒绝隐式回退")
    return {"actual_model_device": str(parameters[0].device), "actual_model_dtype": str(parameters[0].dtype),
            "all_floating_parameters_verified": True, "max_allocated_bytes": torch.cuda.max_memory_allocated(),
            "max_reserved_bytes": torch.cuda.max_memory_reserved(),
            "allocated_after_pass_bytes": torch.cuda.memory_allocated()}


def _pass(reranker: Reranker, row: dict, *, torch: object | None) -> tuple[dict, dict]:
    scorer = reranker.scorer.scorer
    candidates, _, _ = _snapshot_row(row, {"question": row["query"], "standalone_question": None})
    if torch is not None:
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    started = perf_counter()
    error, ranked, observation = None, [], None
    try:
        ranked = reranker.rerank(row["query"], candidates)
        _check(len(ranked) == 10, "重排必须保存全部10名以评分k=10")
        if torch is not None:
            torch.cuda.synchronize()
            observation = _gpu_observation(scorer, torch)
        diagnostics = getattr(scorer, "last_diagnostics", None)
        if diagnostics is None and torch is None:
            diagnostics = [dict.fromkeys(("original_pair_tokens", "input_tokens", "passage_truncated")) for _ in candidates]
        _check(isinstance(diagnostics, list) and len(diagnostics) == 10
               and all(isinstance(item, dict) and set(item) == {
                   "original_pair_tokens", "input_tokens", "passage_truncated"} for item in diagnostics),
               "候选token诊断不完整")
        _validate_diagnostics(diagnostics, required=torch is not None)
    except Exception:
        error, ranked = {"type": "rerank_failed", "message": "本地重排或诊断失败；无替代排名、无自动重试"}, []
        diagnostics = [dict.fromkeys(("original_pair_tokens", "input_tokens", "passage_truncated")) for _ in candidates]
    elapsed = (perf_counter() - started) * 1000
    by_rank = {item.candidate_rank: (rank, item.rerank_score) for rank, item in enumerate(ranked, 1)}
    detail = {"question_id": row["question_id"], "query": row["query"], "error": error,
              "score_succeeded": reranker.scorer.last_score_succeeded,
              "candidates": [{"candidate_rank": rank, "rerank_rank": by_rank.get(rank, (None, None))[0],
                              "raw_logit": by_rank.get(rank, (None, None))[1], "hit": hit.model_dump(mode="json"),
                              **diagnostic} for rank, (hit, diagnostic) in enumerate(zip(candidates, diagnostics, strict=True), 1)],
              "selected_hits": [item.hit.model_dump(mode="json") for item in ranked[:5]],
              "timings_ms": {"total": elapsed,
                             **{field: getattr(scorer, attribute, None) for field, attribute in (
                                 ("scoring_total", "last_total_ms"), ("model_load", "last_load_ms"),
                                 ("model_inference", "last_inference_ms"))}},
              "gpu": observation}
    ranking = {"question_id": row["question_id"], "query": row["query"],
               "hits": [item.hit.model_dump(mode="json") for item in ranked], "error": error,
               "timings_ms": {**dict.fromkeys(PHASES), "rerank": elapsed}}
    return ranking, detail


def _required_files() -> set[str]:
    files = {"experiment_config.json", "evaluation_config.json", "index_manifest.json", "parent_manifest.json",
             "source_parent_status.json", "baseline_rankings.jsonl", "rerank_rankings.jsonl", "comparison.json",
             "score_details.jsonl", "case_details.jsonl", "warmup.json", "runtime.json", "status.json"}
    for branch in ("baseline", "rerank"):
        files.update(f"{branch}_retrieval/{name}" for name in ("config.json", "retrieval.jsonl", "summary.json"))
    return files


def collect_experiment(dataset_dir: Path, hybrid_dir: Path, config_path: Path, output_dir: Path, *,
                       scorer: object | None = None, cache_dir: Path | None = None) -> dict:
    data = prepare_experiment(dataset_dir, hybrid_dir, config_path)
    output = Path(output_dir)
    _check(not output.exists() and not output.is_symlink()
           and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name)), "结果目录已存在或run_id无效")
    for protected in (Path(dataset_dir), Path(hybrid_dir)):
        _check(not output.resolve().is_relative_to(protected.resolve())
               and not protected.resolve().is_relative_to(output.resolve()), "输出目录不得覆盖或包围冻结输入")
    config = RerankConfig(**data.config["descriptor"]["rerank"])
    runtime = {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
               "packages": _packages(), "injected_scorer": scorer is not None,
               "execution_mode": "injected_offline_scorer" if scorer is not None else "local_cuda_model",
               "precision": config.dtype, "requested_device": config.device,
               "cache_condition": "injected-offline-scorer" if scorer is not None else
                   "existing-local-snapshot; OS-cache-not-cleared; CUDA-context-initialized-before-warmup; first-model-load-in-process",
               "warmup_policy": "first-dev-10-pairs-once; excluded-from-evaluation; same-instance-reused-for-30-dev",
               "allocator_policy": "per-score-call-reset; resident-model-included; excludes-whole-card-and-non-PyTorch-memory"}
    torch = None
    if scorer is None:
        cache = Path(cache_dir) if cache_dir is not None else PROJECT_ROOT / "models/huggingface"
        runtime["model_artifact"] = _model_snapshot(cache, config)
        import torch as torch_runtime
        torch = torch_runtime
        _check(torch.cuda.is_available(), "所选CUDA设备不可用；不回退CPU")
        torch.cuda.init()
        properties = torch.cuda.get_device_properties(torch.cuda.current_device())
        runtime.update({"cuda_build_version": torch.version.cuda, "cuda_device_name": properties.name,
                        "compute_capability": list(torch.cuda.get_device_capability()),
                        "device_total_memory_bytes": properties.total_memory})
        scorer = LocalCrossEncoder(config, cache_dir=cache)
    recording = RecordingScorer(scorer)
    reranker = Reranker(recording, config=config)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "experiment_config.json", data.config)
    _write(output / "evaluation_config.json", data.config["evaluation_config"])
    for source, target in (("hybrid_rankings.jsonl", "baseline_rankings.jsonl"),
                           ("manifest.json", "parent_manifest.json"), ("index_manifest.json", "index_manifest.json"),
                           ("status.json", "source_parent_status.json")):
        (output / target).write_bytes((Path(hybrid_dir) / source).read_bytes())
    _write(output / "runtime.json", runtime)
    status = {"state": "running", "complete": False, "question_count": 0, "retrieval_errors": 0,
              "local_score_calls": 0, "attempted_pairs": 0, "successful_score_calls": 0,
              "scored_pairs": 0, "model_api_calls": 0, "qdrant_requests": 0,
              "pair_count_semantics": "scored=complete-finite-score-returns; attempted=input-pairs; failed-partial-batches-unknown",
              "manual_review": "pending", "day16_accepted": False}
    _write(output / "status.json", status)
    try:
        _, warmup = _pass(reranker, data.rows[0], torch=torch)
        _write(output / "warmup.json", {**warmup, "purpose": "load-and-first-pass; excluded-from-dev-metrics"})
        _check(warmup["error"] is None, "独立warmup失败，停止全部开发集评分")
        for i, row in enumerate(data.rows):
            ranking, detail = _pass(reranker, row, torch=torch)
            _line(output / "rerank_rankings.jsonl", ranking)
            _line(output / "score_details.jsonl", detail)
            status["question_count"] += 1
            status["retrieval_errors"] += ranking["error"] is not None
            if i < 2:
                _check(ranking["error"] is None, "前两题smoke失败，停止后续评分")
        summaries = {branch: run_evaluation(Path(dataset_dir), output / "evaluation_config.json",
                                           output / f"{branch}_rankings.jsonl", output / f"{branch}_retrieval")
                     for branch in ("baseline", "rerank")}
        categories = {q["question_id"]: q["category"] for q in data.questions}
        comparison = compare_retrieval(output / "baseline_retrieval", output / "rerank_retrieval", categories)
        _write(output / "comparison.json", comparison)
        scored = {branch: _rows(output / f"{branch}_retrieval/retrieval.jsonl") for branch in ("baseline", "rerank")}
        details = _rows(output / "score_details.jsonl")
        for i, question in enumerate(data.questions):
            _line(output / "case_details.jsonl", {
                "question_id": question["question_id"], "query": data.rows[i]["query"], "category": question["category"],
                "gold": data.labels[question["question_id"]]["evidence"],
                "branch_metrics": {branch: scored[branch][i]["metrics"] for branch in scored},
                "baseline_selected_hits": data.rows[i]["hits"][:5], "rerank_selected_hits": details[i]["selected_hits"],
                "comparison": comparison["per_question"][i]})
        status.update({"state": "recorded", "complete": True, "local_score_calls": recording.local_score_calls,
                       "attempted_pairs": recording.attempted_pairs,
                       "successful_score_calls": recording.successful_score_calls, "scored_pairs": recording.scored_pairs})
        _write(output / "status.json", status)
        sources = [*data.parent["source_sha256"], "src/project_copilot/rag/rerank.py", "evals/rerank_run.py",
                   "evals/configs/rerank_v3.json", "pyproject.toml", "uv.lock"]
        files = {str(p.relative_to(output)): _digest(p) for p in sorted(output.rglob("*")) if p.is_file()}
        _check(set(files) == _required_files(), "采集产物不完整")
        receipt = {"schema_version": 1, "run_id": output.name, "mode": data.config["mode"],
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "code": _code_state(),
                   "source_sha256": {name: _digest(PROJECT_ROOT / name) for name in sources},
                   "configuration_fingerprint": data.config["configuration_fingerprint"],
                   "dataset_fingerprint": DATASET_FINGERPRINT, "parent_manifest_sha256": PARENT_SHA256,
                   "timing_policy": TIMING_POLICY, "execution_mode": runtime["execution_mode"],
                   "status": status, "files": files}
        _write(output / "manifest.json", receipt)
        return {"status": "ok", "run_id": output.name, **status,
                "at_5": {branch: summary["at_k"]["5"] for branch, summary in summaries.items()}}
    except Exception:
        status.update({"state": "partial", "complete": False, "local_score_calls": recording.local_score_calls,
                       "attempted_pairs": recording.attempted_pairs,
                       "successful_score_calls": recording.successful_score_calls, "scored_pairs": recording.scored_pairs})
        _write(output / "status.json", status)
        raise


def _validate_diagnostics(diagnostics: list[dict], *, required: bool) -> None:
    for item in diagnostics:
        original, actual, truncated = (item.get(k) for k in ("original_pair_tokens", "input_tokens", "passage_truncated"))
        if not required and original is actual is truncated is None:
            continue
        _check(type(original) is int and type(actual) is int and original > 0 and 0 < actual <= 512
               and actual == min(original, 512) and type(truncated) is bool and truncated == (original > actual),
               "候选token诊断不符合512/only_second预算")


def _verify_detail(detail: dict, baseline: dict, ranking: dict | None, *, real: bool) -> None:
    _check(detail["question_id"] == baseline["question_id"] and detail["query"] == baseline["query"]
           and len(detail["candidates"]) == 10, "评分详情与原问题或10候选不一致")
    candidates = detail["candidates"]
    _check(all(set(item) == {"candidate_rank", "rerank_rank", "raw_logit", "hit",
                             "original_pair_tokens", "input_tokens", "passage_truncated"} for item in candidates)
           and [item["candidate_rank"] for item in candidates] == list(range(1, 11))
           and [item["hit"] for item in candidates] == baseline["hits"], "评分详情必须保留原候选原序及全部字段")
    _validate_diagnostics(candidates, required=real and detail["error"] is None)
    _check(set(detail["timings_ms"]) == {"total", "scoring_total", "model_load", "model_inference"}
           and _number(detail["timings_ms"]["total"])
           and all(v is None or _number(v) for v in detail["timings_ms"].values()), "评分详情耗时无效")
    if detail["error"] is None:
        _check(detail["score_succeeded"] is True
               and all(type(item["raw_logit"]) in (int, float) and math.isfinite(item["raw_logit"]) for item in candidates),
               "raw logit必须是有限非布尔实数")
        ordered = sorted(candidates, key=lambda item: (-item["raw_logit"], item["candidate_rank"]))
        _check([item["rerank_rank"] for item in ordered] == list(range(1, 11)), "新排名不符合logit及原序稳定排序")
        expected = [item["hit"] for item in ordered]
        _check(detail["selected_hits"] == expected[:5], "最终上下文必须为排名前5块")
    else:
        expected = []
        _check(type(detail["score_succeeded"]) is bool and not detail["selected_hits"]
               and all(item["raw_logit"] is item["rerank_rank"] is None for item in candidates),
               "失败题不能返回替代分数或排名")
    if ranking is not None:
        _check(ranking["hits"] == expected and ranking["error"] == detail["error"]
               and ranking["timings_ms"] == {**dict.fromkeys(PHASES), "rerank": detail["timings_ms"]["total"]},
               "排名、评分详情或本轮重排耗时不一致")


def verify_experiment(dataset_dir: Path, output_dir: Path) -> dict:
    """仅需公开封存和冻结数据；不加载模型/缓存，不重建当前Prompt或检查当前源码。"""
    output, dataset = Path(output_dir), Path(dataset_dir)
    receipt = _json(_read(output / "manifest.json"))
    actual = {str(p.relative_to(output)) for p in output.rglob("*") if p.is_file() and p != output / "manifest.json"}
    _check(actual == set(receipt["files"]) == _required_files(), "封存文件缺失或多出未登记文件")
    for name, digest in receipt["files"].items():
        path = output / name
        _check(path.resolve().is_relative_to(output.resolve()) and _digest(path) == digest, "封存产物字节已变化")
    config = _json(_read(output / "experiment_config.json"))
    _validate_descriptor(config["descriptor"])
    _check(PREFIX + fingerprint({k: v for k, v in config.items() if k != "configuration_fingerprint"})
           == config["configuration_fingerprint"] == receipt["configuration_fingerprint"]
           and config["timing_policy"] == receipt["timing_policy"] == TIMING_POLICY
           and config["answer_context"] == {"top_k": 5, "policy": "first-five-ranked-hits; no-answer-generation"},
           "实验配置指纹或公平比较合同无效")
    _check(_digest(output / "parent_manifest.json") == config["parent_manifest_sha256"]
           == receipt["parent_manifest_sha256"] == PARENT_SHA256, "保存的父归档不是冻结V2")
    parent = _json(_read(output / "parent_manifest.json"))
    for saved, original in (("baseline_rankings.jsonl", "hybrid_rankings.jsonl"),
                            ("index_manifest.json", "index_manifest.json"),
                            ("evaluation_config.json", "evaluation_config.json"), ("source_parent_status.json", "status.json")):
        _check(_digest(output / saved) == parent["files"][original], "父候选、历史耗时、索引或评分配置已变化")
    parent_config = config["parent_experiment_config"]
    _check("hybrid-experiment-v1-" + fingerprint({k: v for k, v in parent_config.items() if k != "configuration_fingerprint"})
           == parent_config["configuration_fingerprint"] == PARENT_FINGERPRINT
           and config["evaluation_config"] == parent_config["evaluation_config"]
           == _json(_read(output / "evaluation_config.json")), "父配置身份与本轮评分合同不一致")
    baseline, rankings = _rows(output / "baseline_rankings.jsonl"), _rows(output / "rerank_rankings.jsonl")
    questions, labels, catalog = _dataset_inputs(dataset, config["evaluation_config"], baseline,
                                               _json(_read(output / "index_manifest.json")))
    ids = [q["question_id"] for q in questions]
    details, cases = _rows(output / "score_details.jsonl"), _rows(output / "case_details.jsonl")
    _check(all([row.get("question_id") for row in rows] == ids for rows in (rankings, details, cases)),
           "所有逐题产物须完整同序覆盖30道dev题")
    runtime = _json(_read(output / "runtime.json"))
    _check(type(runtime["injected_scorer"]) is bool and runtime["execution_mode"] == receipt["execution_mode"]
           == ("injected_offline_scorer" if runtime["injected_scorer"] else "local_cuda_model")
           and runtime["precision"] == "float16" and runtime["requested_device"] == "cuda",
           "运行证据须区分注入评分与实际CUDA模型")
    if not runtime["injected_scorer"]:
        artifact = runtime["model_artifact"]
        _check(artifact["evidence_path"] == MODEL_EVIDENCE and artifact["evidence_sha256"] == MODEL_EVIDENCE_SHA256
               and artifact["model"] == config["descriptor"]["rerank"]["model_name"]
               and artifact["revision"] == config["descriptor"]["rerank"]["revision"]
               and artifact["files"]["model.safetensors"] == {"bytes": 2271071852,
                   "sha256": "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"},
               "真实运行须保存已验证revision及权重SHA来源")
    warmup = _json(_read(output / "warmup.json"))
    _verify_detail(warmup, baseline[0], None, real=not runtime["injected_scorer"])
    _check(warmup["error"] is None and warmup["purpose"] == "load-and-first-pass; excluded-from-dev-metrics",
           "独立warmup未成功")
    for before, after, detail, question in zip(baseline, rankings, details, questions, strict=True):
        _snapshot_row(after, question)
        _verify_detail(detail, before, after, real=not runtime["injected_scorer"])
    _check(all(row["error"] is None for row in rankings[:2]), "前两道smoke失败不能封存")
    categories = {q["question_id"]: q["category"] for q in questions}
    for branch, rows in (("baseline", baseline), ("rerank", rankings)):
        scored = _rows(output / f"{branch}_retrieval/retrieval.jsonl")
        _check([row.get("question_id") for row in scored] == ids, "评分产物须包含完整30题")
        evaluation_receipt = _json(_read(output / f"{branch}_retrieval/config.json"))
        _check(evaluation_receipt["evaluation_config"] == config["evaluation_config"]
               and evaluation_receipt["input_sha256"] == {
                   "config": _digest(output / "evaluation_config.json"), "rankings": _digest(output / f"{branch}_rankings.jsonl")},
               "评分收据与排名或配置不一致")
        for row, record, question in zip(rows, scored, questions, strict=True):
            hits, timings, error = _snapshot_row(row, question)
            _check(record["query"] == row["query"] and record["timings_ms"] == timings and record["error"] == error
                   and record["hits"] == [{"rank": rank, **hit.model_dump(mode="json")} for rank, hit in enumerate(hits, 1)]
                   and record["metrics"] == evaluate_ranking(hits, labels[row["question_id"]], catalog),
                   "公开评分不能由排名与冻结gold重现")
        summary = _json(_read(output / f"{branch}_retrieval/summary.json"))
        _check(all(summary.get(k) == v for k, v in _aggregate(scored, [1, 3, 5, 10]).items()), "评分汇总与逐题记录不同")
    comparison = compare_retrieval(output / "baseline_retrieval", output / "rerank_retrieval", categories)
    _check(comparison == _json(_read(output / "comparison.json")), "成对对照无法由公开评分重现")
    scored = {branch: _rows(output / f"{branch}_retrieval/retrieval.jsonl") for branch in ("baseline", "rerank")}
    for i, (case, question) in enumerate(zip(cases, questions, strict=True)):
        _check(case == {"question_id": ids[i], "query": baseline[i]["query"], "category": question["category"],
                       "gold": labels[ids[i]]["evidence"],
                       "branch_metrics": {branch: scored[branch][i]["metrics"] for branch in scored},
                       "baseline_selected_hits": baseline[i]["hits"][:5], "rerank_selected_hits": details[i]["selected_hits"],
                       "comparison": comparison["per_question"][i]}, "Bad Case详情与公开排名或gold不同")
    status = _json(_read(output / "status.json"))
    _check(status == receipt["status"] and status["state"] == "recorded" and status["complete"] is True
           and status["question_count"] == 30 and status["local_score_calls"] == 31 and status["attempted_pairs"] == 310
           and status["successful_score_calls"] == 1 + sum(detail["score_succeeded"] for detail in details)
           and status["scored_pairs"] == 10 * status["successful_score_calls"]
           and status["retrieval_errors"] == sum(row["error"] is not None for row in rankings)
           and status["pair_count_semantics"] == "scored=complete-finite-score-returns; attempted=input-pairs; failed-partial-batches-unknown"
           and status["model_api_calls"] == status["qdrant_requests"] == 0
           and status["manual_review"] == "pending" and status["day16_accepted"] is False,
           "采集状态、调用预算或人工验收状态不一致")
    return {"status": "ok", "state": "verified", "run_id": receipt["run_id"], "files": len(actual),
            "manual_review": "pending", "day16_accepted": False, "model_loaded": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "collect", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        if name != "verify":
            sub.add_argument("--hybrid-dir", "--parent-dir", dest="hybrid_dir", type=Path, required=True)
            sub.add_argument("--config", type=Path, required=True)
        if name != "plan":
            sub.add_argument("--output-dir", type=Path, required=True)
        if name == "collect":
            sub.add_argument("--local", action="store_true")
            sub.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    if args.command == "collect" and not args.local:
        print(json.dumps({"status": "error", "message": "本地模型评分必须显式添加--local；未读取模型或执行评分"}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        if args.command == "verify":
            result = verify_experiment(args.dataset, args.output_dir)
        elif args.command == "plan":
            result = plan_experiment(args.dataset, args.hybrid_dir, args.config)
        else:
            result = collect_experiment(args.dataset, args.hybrid_dir, args.config, args.output_dir, cache_dir=args.cache_dir)
    except Exception:
        print(json.dumps({"status": "error", "message": "本地重排实验校验或采集失败；检查冻结输入、本地快照、CUDA及局部status，无下载或自动重试"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
