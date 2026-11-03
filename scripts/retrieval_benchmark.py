"""Synthetic Hybrid/Rerank pipeline benchmark; never contacts model providers.

Only UUID-scoped collections in an explicitly supplied isolated Qdrant are
created. The reported latency excludes real Embedding and BGE inference.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import math
from pathlib import Path
import statistics
import tempfile
import threading
import time
import tracemalloc
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from qdrant_client import QdrantClient

from project_copilot.config import Settings
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag import final_retrieval as final
from project_copilot.rag.ingestion import IngestionService
from project_copilot.rag.rerank import RerankedHit


class SyntheticVectors:
    model = "embedding-3"
    expected_dimensions = 2048

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * 2047 for _ in texts]

    def embed_query(self, query):
        return [1.0] + [0.0] * 2047


class SyntheticReranker:
    def prepare(self):
        pass

    def rerank(self, query, candidates):
        return [RerankedHit(hit=hit, candidate_rank=i, rerank_score=float(100 - i))
                for i, hit in enumerate(candidates[:5], 1)]

    def close(self):
        pass


def isolated_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"qdrant", "127.0.0.1", "localhost"}
            or parsed.port not in {6333, 16433} or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("only an explicitly isolated local Qdrant is supported")
    return value


def distribution(values):
    ordered = sorted(values)
    return {"samples": len(values), "median": statistics.median(ordered),
            "p95": ordered[max(0, math.ceil(len(ordered) * .95) - 1)], "max": max(ordered)}


def benchmark(client, root, size, *, repeats=4, concurrency=4):
    name = "pc_stage15_perf_" + uuid4().hex
    directory = root / name
    directory.mkdir()
    settings = Settings(_env_file=None, environment="test", manifest_dir=directory / "manifests",
                        qdrant_collection_name=name, zhipu_api_key=None, deepseek_api_key=None)
    retriever = final.FinalRetriever(settings, reranker=SyntheticReranker())
    store = DenseQdrantStore(client, collection_name=name, dimensions=2048)
    vectors = SyntheticVectors()
    source = directory / "synthetic.txt"
    source.write_text("\n\n".join(f"性能条目 {i:05d} " + "研发故障说明" * 110 for i in range(size)), encoding="utf-8")

    @contextmanager
    def embedding_factory(_):
        yield vectors

    @contextmanager
    def store_factory(_):
        yield store

    original_lock, original_retrieve = final.index_lock, client.retrieve
    original_read = final.read_completed_chunks
    records = {"wait_ms": [], "hold_ms": [], "verification_ms": [], "read_batches": 0}
    guard = threading.Lock()

    @contextmanager
    def timed_lock(path, **kwargs):
        started = time.perf_counter()
        with original_lock(path, **kwargs):
            acquired = time.perf_counter()
            try:
                yield
            finally:
                ended = time.perf_counter()
                with guard:
                    records["wait_ms"].append((acquired - started) * 1000)
                    records["hold_ms"].append((ended - acquired) * 1000)

    def counted(*args, **kwargs):
        with guard:
            records["read_batches"] += 1
        return original_retrieve(*args, **kwargs)

    def timed_verification(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original_read(*args, **kwargs)
        finally:
            with guard:
                records["verification_ms"].append((time.perf_counter() - started) * 1000)

    def search(_):
        started = time.perf_counter()
        hits = retriever.search("研发故障 性能条目")
        assert hits and all(hit.metadata.document_id == document.document_id for hit in hits)
        return (time.perf_counter() - started) * 1000

    try:
        document = IngestionService(vectors, store, retriever.manifest).ingest_file(source)
        with patch.object(final, "create_embedding_client", embedding_factory), patch.object(final, "create_qdrant_store", store_factory):
            search(0)  # Build/warm the snapshot; exclude it from steady-state samples.
            phases = {}
            with patch.object(final, "index_lock", timed_lock), patch.object(client, "retrieve", counted), patch.object(final, "read_completed_chunks", timed_verification):
                for workers in (1, concurrency):
                    records.update(wait_ms=[], hold_ms=[], verification_ms=[], read_batches=0)
                    tracemalloc.start()
                    started = time.perf_counter()
                    try:
                        with ThreadPoolExecutor(max_workers=workers) as executor:
                            latencies = list(executor.map(search, range(repeats)))
                        elapsed = time.perf_counter() - started
                        _, peak = tracemalloc.get_traced_memory()
                    finally:
                        tracemalloc.stop()
                    expected = math.ceil(len(document.chunk_ids) / 64) * repeats
                    if records["read_batches"] != expected:
                        raise RuntimeError("completed point verification was skipped or repeated unexpectedly")
                    phases[str(workers)] = {"requests": repeats, "workers": workers,
                        "latency_ms": distribution(latencies), "lock_wait_ms": distribution(records["wait_ms"]),
                        "lock_hold_ms": distribution(records["hold_ms"]), "qdrant_retrieve_batches": records["read_batches"],
                        "completed_verification_ms": distribution(records["verification_ms"]),
                        "expected_batches_per_request": math.ceil(len(document.chunk_ids) / 64),
                        "python_tracemalloc_peak_bytes": peak, "requests_per_second": repeats / elapsed}
        return {"requested_paragraphs": size, "actual_chunks": len(document.chunk_ids), "phases": phases}
    finally:
        retriever.close()
        # Only this UUID-scoped synthetic collection. No volumes are removed.
        if client.collection_exists(name):
            client.delete_collection(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-url", required=True, type=isolated_origin)
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sizes", type=int, nargs="+", default=[10, 100, 500])
    parser.add_argument("--repeats", type=int, default=4)
    args = parser.parse_args()
    if not args.confirm_isolated or args.output.exists() or any(not 1 <= s <= 2000 for s in args.sizes) or not 2 <= args.repeats <= 20:
        parser.error("requires explicit isolated target, a new output, bounded sizes and repeats")
    report = {"mode": "real-http-qdrant-synthetic-models", "model_provider_calls": 0,
              "limitations": ["Embedding and BGE are deterministic stubs; no real inference latency",
                              "tracemalloc measures Python allocations, excluding server/RSS/CUDA memory",
                              "snapshot warmup excluded; shared index lock serializes queries"], "sizes": []}
    with tempfile.TemporaryDirectory(prefix="pc-stage15-perf-") as temporary:
        client = QdrantClient(url=args.qdrant_url, timeout=60, check_compatibility=False, trust_env=False)
        try:
            for size in args.sizes:
                report["sizes"].append(benchmark(client, Path(temporary), size, repeats=args.repeats))
        finally:
            client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"status": "ok", "scales": len(report["sizes"]), "model_provider_calls": 0}))


if __name__ == "__main__":
    main()
