"""D12：固定历史问题向量，独立索引并成对比较V0与单因素切分候选。"""

import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import re
import time

from evals.baseline import _line, _write, verify_baseline
from evals.baseline_config import assert_runtime_matches, fingerprint, validate_baseline_config
from evals.cache import CachedEmbeddings
from evals.chunking import _boundary, inspect_chunking
from evals.comparison import compare_retrieval
from evals.freeze_dataset import verify_freeze
from evals.metrics import EvaluationError, _fully_covered, build_chunk_catalog, evaluate_ranking
from evals.run import PHASES, _code_state, _json, _read, _rows, run_evaluation
from project_copilot.config import PROJECT_ROOT, Settings, load_settings
from project_copilot.infrastructure.models import create_embedding_client
from project_copilot.infrastructure.qdrant import create_qdrant_store
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec, IndexManifest


CANDIDATE_COLLECTION = "pc_eval_dense_v1_size500"


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise EvaluationError(message)


class _NoRequests:
    model = "embedding-3"
    expected_dimensions = 2048
    batch_size = 64

    def embed_documents(self, texts):
        raise EvaluationError("只读准备阶段不得请求Embedding")


@dataclass
class Inputs:
    config: dict
    documents: list
    questions: list[dict]
    labels: dict
    catalogs: dict
    vectors: dict
    specs: dict
    cache_plan: dict
    gold_mapping: list


def _map_gold(questions, labels, catalogs) -> list:
    """所有dev的确定性原文映射先离线验证，不能付费入库后才发现不充分。"""
    mapping = []
    for question in questions:
        for unit in labels[question["question_id"]]["evidence"]:
            sides = {}
            for side, catalog in catalogs.items():
                detail = _boundary(unit, catalog)
                _check(_fully_covered(unit, list(catalog.values())), "候选无法按原文连续并集完整映射gold")
                sides[side] = {"union_covers_quote": True, "single_chunk_covering_ids": detail["single_chunk_covering_ids"],
                               "intersecting_chunks": [{k: v for k, v in chunk.items() if k != "text"}
                                                       for chunk in detail["intersecting_chunks"]]}
            mapping.append({"question_id": question["question_id"], "gold": unit, **sides})
    return mapping


def prepare_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path,
                       candidate_path: Path, cache_dir: Path) -> Inputs:
    dataset, baseline, private = map(Path, (dataset_dir, baseline_dir, baseline_private_dir))
    inspection = inspect_chunking(dataset, baseline, candidate_path)
    archive = _json(_read(baseline / "archive_manifest.json"))
    for name, digest in archive["files"].items():
        path = baseline / name
        _check(path.resolve().is_relative_to(baseline.resolve())
               and sha256(path.read_bytes()).hexdigest() == digest, "公开V0机器归档已变化")
    _check(sha256((private / "manifest.json").read_bytes()).hexdigest()
           == archive["source_manifest_sha256"], "私有V0归档不属于该公开基线")
    verify_baseline(dataset, private)
    for name, digest in _json(_read(private / "manifest.json"))["source_sha256"].items():
        path = PROJECT_ROOT / name
        _check(path.resolve().is_relative_to(PROJECT_ROOT)
               and sha256(path.read_bytes()).hexdigest() == digest, "历史运行源码已漂移，不能沿用原V0合同")
    original = _json(_read(baseline / "baseline_config.json"))
    descriptor = _json(_read(candidate_path))
    candidate = deepcopy(original)
    candidate["baseline_id"] = descriptor["candidate_id"]
    candidate["evaluation_config"]["splitter"] = descriptor["splitter"]
    candidate_spec = DenseIndexSpec(
        collection_name=CANDIDATE_COLLECTION, embedding_model=original["embedding"]["model"],
        dimensions=original["embedding"]["dimensions"], splitter_version=inspection["splitter_versions"]["candidate"],
    )
    candidate["index"] = {**candidate_spec.model_dump(mode="json"), "index_version": candidate_spec.index_version}
    candidate.pop("configuration_fingerprint")
    candidate["configuration_fingerprint"] = "chunking-config-v1-" + fingerprint(candidate)
    specs = {"baseline": DenseIndexSpec.model_validate({k: v for k, v in original["index"].items()
                                                       if k != "index_version"}), "candidate": candidate_spec}
    documents = [load_document(dataset / row["path"]) for row in _rows(dataset / "sources.jsonl")]
    catalogs = {side: build_chunk_catalog(documents, SplitConfig.model_validate(config["evaluation_config"]["splitter"]))
                for side, config in (("baseline", original), ("candidate", candidate))}
    ids = _json(_read(dataset / "split_manifest.json"))["dev"]
    by_id = {q["question_id"]: q for q in _rows(dataset / "questions.jsonl")}
    questions = [by_id[qid] for qid in ids]
    labels = {q["question_id"]: q for q in _rows(dataset / "labels.draft.jsonl") if q["question_id"] in ids}
    gold_mapping = _map_gold(questions, labels, catalogs)
    vector_path = private / "query_vectors.jsonl"
    rows = _rows(vector_path)
    _check([row.get("question_id") for row in rows] == ids, "历史问题向量须完整、同序覆盖全部dev题")
    vectors = {}
    for row, question in zip(rows, questions, strict=True):
        _check(set(row) == {"question_id", "query", "vector", "embedding_contract"}
               and row["query"] == (question["standalone_question"] or question["question"])
               and row["embedding_contract"] == original["embedding"], "历史问题向量的查询或模型合同不匹配")
        vector = row["vector"]
        _check(isinstance(vector, list) and len(vector) == original["embedding"]["dimensions"]
               and all(type(v) in (int, float) and math.isfinite(v) for v in vector), "历史问题向量维度或数值无效")
        vectors[row["question_id"]] = vector
    sealed_index = IndexManifest.model_validate_json(_read(baseline / "index_manifest.json"))
    _check(sealed_index.spec == specs["baseline"]
           and {str(cid) for doc in sealed_index.documents for cid in doc.chunk_ids} == set(catalogs["baseline"]),
           "原V0完成清单与原文目录不一致")
    cache = CachedEmbeddings(_NoRequests(), cache_dir, base_url=original["embedding"]["provider_url"])
    missing = []
    texts = list(dict.fromkeys(item.chunk.text for item in catalogs["candidate"].values()))
    # 复用D10缓存身份/损坏校验；仅窥查，不调用embed_documents或生成缓存。
    for text in texts:
        identity, path = cache._identity(text)
        if cache._load(path, identity) is None:
            missing.append(text)
    cache_plan = {"unique_texts": len(texts), "cached_texts": len(texts) - len(missing),
                  "missing_texts": len(missing), "missing_chars": sum(map(len, missing)),
                  "missing_utf8_bytes": sum(len(text.encode()) for text in missing),
                  "embedding_requests": math.ceil(len(missing) / cache.batch_size)}
    config = {"schema_version": 1, "mode": "paired_dense_chunking", "changed_factor": "chunk_size",
              "baseline": original, "candidate": candidate, "candidate_descriptor": descriptor,
              "dataset_fingerprint": original["dataset_fingerprint"], "split": "dev",
              "baseline_archive_sha256": descriptor["baseline_archive_sha256"],
              "source_manifest_sha256": archive["source_manifest_sha256"],
              "query_vectors_sha256": sha256(vector_path.read_bytes()).hexdigest(),
              "query_policy": "exact-original-vectors; no-query-embedding; no-chat",
              "timing_policy": "paired-interleaved; alternating-side-first; no-explicit-warmup; preload-separate"}
    config["configuration_fingerprint"] = "chunking-experiment-v1-" + fingerprint(config)
    return Inputs(config, documents, questions, labels, catalogs, vectors, specs, cache_plan, gold_mapping)


def plan_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path,
                    candidate_path: Path, cache_dir: Path) -> dict:
    data = prepare_experiment(dataset_dir, baseline_dir, baseline_private_dir, candidate_path, cache_dir)
    return {"status": "ok", "mode": "offline_plan", "configuration_fingerprint": data.config["configuration_fingerprint"],
            "documents": len(data.documents), "baseline_chunks": len(data.catalogs["baseline"]),
            "candidate_chunks": len(data.catalogs["candidate"]), "reused_query_vectors": len(data.vectors),
            "cache": data.cache_plan, "max_query_embedding_calls": 0, "max_chat_calls": 0,
            "qdrant_queries": 2 * len(data.questions), "smoke_pairs": 2, "model_calls": 0}


def _check_collection(store, spec, catalog, *, new: bool) -> None:
    _check((store.collection_name, store.dimensions, store.vector_name)
           == (spec.collection_name, spec.dimensions, spec.vector_name), "Qdrant适配与实验索引身份不一致")
    try:
        exists = store._client.collection_exists(spec.collection_name)
        _check(exists is (not new), "候选集合必须新建；原V0集合必须存在，禁止覆盖或自动重建")
        store.ensure_collection()
        if new:
            return
        count = store._client.count(spec.collection_name, exact=True).count
        points = store._client.retrieve(spec.collection_name, ids=list(catalog), with_payload=True, with_vectors=False)
        _check(count == len(catalog) and len(points) == len(catalog), "集合点数或原V0片段缺失")
        _check({str(point.id) for point in points} == set(catalog), "集合片段ID与实验不一致")
        for point in points:
            chunk = catalog[str(point.id)].chunk
            _check(point.payload == {**chunk.model_dump(mode="json"), "dense_index_version": spec.index_version},
                   "集合原文、来源或索引版本已变化")
    except EvaluationError:
        raise
    except Exception:
        raise EvaluationError("Qdrant集合预检失败；未发Embedding请求，不自动重试") from None


class _LimitedEmbeddings:
    def __init__(self, delegate, limit: int) -> None:
        self.delegate, self.remaining = delegate, limit
        self.model, self.expected_dimensions, self.batch_size = delegate.model, delegate.expected_dimensions, delegate.batch_size

    def embed_documents(self, texts):
        _check(len(texts) <= self.remaining, "新增Embedding文本超过本次授权上限")
        self.remaining -= len(texts)
        return self.delegate.embed_documents(texts)


def collect_experiment(dataset_dir: Path, baseline_dir: Path, baseline_private_dir: Path,
                       candidate_path: Path, cache_dir: Path, output_dir: Path, settings: Settings,
                       *, embeddings, baseline_store, candidate_store, new_text_limit: int = 64) -> dict:
    started = time.monotonic()
    data = prepare_experiment(dataset_dir, baseline_dir, baseline_private_dir, candidate_path, cache_dir)
    preparation_ms = (time.monotonic() - started) * 1000
    output, dataset = Path(output_dir), Path(dataset_dir)
    assert_runtime_matches(data.config["baseline"], settings)
    _check(not output.exists() and not output.is_symlink()
           and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", output.name)), "结果目录已存在或run_id无效，拒绝覆盖")
    for protected in (dataset, Path(baseline_dir), Path(baseline_private_dir), Path(cache_dir)):
        _check(not output.resolve().is_relative_to(protected.resolve())
               and not protected.resolve().is_relative_to(output.resolve()), "结果目录须与冻结输入、基线及缓存分开")
    _check(type(new_text_limit) is int and 0 <= data.cache_plan["missing_texts"] <= new_text_limit <= 64,
           "新增Embedding数量超过本次授权上限")
    _check((embeddings.model, embeddings.expected_dimensions, embeddings.batch_size)
           == (data.config["baseline"]["embedding"]["model"], data.config["baseline"]["embedding"]["dimensions"],
               data.config["baseline"]["embedding"]["batch_size"]), "Embedding客户端与冻结合同不一致")
    _check_collection(baseline_store, data.specs["baseline"], data.catalogs["baseline"], new=False)
    _check_collection(candidate_store, data.specs["candidate"], data.catalogs["candidate"], new=True)
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "experiment_config.json", data.config)
    for side in ("baseline", "candidate"):
        _write(output / f"{side}_evaluation_config.json", data.config[side]["evaluation_config"])
    _write(output / "status.json", {"state": "running", "complete": False})
    cached = CachedEmbeddings(_LimitedEmbeddings(embeddings, new_text_limit), cache_dir,
                              base_url=data.config["baseline"]["embedding"]["provider_url"])
    manifest = ManifestStore(output / "index_manifests", data.specs["candidate"])
    spec = data.specs["candidate"]
    ingestion = IngestionService(cached, candidate_store, manifest,
                                 split_config=SplitConfig.model_validate(data.config["candidate"]["evaluation_config"]["splitter"]))
    order, errors = [], {"baseline": 0, "candidate": 0}

    def retrieve(side, question):
        store, catalog = (baseline_store if side == "baseline" else candidate_store), data.catalogs[side]
        timings, error, hits = dict.fromkeys(PHASES), None, []
        begin = time.monotonic()
        try:
            search_start = time.monotonic()
            try:
                results = store.query(data.vectors[question["question_id"]],
                                      document_ids=[d.document_id for d in data.documents],
                                      dense_index_version=data.specs[side].index_version,
                                      limit=data.config[side]["retrieval"]["top_k"])
            finally:
                timings["search"] = (time.monotonic() - search_start) * 1000
            evaluate_ranking(results, data.labels[question["question_id"]], catalog)
            hits = [hit.model_dump(mode="json") for hit in results]
        except (RuntimeError, ValueError):
            error = {"type": "retrieval_failed", "message": "检索或原文身份校验失败；无自动重试"}
            errors[side] += 1
        timings["total"] = (time.monotonic() - begin) * 1000
        _line(output / f"{side}_rankings.jsonl", {"question_id": question["question_id"],
              "query": question["standalone_question"] or question["question"],
              "hits": hits, "error": error, "timings_ms": timings})
        return error is None

    try:
        begin = time.monotonic()
        try:
            cached.embed_documents([item.chunk.text for item in data.catalogs["candidate"].values()])
        finally:
            _write(output / "embedding_batches.json", {"initial_cache": data.cache_plan,
                   "after_prefill": cached.stats(), "preparation_ms": preparation_ms,
                   "document_vectors_ms": (time.monotonic() - begin) * 1000})
        for source in _rows(dataset / "sources.jsonl"):
            ingestion.ingest_file(dataset / source["path"])
        _write(output / "index_manifest.json", manifest.load().model_dump(mode="json"))
        _check_collection(candidate_store, spec, data.catalogs["candidate"], new=False)
        _write(output / "gold_mapping.json", data.gold_mapping)
        for i, question in enumerate(data.questions):
            sides = ("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")
            success = [retrieve(side, question) for side in sides]
            order.append({"question_id": question["question_id"], "order": list(sides)})
            if i < 2 and not all(success):
                raise EvaluationError("前两题smoke失败，停止剩余查询；已付费结果保留，不自动重试")
        summaries = {}
        for side in ("baseline", "candidate"):
            summaries[side] = run_evaluation(dataset, output / f"{side}_evaluation_config.json",
                                             output / f"{side}_rankings.jsonl", output / f"{side}_retrieval")
        categories = {q["question_id"]: q["category"] for q in data.questions}
        comparison = compare_retrieval(output / "baseline_retrieval", output / "candidate_retrieval", categories)
        _write(output / "comparison.json", comparison)
        _write(output / "historical_v0_recheck.json", compare_retrieval(Path(baseline_dir) / "retrieval",
                                                                        output / "baseline_retrieval", categories))
        old_records = {r["question_id"]: r for r in _rows(output / "baseline_retrieval/retrieval.jsonl")}
        new_records = {r["question_id"]: r for r in _rows(output / "candidate_retrieval/retrieval.jsonl")}
        for qid in categories:
            _line(output / "case_details.jsonl", {"question_id": qid, "category": categories[qid],
                  "gold": data.labels[qid]["evidence"], "baseline": old_records[qid], "candidate": new_records[qid]})
        status = {"state": "recorded", "complete": True, "retrieval_errors": errors,
                  "model_calls": {"embedding": cached.stats()["api_calls"], "query_embedding": 0, "chat": 0},
                  "reused_query_vectors": len(data.vectors), "manual_review": "pending", "day12_accepted": False}
        _write(output / "status.json", status)
        historical_sources = _json(_read(Path(baseline_private_dir) / "manifest.json"))["source_sha256"]
        sources = [*historical_sources, "evals/chunking_run.py", "evals/comparison.py", "evals/chunking.py",
                   "evals/freeze_dataset.py", "evals/validate_dataset.py"]
        receipt = {"schema_version": 1, "run_id": output.name, "mode": "paired_dense_chunking",
                   "recorded_at": datetime.now(timezone.utc).isoformat(), "code": _code_state(),
                   "configuration_fingerprint": data.config["configuration_fingerprint"],
                   "dataset_fingerprint": data.config["dataset_fingerprint"],
                   "source_sha256": {name: sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() for name in sources},
                   "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                               "dependencies": {name: version(name) for name in ("qdrant-client", "openai", "langchain-text-splitters")}},
                   "qdrant_url": str(settings.qdrant_url), "collections": {side: s.model_dump(mode="json") for side, s in data.specs.items()},
                   "query_vectors_sha256": data.config["query_vectors_sha256"], "query_order": order,
                   "timing_policy": data.config["timing_policy"], "cache_stats": cached.stats(),
                   "model_calls": status["model_calls"], "embedding_usage": None, "actual_billed_cost": None,
                   "files": {str(p.relative_to(output)): sha256(p.read_bytes()).hexdigest()
                             for p in sorted(output.rglob("*")) if p.is_file()}}
        _write(output / "manifest.json", receipt)
        return {"status": "ok", "run_id": output.name, **status, "delta_at_k": comparison["delta_at_k"]}
    except Exception:
        _write(output / "status.json", {"state": "failed", "complete": False,
               "model_calls": {"embedding": cached.stats()["api_calls"], "query_embedding": 0, "chat": 0},
               "message": "实验未完整完成，保留局部结果与缓存；禁止自动重跑收费请求"})
        raise


def verify_experiment(dataset_dir: Path, output_dir: Path) -> dict:
    output = Path(output_dir)
    manifest, config = _json(_read(output / "manifest.json")), _json(_read(output / "experiment_config.json"))
    frozen = verify_freeze(dataset_dir)
    validate_baseline_config(config["baseline"], dataset_dir)
    content = {k: v for k, v in config.items() if k != "configuration_fingerprint"}
    _check(config["configuration_fingerprint"] == "chunking-experiment-v1-" + fingerprint(content)
           == manifest["configuration_fingerprint"] and frozen["fingerprint"] == manifest["dataset_fingerprint"], "实验配置或数据指纹不匹配")
    status = _json(_read(output / "status.json"))
    _check(status.get("complete") is True and status.get("state") == "recorded", "实验未完整完成")
    actual = {str(p.relative_to(output)) for p in output.rglob("*") if p.is_file() and p.name != "manifest.json"}
    required = {"experiment_config.json", "index_manifest.json", "gold_mapping.json", "comparison.json", "case_details.jsonl",
                "baseline_rankings.jsonl", "candidate_rankings.jsonl", "historical_v0_recheck.json", "embedding_batches.json", "status.json"}
    required |= {"baseline_evaluation_config.json", "candidate_evaluation_config.json",
                 "index_manifests/" + config["candidate"]["index"]["collection_name"] + ".json"}
    required |= {side + "_retrieval/" + name for side in ("baseline", "candidate")
                 for name in ("config.json", "summary.json", "retrieval.jsonl")}
    _check(required == set(manifest["files"]) == actual, "实验清单遗漏文件或出现未封存产物")
    for name, digest in manifest["files"].items():
        path = output / name
        _check(path.resolve().is_relative_to(output.resolve()) and sha256(path.read_bytes()).hexdigest() == digest,
               "实验文件已变化或缺失")
    return {"status": "ok", "state": "verified", "run_id": manifest["run_id"],
            "files": len(actual), "manual_review": status["manual_review"], "day12_accepted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "collect", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--dataset", type=Path, required=True)
        if name != "verify":
            for arg in ("baseline-dir", "baseline-private-dir", "candidate", "cache-dir"):
                sub.add_argument("--" + arg, type=Path, required=True)
        if name != "plan":
            sub.add_argument("--output-dir", type=Path, required=True)
        if name == "collect":
            sub.add_argument("--live", action="store_true")
            sub.add_argument("--max-new-texts", type=int, default=0, help="本次授权新增Embedding文本上限，默认0")
    args = parser.parse_args()
    if args.command == "collect" and not args.live:
        parser.error("真实切分实验需显式--live；新增Embedding费用须先获授权，无自动重试")
    try:
        if args.command == "verify":
            result = verify_experiment(args.dataset, args.output_dir)
        else:
            paths = (args.dataset, args.baseline_dir, args.baseline_private_dir, args.candidate, args.cache_dir)
            if args.command == "plan":
                result = plan_experiment(*paths)
            else:
                settings = load_settings()
                with ExitStack() as stack:
                    embeddings = stack.enter_context(create_embedding_client(settings))
                    baseline_store = stack.enter_context(create_qdrant_store(settings.model_copy(update={"qdrant_collection_name": "pc_eval_dense_v0"})))
                    candidate_store = stack.enter_context(create_qdrant_store(settings.model_copy(update={"qdrant_collection_name": CANDIDATE_COLLECTION})))
                    result = collect_experiment(*paths, args.output_dir, settings, embeddings=embeddings,
                                                baseline_store=baseline_store, candidate_store=candidate_store,
                                                new_text_limit=args.max_new_texts)
    except (RuntimeError, ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "error", "message": "切分实验校验或执行失败；检查输入及局部status，禁止自动重试"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
