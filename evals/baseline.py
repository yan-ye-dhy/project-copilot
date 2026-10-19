"""D10：显式授权的真实Dense采集，复用D9评分并保存不可覆盖的实验记录。"""

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import re
import time

from evals.baseline_config import assert_runtime_matches, validate_baseline_config
from evals.cache import CachedEmbeddings
from evals.metrics import EvaluationError, build_chunk_catalog, evaluate_ranking
from evals.run import PHASES, _code_state, _json, _read, _rows, run_evaluation
from project_copilot.config import PROJECT_ROOT, Settings, load_settings
from project_copilot.infrastructure.models import create_chat_client, create_embedding_client
from project_copilot.infrastructure.qdrant import create_qdrant_store
from project_copilot.rag.answering import RagAnswerer
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _line(path: Path, value: dict) -> None:
    # 每题立即保存，即使后续失败，也不丢弃已完成的真实请求记录。
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        handle.flush()


def _inputs(dataset_dir: Path, config_path: Path) -> tuple[dict, list, list, dict, dict]:
    config = validate_baseline_config(_json(_read(config_path)), dataset_dir)
    evaluation = config["evaluation_config"]
    splitter = SplitConfig.model_validate(evaluation["splitter"])
    documents = [load_document(dataset_dir / s["path"]) for s in _rows(dataset_dir / "sources.jsonl")]
    catalog = build_chunk_catalog(documents, splitter)
    ids = _json(_read(dataset_dir / "split_manifest.json"))["dev"]
    questions = {q["question_id"]: q for q in _rows(dataset_dir / "questions.jsonl")}
    labels = {q["question_id"]: q for q in _rows(dataset_dir / "labels.draft.jsonl")}
    return config, documents, [questions[qid] for qid in ids], labels, catalog


def plan_baseline(dataset_dir: Path, config_path: Path) -> dict:
    """仅使用冻结输入；不读.env、不构造客户端、不创建输出目录。"""
    config, documents, questions, _, catalog = _inputs(dataset_dir, config_path)
    chunks = [item.chunk.text for item in catalog.values()]
    queries = [q["standalone_question"] or q["question"] for q in questions]
    seen = set()
    batches = 0
    for texts in (chunks, queries[:2], queries[2:]):
        missing = set(texts) - seen
        batches += math.ceil(len(missing) / config["embedding"]["batch_size"])
        seen.update(texts)
    return {"mode": "offline_plan", "dataset_fingerprint": config["dataset_fingerprint"],
            "configuration_fingerprint": config["configuration_fingerprint"],
            "documents": len(documents), "chunks": len(chunks), "questions": len(questions),
            "chunk_chars": sum(map(len, chunks)), "query_chars": sum(map(len, queries)),
            "cold_embedding_batches": batches, "smoke_questions": min(2, len(queries)),
            "answer_sample_ids": config["answer_sample_ids"],
            "max_answer_calls": len(config["answer_sample_ids"]), "model_calls": 0}


class _SnapshotRetriever:
    def __init__(self, query: str, hits: list) -> None:
        self.query, self.hits = query, hits

    def search(self, query: str) -> list:
        if query != self.query:
            raise EvaluationError("回答问题与已采集检索快照不一致")
        return self.hits


class _RecordingChat:
    """复用D5适配器，额外保留消息及原始文本，包括Schema校验失败的输出。"""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.model, self.temperature, self.max_tokens = delegate.model, delegate.temperature, delegate.max_tokens
        self.last_messages = None
        self.last_generation = None
        self.calls = 0

    def generate_json(self, messages):
        self.last_messages = messages
        self.calls += 1
        self.last_generation = self.delegate.generate_json(messages)
        return self.last_generation


def _answers(output: Path, config: dict, questions: list, labels: dict,
             snapshots: dict, chat, index_version: str) -> tuple[int, int]:
    recording = chat
    failures = 0
    by_id = {q["question_id"]: q for q in questions}
    for qid in config["answer_sample_ids"]:
        question, snapshot = by_id[qid], snapshots[qid]
        recording.last_messages = recording.last_generation = None
        started = time.monotonic()
        result, error = None, None
        try:
            if snapshot["error"]:
                raise EvaluationError("回答样本的检索失败")
            # 生成使用冻结的最终k，不重新Embedding或查询Qdrant。
            from project_copilot.schemas.retrieval import RetrievedChunk
            hits = [RetrievedChunk.model_validate(h) for h in snapshot["hits"][:config["retrieval"]["answer_top_k"]]]
            answerer = RagAnswerer(_SnapshotRetriever(snapshot["query"], hits), recording,
                                  index_version=index_version,
                                  context_max_chars=config["prompt"]["context_policy"]["max_chars"])
            result = answerer.answer(snapshot["query"]).model_dump(mode="json")
        except (RuntimeError, ValueError):
            failures += 1
            error = {"type": "answer_failed", "message": "回答请求或JSON/引用校验失败；无自动重试"}
        raw = recording.last_generation
        record = {"question_id": qid, "category": question["category"],
                  "question": question["question"], "query": snapshot["query"],
                  "messages": recording.last_messages, "raw_content": raw.content if raw else None,
                  "actual_model": raw.model if raw else None, "usage": raw.usage if raw else None,
                  "answer": result, "error": error, "elapsed_ms": (time.monotonic() - started) * 1000,
                  "reference_answer": labels[qid]["reference_answer"],
                  "gold_answerable": labels[qid]["answerable"]}
        _line(output / "answers.jsonl", record)
        # 人工声明拆分与支持判断不能由程序自动确认。缺引用声明同样需要登记。
        _line(output / "review.jsonl", {"question_id": qid, "review_status": "pending",
              "reviewed_by": None, "reviewed_at": None, "answer_correct": None,
              "refusal_correct": None, "claims": [], "notes": "请人工列出全部需证据声明及支持引用；缺引用也计未支持。纯拒答支持率N/A，错误拒答仍计失败。"})
    return recording.calls, failures


def collect_baseline(dataset_dir: Path, config_path: Path, output_dir: Path,
                     cache_dir: Path, settings: Settings, *, embeddings, store, chat=None) -> dict:
    """依赖显式注入；真实客户端仅由CLI在--live之后构造。"""
    root, output, cache_path = Path(dataset_dir).resolve(), Path(output_dir), Path(cache_dir).resolve()
    config, documents, questions, labels, catalog = _inputs(root, config_path)
    assert_runtime_matches(config, settings)
    if output.exists() or output.is_symlink() or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name):
        raise EvaluationError("结果目录已存在或run_id不合法，拒绝覆盖")
    if output.resolve().is_relative_to(root) or cache_path.is_relative_to(root):
        raise EvaluationError("结果及缓存不能写入冻结数据集")
    if output.resolve() == cache_path or cache_path.is_relative_to(output.resolve()):
        raise EvaluationError("可复用缓存必须与本次结果目录分开")
    spec = DenseIndexSpec.model_validate({k: v for k, v in config["index"].items() if k != "index_version"})
    cached = CachedEmbeddings(embeddings, cache_path, base_url=config["embedding"]["provider_url"])
    manifest = ManifestStore(output / "index_manifests", spec)
    splitter = SplitConfig.model_validate(config["evaluation_config"]["splitter"])
    # validate_index_binding在构造时先拒绝错误适配，避免付费后才发现集合错误。
    ingestion = IngestionService(cached, store, manifest, split_config=splitter)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "baseline_config.json", config)
    _write(output / "evaluation_config.json", config["evaluation_config"])
    _write(output / "status.json", {"state": "running", "complete": False})
    batch_events, snapshots = [], {}
    chat_calls = answer_failures = 0
    recording = _RecordingChat(chat) if chat is not None else None

    def embed_batch(name, texts):
        before, started = cached.stats(), time.monotonic()
        try:
            return cached.embed_documents(texts)
        finally:
            after = cached.stats()
            event = {"phase": name, "text_count": len(texts),
                     "elapsed_ms": (time.monotonic() - started) * 1000,
                     "cache": {key: after[key] - before[key] for key in before}}
            batch_events.append(event)
            _write(output / "embedding_batches.json", batch_events)

    def retrieve(question):
        qid = question["question_id"]
        query = question["standalone_question"] or question["question"]
        timings, error, hits = dict.fromkeys(PHASES), None, []
        started = time.monotonic()
        try:
            embedding_started = time.monotonic()
            vector = cached.embed_query(query)
            timings["embedding"] = (time.monotonic() - embedding_started) * 1000
            _line(output / "query_vectors.jsonl", {"question_id": qid, "query": query, "vector": vector,
                                                   "embedding_contract": config["embedding"]})
            search_started = time.monotonic()
            try:
                results = store.query(vector, document_ids=[d.document_id for d in manifest.load().documents],
                                      dense_index_version=spec.index_version, limit=config["retrieval"]["top_k"])
            finally:
                timings["search"] = (time.monotonic() - search_started) * 1000
            evaluate_ranking(results, labels[qid], catalog, tuple(config["evaluation_config"]["ks"]))
            hits = [h.model_dump(mode="json") for h in results]
        except (RuntimeError, ValueError):
            error = {"type": "retrieval_failed", "message": "真实检索或片段身份校验失败；无自动重试"}
        timings["total"] = (time.monotonic() - started) * 1000
        row = {"question_id": qid, "query": query, "hits": hits, "timings_ms": timings, "error": error}
        snapshots[qid] = row
        _line(output / "rankings.jsonl", row)
        return error is None

    try:
        store.ensure_collection()
        embed_batch("document_vectors", [item.chunk.text for item in catalog.values()])
        for source in _rows(root / "sources.jsonl"):
            ingestion.ingest_file(root / source["path"])
        completed = manifest.load()
        if {str(c) for d in completed.documents for c in d.chunk_ids} != set(catalog):
            raise EvaluationError("成功清单与当前冻结语料片段不一致")
        _write(output / "index_manifest.json", completed.model_dump(mode="json"))
        smoke = questions[:2]
        embed_batch("smoke_query_vectors", [q["standalone_question"] or q["question"] for q in smoke])
        # 两题都实际查询；smoke失败即停止后续付费批次，已有结果仍保留。
        if not all([retrieve(question) for question in smoke]):
            raise EvaluationError("真实smoke失败，未执行剩余问题Embedding或回答")
        remaining = questions[2:]
        embed_batch("remaining_query_vectors", [q["standalone_question"] or q["question"] for q in remaining])
        for question in remaining:
            retrieve(question)
        summary = run_evaluation(root, output / "evaluation_config.json", output / "rankings.jsonl", output / "retrieval")
        if chat is not None:
            chat_calls, answer_failures = _answers(output, config, questions, labels, snapshots, recording, spec.index_version)
        status = {"state": "recorded", "complete": True,
                  "retrieval_errors": summary["retrieval_error_count"], "answer_errors": answer_failures,
                  "answers_requested": chat is not None,
                  "human_review": "pending" if chat is not None else "not_collected",
                  "day10_accepted": False,
                  "model_calls": {"embedding": cached.stats()["api_calls"], "chat": chat_calls}}
        _write(output / "status.json", status)
        source_paths = ["evals/baseline.py", "evals/baseline_config.py", "evals/cache.py",
                        "evals/run.py", "evals/metrics.py", "src/project_copilot/config.py",
                        "src/project_copilot/infrastructure/models.py", "src/project_copilot/infrastructure/qdrant.py",
                        "src/project_copilot/rag/answering.py", "src/project_copilot/rag/ingestion.py",
                        "src/project_copilot/rag/retrieval.py", "src/project_copilot/rag/loaders.py",
                        "src/project_copilot/rag/splitters.py", "src/project_copilot/schemas/rag.py",
                        "src/project_copilot/schemas/documents.py", "src/project_copilot/schemas/retrieval.py"]
        receipt = {"format_version": 1, "run_id": output.name, "mode": "live_dense_baseline",
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "code": _code_state(),
                   "source_sha256": {name: sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() for name in source_paths},
                   "configuration_fingerprint": config["configuration_fingerprint"],
                   "dataset_fingerprint": config["dataset_fingerprint"],
                   "qdrant_url": str(settings.qdrant_url), "collection_name": spec.collection_name,
                   "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                               "machine": platform.machine(), "cpu": platform.processor() or None,
                               "dependencies": {name: version(name) for name in
                                                ("openai", "qdrant-client", "langchain-text-splitters", "pydantic")}},
                   "cache_stats": cached.stats(), "model_calls": status["model_calls"],
                   "embedding_usage": None, "actual_billed_cost": None,
                   "timing_condition": "query-vector-cache-hit; provider-batch-time-separate; no-explicit-warmup",
                   "retrieval_evaluation": "D9 replay of actual Qdrant rankings; replay model_calls=0 is scoring only",
                   "human_review": status["human_review"], "day10_accepted": False,
                   "files": {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
                             for p in sorted(output.rglob("*")) if p.is_file() and p.name != "review.jsonl"}}
        _write(output / "manifest.json", receipt)
        return {"status": "ok", "run_id": output.name, **status,
                "at_k": summary["at_k"], "timings_ms": summary["timings_ms"]}
    except (RuntimeError, ValueError, OSError):
        _write(output / "status.json", {"state": "failed", "complete": False,
               "model_calls": {"embedding": cached.stats()["api_calls"], "chat": recording.calls if recording else 0},
               "message": "采集未完整完成；已发生的请求和局部文件保留，禁止冒充冻结基线"})
        raise


def verify_baseline(dataset_dir: Path, output_dir: Path) -> dict:
    """冻结机器产物按字节核验；人工review另行维护，不能据此认定人工已通过。"""
    output = Path(output_dir)
    manifest = _json(_read(output / "manifest.json"))
    config = validate_baseline_config(_json(_read(output / "baseline_config.json")), dataset_dir)
    status = _json(_read(output / "status.json"))
    if (manifest.get("configuration_fingerprint") != config["configuration_fingerprint"]
            or manifest.get("dataset_fingerprint") != config["dataset_fingerprint"]
            or manifest.get("run_id") != output.name):
        raise EvaluationError("基线配置身份不一致")
    if (status.get("state") != "recorded" or status.get("complete") is not True
            or status.get("day10_accepted") is not False
            or manifest.get("human_review") not in ("pending", "not_collected")
            or manifest.get("human_review") != status.get("human_review")):
        raise EvaluationError("基线采集未完成或人工状态不一致")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise EvaluationError("基线产物清单缺失")
    required = {"baseline_config.json", "evaluation_config.json", "index_manifest.json", "status.json",
                "embedding_batches.json", "query_vectors.jsonl", "rankings.jsonl",
                "retrieval/config.json", "retrieval/retrieval.jsonl", "retrieval/summary.json"}
    if status.get("answers_requested"):
        required.add("answers.jsonl")
    actual = {str(p.relative_to(output)) for p in output.rglob("*")
              if p.is_file() and p not in (output / "manifest.json", output / "review.jsonl")}
    if not required <= set(files) or actual != set(files):
        raise EvaluationError("基线产物清单遗漏文件或出现未封存产物")
    for name, digest in files.items():
        path = output / name
        if not path.resolve().is_relative_to(output.resolve()) or not path.is_file() or sha256(path.read_bytes()).hexdigest() != digest:
            raise EvaluationError("基线产物缺失或已变化")
    return {"state": "verified", "run_id": manifest["run_id"], "files": len(files),
            "human_review": manifest["human_review"], "day10_accepted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "collect", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        if name != "verify":
            sub.add_argument("--config", type=Path, required=True)
        if name != "plan":
            sub.add_argument("--output-dir", type=Path, required=True)
        if name == "collect":
            sub.add_argument("--cache-dir", type=Path, required=True)
            sub.add_argument("--live", action="store_true")
            sub.add_argument("--answers", action="store_true", help="额外调用八题回答；须单独获得费用范围授权")
    args = parser.parse_args()
    if args.command == "collect" and not args.live:
        parser.error("真实基线需显式--live；批量模型调用前须确认范围与费用")
    try:
        if args.command == "plan":
            result = plan_baseline(args.dataset, args.config)
        elif args.command == "verify":
            result = verify_baseline(args.dataset, args.output_dir)
        else:
            settings = load_settings()
            # 构造任何客户端前核对完整冻结合同和输出，密钥不写入记录。
            config = validate_baseline_config(_json(_read(args.config)), args.dataset)
            assert_runtime_matches(config, settings)
            if args.output_dir.exists():
                raise EvaluationError("结果目录已存在，拒绝覆盖")
            eval_settings = settings.model_copy(update={"qdrant_collection_name": config["index"]["collection_name"]})
            with ExitStack() as stack:
                embeddings = stack.enter_context(create_embedding_client(eval_settings))
                store = stack.enter_context(create_qdrant_store(eval_settings))
                chat = stack.enter_context(create_chat_client(eval_settings)) if args.answers else None
                result = collect_baseline(args.dataset, args.config, args.output_dir, args.cache_dir,
                                          eval_settings, embeddings=embeddings, store=store, chat=chat)
    except (RuntimeError, ValueError, OSError):
        print(json.dumps({"status": "error", "message": "基线采集或校验失败；检查环境、冻结配置及局部status记录，无自动重试"}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
