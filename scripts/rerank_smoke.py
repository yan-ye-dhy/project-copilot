"""D15：显式--local后，用公开D14的一题10个候选验证本地BGE重排。"""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = PROJECT_ROOT / "evals/datasets/copilot_v0"
ARCHIVE_DIR = PROJECT_ROOT / "evals/results/hybrid-v2-20261022-01"
QUESTION_ID = "pc-d7-012"
ERROR_MESSAGES = {
    "missing_model": "本地模型快照缺失；请先准备冻结revision对应缓存，本脚本不自动下载",
    "invalid_input": "冻结D14输入不满足本地单题10候选合同，未执行重排",
    "invalid_output": "本地重排输出或诊断不完整，未生成替代结果",
}


class SmokeError(ValueError):
    """只使用固定错误码，不向stderr回显输入、物理路径或底层异常。"""


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _load_smoke_input() -> tuple[dict, list, dict]:
    # 默认门禁通过之后才导入项目依赖；verify不会创建Qdrant或模型客户端。
    from evals.hybrid_run import verify_experiment
    from project_copilot.schemas.retrieval import RetrievedChunk

    verification = verify_experiment(DATASET_DIR, ARCHIVE_DIR)
    rows = [row for row in _rows(ARCHIVE_DIR / "hybrid_rankings.jsonl")
            if row.get("question_id") == QUESTION_ID]
    questions = [row for row in _rows(DATASET_DIR / "questions.jsonl")
                 if row.get("question_id") == QUESTION_ID]
    split = json.loads((DATASET_DIR / "split_manifest.json").read_text(encoding="utf-8"))
    if len(rows) != 1 or len(questions) != 1 or QUESTION_ID not in split["dev"]:
        raise SmokeError("invalid_input")
    row, question = rows[0], questions[0]
    query = question["standalone_question"] or question["question"]
    if row.get("error") is not None or row.get("query") != query or len(row.get("hits", [])) != 10:
        raise SmokeError("invalid_input")
    candidates = [RetrievedChunk.model_validate(hit) for hit in row["hits"]]
    if len({hit.metadata.chunk_id for hit in candidates}) != 10:
        raise SmokeError("invalid_input")
    receipt = json.loads((ARCHIVE_DIR / "manifest.json").read_text(encoding="utf-8"))
    source = {
        "run_id": verification["run_id"],
        "archive_verified": verification["state"] == "verified",
        "dataset_fingerprint": receipt["dataset_fingerprint"],
        "configuration_fingerprint": receipt["configuration_fingerprint"],
        "archive_manifest_sha256": sha256((ARCHIVE_DIR / "manifest.json").read_bytes()).hexdigest(),
        "hybrid_rankings_sha256": sha256((ARCHIVE_DIR / "hybrid_rankings.jsonl").read_bytes()).hexdigest(),
        "candidate_policy": "pc-d7-012; first-10-archived-hybrid-hits; original-order",
    }
    return row, candidates, source


class RecordingScorer:
    """保留同一次模型评分的全部10个logit；不为记录而增加模型推断。"""

    def __init__(self, scorer: object) -> None:
        self.scorer = scorer
        self.scores: list[float] = []

    def score(self, query: str, passages: list[str]) -> list[float]:
        self.scores = list(self.scorer.score(query, passages))
        return self.scores


def _pass_result(reranker: object, recording: RecordingScorer, query: str, candidates: list) -> dict:
    started = perf_counter()
    selected = reranker.rerank(query, candidates)
    elapsed_ms = (perf_counter() - started) * 1000
    scorer = recording.scorer
    diagnostics = scorer.last_diagnostics
    if len(recording.scores) != 10 or len(diagnostics) != 10 or len(selected) != 5:
        raise SmokeError("invalid_output")
    return {
        "scored_candidates": [{
            "candidate_rank": rank,
            "chunk_id": str(hit.metadata.chunk_id),
            "original_score": hit.score,
            "text_sha256": sha256(hit.text.encode("utf-8")).hexdigest(),
            "raw_logit": score,
            **diagnostic,
        } for rank, (hit, score, diagnostic) in enumerate(
            zip(candidates, recording.scores, diagnostics, strict=True), start=1
        )],
        "selected_hits": [{
            "rerank_rank": rank,
            "candidate_rank": item.candidate_rank,
            "rerank_score": item.rerank_score,
            "hit": item.hit.model_dump(mode="json"),
        } for rank, item in enumerate(selected, start=1)],
        "truncated_pairs": sum(item["passage_truncated"] for item in diagnostics),
        "timings_ms": {
            "total": elapsed_ms,
            "scoring_total": scorer.last_total_ms,
            "model_load": scorer.last_load_ms,
            "model_inference": scorer.last_inference_ms,
        },
    }


def run_smoke(*, cache_dir: Path, device: str, dtype: str) -> dict:
    row, candidates, source = _load_smoke_input()
    from project_copilot.rag.rerank import LocalCrossEncoder, RerankConfig, Reranker

    config = RerankConfig(device=device, dtype=dtype)
    snapshot = cache_dir / ("models--" + config.model_name.replace("/", "--")) / "snapshots" / config.revision
    if not snapshot.is_dir():
        raise SmokeError("missing_model")
    scorer = LocalCrossEncoder(config, cache_dir=cache_dir)
    recording = RecordingScorer(scorer)
    reranker = Reranker(recording, config=config)
    first = _pass_result(reranker, recording, row["query"], candidates)
    # 有意重复同一输入以观察加载后耗时；异常直接失败，不吞异常或自动重试。
    warm = _pass_result(reranker, recording, row["query"], candidates)
    return {
        "status": "ok",
        "scope": "D15-single-question-local-smoke; no-dev-metrics-or-answer-evaluation",
        "question_id": QUESTION_ID,
        "query": row["query"],
        "source": source,
        "config": asdict(config),
        "truncation_policy": "only_second; full-query-preserved; reject-query-budget-exhausted; original-hit-text-preserved",
        "score_semantics": "cross-encoder-raw-logit; no-sigmoid; original-score-preserved",
        "external_model_api_calls": 0,
        "local_rerank_invocations": 2,
        "scored_pairs": 20,
        "qdrant_requests": 0,
        "model_load_ms": scorer.load_ms,
        "timing_policy": "load-separate; first-and-warm-same-10-pairs; model-inference-excludes-tokenization; no-retry",
        "first_pass": first,
        "warm_repeat": warm,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="允许本地加载冻结模型并评分两遍")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "models/huggingface",
                        help="预先准备的Hugging Face缓存目录；不自动下载")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--dtype", choices=("float32", "float16"), default="float32")
    args = parser.parse_args()
    if not args.local:
        parser.error("本地模型推断需显式添加--local；未加载模型或读取评测输入")
    try:
        # 直接运行scripts路径时，离线evals包位于项目根目录而非安装的src中。
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        result = run_smoke(cache_dir=args.cache_dir, device=args.device, dtype=args.dtype)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except Exception as exc:
        message = ERROR_MESSAGES.get(str(exc)) if isinstance(exc, SmokeError) else None
        print(json.dumps({"status": "error", "message": message or
              "本地重排验证失败；检查冻结D14归档、模型缓存和设备配置，无自动下载或重试"},
              ensure_ascii=False), file=sys.stderr)
        return 1
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
