"""Benchmark correctness with synthetic vectors and an in-memory Qdrant."""
import pytest
from qdrant_client import QdrantClient

from scripts.retrieval_benchmark import benchmark, isolated_origin


@pytest.mark.parametrize("origin", ["https://example.com", "http://127.0.0.1:6433", "http://qdrant:6333/private",
                                    "http://user:pass@qdrant:6333", "http://qdrant:6333?url=http://example.com"])
def test_benchmark_rejects_non_isolated_target(origin):
    with pytest.raises(ValueError):
        isolated_origin(origin)


def test_benchmark_counts_full_verification_and_preserves_other_collections(tmp_path):
    client = QdrantClient(":memory:")
    try:
        result = benchmark(client, tmp_path, 2, repeats=2, concurrency=2)
        assert result["actual_chunks"] >= 2
        for phase in result["phases"].values():
            assert phase["qdrant_retrieve_batches"] == 2 * phase["expected_batches_per_request"]
            assert phase["latency_ms"]["samples"] == phase["lock_hold_ms"]["samples"] == 2
            assert phase["completed_verification_ms"]["samples"] == 2
            assert phase["python_tracemalloc_peak_bytes"] > 0
        assert not client.get_collections().collections
    finally:
        client.close()
