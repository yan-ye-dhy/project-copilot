"""Opt-in real Redis scripts plus controlled, non-model retrieval integration."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import time
from uuid import uuid4

from pydantic import SecretStr
import pytest
from qdrant_client import models

from project_copilot.infrastructure import ranking_cache as caching
from project_copilot.infrastructure.call_accounting import account_job
from project_copilot.infrastructure.ranking_cache import RankingCache, RankingRecord
from project_copilot.rag.bm25 import BM25Error
from project_copilot.rag.deletion import DocumentDeletion
from project_copilot.rag.final_retrieval import FinalRetrievalError
from project_copilot.rag.ingestion import ManifestError
from project_copilot.rag.index_lock import index_lock
from redis_support import isolated_redis
from test_final_retrieval import system, ingest
from test_ranking_cache import manifest


def options(**overrides):
    return dict(redis_cache_enabled=True, redis_cache_hmac_key=SecretStr("synthetic-cache-hmac-key-" * 2),
                redis_cache_ttl_seconds=300, redis_cache_negative_ttl_seconds=10,
                redis_cache_ttl_jitter_ratio=0.1, redis_cache_lock_ttl_seconds=120, **overrides)


def identity(adapter):
    return adapter.identity("synthetic-lock-experiment", manifest(), pipeline={"revision": "test"})


def test_real_hot_key_has_one_owner_and_monotonic_fence():
    with isolated_redis(**options()) as (active, runtime):
        adapter = RankingCache(active, runtime)
        key = identity(adapter)
        barrier = Barrier(8)
        def attempt(_):
            barrier.wait(timeout=5)
            return RankingCache(active, runtime).acquire(key)
        with ThreadPoolExecutor(max_workers=8) as pool:
            leases = list(pool.map(attempt, range(8)))
        owners = [lease for lease in leases if lease is not None]
        assert len(owners) == 1
        first = owners[0]
        assert adapter.release(key, first)
        second = adapter.acquire(key)
        assert second.fence > first.fence and second.owner != first.owner
        assert adapter.release(key, second)


def test_real_delayed_miss_cannot_acquire_after_another_owner_publishes():
    with isolated_redis(**options()) as (active, runtime):
        first, delayed = RankingCache(active, runtime), RankingCache(active, runtime)
        key = identity(first)
        record = RankingRecord(uuid4(), 3.0)
        assert first.get(key, {record.chunk_id}) is None
        assert delayed.get(key, {record.chunk_id}) is None
        lease = first.acquire(key)
        assert first.publish(key, lease, [record])
        # The delayed request still holds its earlier miss observation. The
        # existence check and lease acquisition must be one atomic operation.
        assert delayed.acquire(key) is None
        assert delayed.get(key, {record.chunk_id}) == [record]
        assert int(runtime.client.get(key.sequence_key)) == lease.fence
        assert not runtime.client.exists(key.owner_key, key.fence_key)


def test_real_invalid_existing_data_cannot_be_overwritten_by_a_new_lease():
    with isolated_redis(**options()) as (active, runtime):
        adapter = RankingCache(active, runtime)
        key = identity(adapter)
        runtime.client.set(key.data_key, b"controlled-invalid-data", px=1000)
        assert adapter.get(key, set(), allow_negative=True) is None
        assert adapter.last_status == "invalid"
        assert adapter.acquire(key) is None
        assert runtime.client.get(key.data_key) == b"controlled-invalid-data"
        assert runtime.client.pttl(key.data_key) > 0
        assert not runtime.client.exists(key.owner_key, key.fence_key, key.sequence_key)


def test_real_expired_owner_cannot_publish_or_unlock_new_owner():
    values = options()
    values["redis_cache_lock_ttl_seconds"] = 0.08
    with isolated_redis(**values) as (active, runtime):
        adapter = RankingCache(active, runtime)
        key = identity(adapter)
        older = adapter.acquire(key)
        deadline = time.monotonic() + 2
        while runtime.client.exists(key.owner_key) and time.monotonic() < deadline:
            time.sleep(0.01)
        newer = adapter.acquire(key)
        assert newer is not None and newer.fence > older.fence
        record = RankingRecord(uuid4(), 5.0)
        assert not adapter.publish(key, older, [RankingRecord(record.chunk_id, -7.0)])
        assert not adapter.release(key, older)
        assert runtime.client.get(key.owner_key) == newer.owner.encode()
        assert not adapter.publish(key, replace(newer, fence=newer.fence + 1), [record])
        assert not adapter.release(key, replace(newer, fence=newer.fence + 1))
        assert adapter.publish(key, newer, [record])
        assert not adapter.publish(key, older, [RankingRecord(record.chunk_id, -7.0)])
        assert adapter.get(key, {record.chunk_id}) == [record]


def test_real_jitter_short_negative_expiry_and_new_fill():
    with isolated_redis(**options()) as (active, runtime):
        adapter = RankingCache(active, runtime)
        key = identity(adapter)
        lease = adapter.acquire(key)
        assert adapter.publish(key, lease, [], negative=True)
        assert 0 < runtime.client.pttl(key.data_key) <= 11000
        assert adapter.get(key, set(), allow_negative=True) == []
        runtime.client.pexpire(key.data_key, 1)
        deadline = time.monotonic() + 2
        while runtime.client.exists(key.data_key) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert adapter.get(key, set(), allow_negative=True) is None
        record = RankingRecord(uuid4(), -1.5)
        assert adapter.publish(key, adapter.acquire(key), [record])
        assert 269000 < runtime.client.pttl(key.data_key) <= 330000
        assert adapter.get(key, {record.chunk_id}) == [record]


@pytest.fixture
def cached_system(system, monkeypatch):
    with isolated_redis(**options()) as (active, runtime):
        for name, value in vars(active).items():
            setattr(system[0], name, value)
        # Keep the normal business factory and Settings. Only reuse the pool
        # owned by this test's random namespace so teardown closes every socket.
        monkeypatch.setattr(caching, "get_redis_runtime", lambda _: runtime)
        yield system, runtime


def test_real_business_hit_preserves_verification_and_skips_embedding_and_inference(cached_system, tmp_path, monkeypatch):
    system, runtime = cached_system
    document = ingest(system, tmp_path, "synthetic.txt", "合成知识条目 CACHE-391。")
    retriever = system[3]
    first = retriever.search("CACHE-391")
    assert len(first) == 1
    monkeypatch.setattr(system[2], "embed_query", lambda _: pytest.fail("cache hit spent embedding"))
    monkeypatch.setattr(retriever.reranker, "rerank", lambda *_: pytest.fail("cache hit ran inference"))
    assert retriever.search("CACHE-391") == first
    assert retriever.reranker.prepares == 2
    # A hit never bypasses current Qdrant verification.
    runtime_keys = list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*data:*"))
    assert len(runtime_keys) == 1
    raw = runtime.client.get(runtime_keys[0])
    assert b"CACHE-391" not in raw and b"synthetic.txt" not in raw
    system[1]._client.delete(collection_name=system[0].qdrant_collection_name,
                            points_selector=models.PointIdsList(points=[str(document.chunk_ids[0])]), wait=True)
    with pytest.raises(BM25Error):
        retriever.search("CACHE-391")


def test_real_hit_still_requires_reranker_prepare(cached_system, tmp_path, monkeypatch):
    system, _ = cached_system
    ingest(system, tmp_path, "synthetic.txt", "合成知识条目 MODEL-302。")
    retriever = system[3]
    assert retriever.search("MODEL-302")
    def unavailable():
        raise FinalRetrievalError("controlled model unavailable")
    monkeypatch.setattr(retriever.reranker, "prepare", unavailable)
    with pytest.raises(FinalRetrievalError):
        retriever.search("MODEL-302")


def test_real_ingestion_changes_generation_and_account_scope(cached_system, tmp_path):
    system, runtime = cached_system
    retriever = system[3]
    ingest(system, tmp_path, "first.txt", "第一条合成知识。")
    assert len(retriever.search("知识")) == 1
    ingest(system, tmp_path, "second.txt", "第二条合成知识。")
    assert len(retriever.search("知识")) == 2
    with account_job(None, uuid4(), uuid4()):
        assert len(retriever.search("知识")) == 2
    with account_job(None, uuid4(), uuid4()):
        assert len(retriever.search("知识")) == 2
    assert len(retriever.reranker.seen) == 4
    assert len(list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*data:*"))) == 4


def test_real_deletion_switches_cache_generation(cached_system, tmp_path):
    system, runtime = cached_system
    first = ingest(system, tmp_path, "first.txt", "准备删除的合成知识。")
    second = ingest(system, tmp_path, "second.txt", "保留的合成知识。")
    retriever = system[3]
    assert len(retriever.search("知识")) == 2
    DocumentDeletion(retriever.manifest, system[1]._client).delete(first.document_id)
    hits = retriever.search("知识")
    assert {item.metadata.document_id for item in hits} == {second.document_id}
    assert len(retriever.reranker.seen) == 2
    assert len(list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*data:*"))) == 2


def test_real_empty_manifest_negative_cache_cannot_hide_new_documents_or_delete_journal(cached_system, tmp_path):
    system, runtime = cached_system
    retriever = system[3]
    assert retriever.search("知识") == []
    assert retriever.search("知识") == []
    assert retriever.reranker.prepares == 0
    keys = list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*data:*"))
    assert len(keys) == 1 and runtime.client.pttl(keys[0]) <= 11000
    ingest(system, tmp_path, "new.txt", "新增合成知识。")
    assert retriever.search("知识")
    retriever.manifest.deletion_journal_path.write_text("controlled unfinished deletion")
    with pytest.raises(ManifestError):
        retriever.search("知识")


def test_real_busy_lease_uses_original_business_without_waiting_or_publishing(cached_system, tmp_path):
    system, runtime = cached_system
    retriever = system[3]
    ingest(system, tmp_path, "one.txt", "热点重建合成知识。")
    with index_lock(retriever.manifest.path):
        index = retriever.snapshots.get_locked(system[1]._client)
        adapter, key = retriever._cache_context("热点", retriever.manifest.load(), index=index)
    lease = adapter.acquire(key)
    assert lease is not None
    assert retriever.search("热点")
    assert not runtime.client.exists(key.data_key)
    assert runtime.client.get(key.owner_key) == lease.owner.encode()
    assert adapter.release(key, lease)
    assert retriever.search("热点")
    assert runtime.client.exists(key.data_key)


def test_real_generation_recheck_blocks_late_cache_publication(cached_system, tmp_path, monkeypatch):
    system, runtime = cached_system
    retriever = system[3]
    ingest(system, tmp_path, "one.txt", "版本核对合成知识。")
    original = retriever.reranker.rerank
    def replace_manifest(query, candidates):
        ranked = original(query, candidates)
        retriever.manifest.path.unlink()  # Controlled unsupported external writer.
        return ranked
    monkeypatch.setattr(retriever.reranker, "rerank", replace_manifest)
    with pytest.raises(BM25Error, match="完成清单发生变化"):
        retriever.search("知识")
    assert list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*data:*")) == []
    assert list(runtime.client.scan_iter(match=system[0].redis_namespace + ":*owner:*")) == []
