"""Metadata, scope and failure contracts; these tests never open sockets."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from project_copilot.config import Settings
from project_copilot.infrastructure.call_accounting import account_job
from project_copilot.infrastructure import ranking_cache as caching
from project_copilot.infrastructure.ranking_cache import CacheLease, RankingCache, RankingRecord
from project_copilot.infrastructure.redis_runtime import RedisUnavailable
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest


class Runtime:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def eval(self, *args):
        self.calls.append(args)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def settings(**overrides):
    return Settings(_env_file=None, redis_enabled=True, redis_cache_enabled=True,
                    redis_url=SecretStr("redis://127.0.0.1:16380/15"),
                    redis_cache_hmac_key=SecretStr("cache-private-canary-" * 3), **overrides)


def manifest():
    return IndexManifest(spec=DenseIndexSpec(collection_name="pc_cache_test",
        embedding_model="embedding-3", dimensions=2048, splitter_version="split-v1"))


def cache_case(runtime=None, **overrides):
    adapter = RankingCache(settings(**overrides), runtime=runtime or Runtime())
    identity = adapter.identity("private-query-canary", manifest(), pipeline={"revision": "first"})
    return adapter, identity


def test_disabled_cache_does_not_construct_or_inspect_anything(monkeypatch):
    monkeypatch.setattr(caching, "get_redis_runtime", lambda _: pytest.fail("disabled cache constructed Redis"))
    assert caching.create_ranking_cache(SimpleNamespace()) is None
    monkeypatch.setattr(caching, "get_redis_runtime", lambda _: (_ for _ in ()).throw(RedisUnavailable()))
    assert caching.create_ranking_cache(SimpleNamespace(redis_cache_enabled=True)) is None


@pytest.mark.parametrize("profile", [{"public_demo": True}, {"environment": "production"}])
def test_public_cache_requires_trusted_account_before_touching_redis(monkeypatch, profile):
    active = settings().model_copy(update=profile)
    monkeypatch.setattr(caching, "get_redis_runtime", lambda _: pytest.fail("missing scope touched Redis"))
    assert caching.create_ranking_cache(active) is None
    adapter = RankingCache(active, runtime=Runtime())
    with pytest.raises(RedisUnavailable):
        adapter.identity("synthetic", manifest(), pipeline={})
    with account_job(None, uuid4(), uuid4()):
        assert adapter.identity("synthetic", manifest(), pipeline={}).data_key


@pytest.mark.parametrize("overrides", [
    {"redis_cache_hmac_key": "short"}, {"redis_cache_hmac_key": None},
    {"redis_enabled": False}, {"redis_cache_ttl_jitter_ratio": 0.6},
    {"redis_cache_ttl_jitter_ratio": float("nan")}, {"redis_cache_lock_ttl_seconds": 0},
    {"redis_cache_ttl_seconds": 1, "redis_cache_negative_ttl_seconds": 2},
])
def test_invalid_cache_settings_are_rejected(overrides):
    values = dict(redis_enabled=True, redis_cache_enabled=True,
                  redis_url="redis://127.0.0.1:16380/15", redis_cache_hmac_key="x" * 32)
    values.update(overrides)
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, **values)
    assert "x" * 32 not in str(error.value)


def test_keys_bind_full_manifest_scope_resource_query_and_pipeline(tmp_path):
    active = settings(manifest_dir=tmp_path)
    adapter = RankingCache(active, runtime=Runtime())
    current = manifest()
    def identify(query="private-query-canary", selected=current, pipeline=None):
        return adapter.identity(query, selected, pipeline=pipeline or {"revision": "first"})
    base = identify()
    document = CompletedDocument(document_id=uuid4(), content_hash="a" * 64,
        source_name="private-filename-canary.txt", chunk_ids=(uuid4(),))
    updated = current.model_copy(update={"documents": (document,)})
    assert current.spec.index_version == updated.spec.index_version
    keys = [base, identify(" private-query-canary"), identify(selected=updated),
            identify(pipeline={"revision": "second"})]
    with account_job(None, uuid4(), uuid4()):
        keys.append(identify())
    with account_job(None, uuid4(), uuid4()):
        keys.append(identify())
    for name, value in [("public_resource_id", "another-resource"), ("qdrant_url", "http://127.0.0.1:9999"),
                        ("manifest_dir", tmp_path / "other"), ("environment", "test")]:
        previous = getattr(active, name)
        setattr(active, name, value)
        keys.append(identify())
        setattr(active, name, previous)
    assert len({key.data_key for key in keys}) == len(keys)
    for identity in keys:
        rendered = str(identity)
        assert "private-" not in rendered and str(tmp_path) not in rendered
        assert all(key.split("{")[1].split("}")[0] == base.data_key.split("{")[1].split("}")[0]
                   for key in (identity.data_key, identity.owner_key, identity.fence_key, identity.sequence_key))


def test_payload_contains_only_ranking_metadata_and_current_ids():
    runtime = Runtime(1)
    adapter, identity = cache_case(runtime)
    identifier = uuid4()
    lease = CacheLease("owner-canary", 7)
    assert adapter.publish(identity, lease, [RankingRecord(identifier, -3.5)])
    script, keys, arguments = runtime.calls[-1]
    assert script == caching.PUBLISH_LUA
    payload = json.loads(arguments[2])
    assert payload == {"format_version": 1, "hits": [{"chunk_id": str(identifier), "score": -3.5}]}
    assert "canary" not in arguments[2]
    runtime.result = arguments[2].encode()
    assert adapter.get(identity, {identifier}) == [RankingRecord(identifier, -3.5)]
    assert adapter.last_status == "hit"
    assert adapter.get(identity, set()) is None
    assert adapter.last_status == "invalid"


@pytest.mark.parametrize("payload", [b"not json", b"x" * 4097, b"[" * 1100 + b"]" * 1100, {"unexpected": True},
    {"format_version": True, "hits": []}, {"format_version": 1, "hits": [], "text": "private"},
    {"format_version": 1, "hits": [{"chunk_id": "invalid", "score": 1.0}]},
    {"format_version": 1, "hits": [{"chunk_id": str(uuid4()), "score": True}]},
    {"format_version": 1, "hits": [{"chunk_id": str(uuid4()), "score": float("nan")}]},
    {"format_version": 1, "hits": [{"chunk_id": str(uuid4()), "score": 1.0, "text": "private"}]},
])
def test_corrupt_payload_is_a_cache_miss(payload):
    raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    adapter, identity = cache_case(Runtime(raw))
    assert adapter.get(identity, set(), allow_negative=True) is None


def test_duplicate_and_oversized_rankings_are_not_published():
    runtime = Runtime(1)
    adapter, identity = cache_case(runtime)
    item = RankingRecord(uuid4(), 1.0)
    for records in [[item, item], [RankingRecord(uuid4(), 1.0) for _ in range(6)],
                    [RankingRecord(uuid4(), float("inf"))], []]:
        assert not adapter.publish(identity, CacheLease("owner", 1), records)
    assert runtime.calls == []


def test_negative_cache_is_explicit_and_shorter(monkeypatch):
    runtime = Runtime(1)
    adapter, identity = cache_case(runtime)
    monkeypatch.setattr(caching.random, "uniform", lambda low, high: low)
    assert adapter.publish(identity, CacheLease("owner", 1), [], negative=True)
    assert runtime.calls[-1][2][-1] == 9000
    runtime.result = runtime.calls[-1][2][2].encode()
    assert adapter.get(identity, set(), allow_negative=True) == []
    assert adapter.last_status == "negative_hit"
    assert adapter.get(identity, set()) is None
    monkeypatch.setattr(caching.random, "uniform", lambda low, high: high)
    assert adapter._ttl_ms(False) == 330000


def test_owner_fence_and_bounded_lease_are_sent_atomically():
    runtime = Runtime(13)
    adapter, identity = cache_case(runtime, redis_cache_lock_ttl_seconds=0.15)
    lease = adapter.acquire(identity)
    assert lease.fence == 13 and len(lease.owner) == 32
    assert runtime.calls[-1][0] == caching.ACQUIRE_LUA
    assert runtime.calls[-1][1] == [identity.owner_key, identity.fence_key, identity.sequence_key, identity.data_key]
    assert runtime.calls[-1][2] == [lease.owner, 150]
    runtime.result = 1
    assert adapter.release(identity, lease)
    assert runtime.calls[-1][0] == caching.RELEASE_LUA
    assert runtime.calls[-1][2] == [lease.owner, "13"]
    runtime.result = 0
    assert adapter.acquire(identity) is None and adapter.last_status == "busy"
    assert not adapter.publish(identity, lease, [], negative=True)
    assert adapter.last_status == "lease_lost"


def test_cache_failures_are_safe_and_not_retried():
    runtime = Runtime(RedisUnavailable())
    adapter, identity = cache_case(runtime)
    assert adapter.get(identity, set()) is None
    assert adapter.acquire(identity) is None
    assert not adapter.publish(identity, CacheLease("owner", 1), [], negative=True)
    assert not adapter.release(identity, CacheLease("owner", 1))
    assert len(runtime.calls) == 4 and adapter.last_status == "unavailable"


def test_lock_metrics_record_nonblocking_elapsed_and_safe_status(monkeypatch):
    spans = []
    @contextmanager
    def span(name, attributes):
        captured = dict(attributes)
        spans.append((name, captured))
        yield SimpleNamespace(set_attribute=lambda key, value: captured.update({key: value}))
    monkeypatch.setattr(caching, "trace_span", span)
    adapter, key = cache_case(Runtime(0))
    assert adapter.acquire(key) is None
    assert spans[-1][0] == "rag.cache.lock"
    assert spans[-1][1]["cache.status"] == "busy"
    assert spans[-1][1]["lock_wait_ms"] == adapter.last_lock_wait_ms >= 0
    assert "canary" not in str(spans)
