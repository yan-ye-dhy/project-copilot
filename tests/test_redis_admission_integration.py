"""Actual Lua/Redis races in isolated namespaces; explicit opt-in only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest

from project_copilot.infrastructure.redis_runtime import RedisUnavailable, close_redis_runtime, get_redis_runtime
from project_copilot.services.redis_admission import RateLimited, admit, bucket_keys
from redis_support import isolated_redis


def accepted(settings, user, barrier=None):
    if barrier is not None:
        barrier.wait(timeout=10)
    try:
        admit(settings, user)
        return True
    except RateLimited:
        return False


def test_real_global_bucket_is_atomic_across_accounts():
    with isolated_redis(redis_admission_global_capacity=3,
                        redis_admission_global_refill_per_second=0.000001) as (settings, runtime):
        barrier = Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: accepted(settings, uuid4(), barrier), range(8)))
        assert results.count(True) == 3
        keys = list(runtime.client.scan_iter(match=settings.redis_namespace + ":*"))
        assert len(keys) == 4  # One global and only the three admitted accounts.


def test_real_account_bucket_does_not_partially_consume_global():
    with isolated_redis(redis_admission_global_capacity=20, redis_admission_user_capacity=3,
                        redis_admission_global_refill_per_second=0.000001,
                        redis_admission_user_refill_per_second=0.000001) as (settings, runtime):
        user = uuid4()
        barrier = Barrier(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: accepted(settings, user, barrier), range(8)))
        assert results.count(True) == 3
        keys, _ = bucket_keys(settings, user)
        assert float(runtime.client.hget(keys[0], "tokens")) == pytest.approx(17, abs=0.0001)
        snapshot = [runtime.client.hgetall(key) for key in keys]
        assert accepted(settings, user) is False
        assert [runtime.client.hgetall(key) for key in keys] == snapshot


def test_real_refill_retry_after_and_millisecond_ttl():
    with isolated_redis(redis_admission_global_capacity=2, redis_admission_user_capacity=1,
                        redis_admission_user_refill_per_second=0.2) as (settings, runtime):
        user = uuid4()
        admit(settings, user)
        keys, _ = bucket_keys(settings, user)
        with pytest.raises(RateLimited) as error:
            admit(settings, user)
        assert error.value.retry_after == 5
        assert 0 < runtime.client.pttl(keys[0]) <= 3000
        assert 0 < runtime.client.pttl(keys[1]) <= 6000
        seconds, micros = runtime.client.time()
        now_ms = seconds * 1000 + micros // 1000
        runtime.client.hset(keys[1], mapping={"tokens": 0, "updated_ms": now_ms - 5001})
        admit(settings, user)
        assert float(runtime.client.hget(keys[1], "tokens")) < 0.01


def test_real_denial_does_not_create_account_and_cold_runtime_preserves_state():
    with isolated_redis(redis_admission_global_capacity=1,
                        redis_admission_global_refill_per_second=0.000001) as (settings, runtime):
        first, second = uuid4(), uuid4()
        admit(settings, first)
        second_keys, _ = bucket_keys(settings, second)
        assert accepted(settings, second) is False
        assert not runtime.client.exists(second_keys[1])
        close_redis_runtime(settings)
        replacement = get_redis_runtime(settings)
        assert replacement is not runtime
        assert accepted(settings, first) is False
        assert runtime._closed and not runtime.client.connection_pool._available_connections[0]._sock


def test_real_configuration_version_and_provider_scope_are_independent():
    with isolated_redis(redis_admission_global_capacity=1, redis_admission_user_capacity=1,
                        redis_admission_global_refill_per_second=0.000001,
                        redis_admission_user_refill_per_second=0.000001) as (settings, runtime):
        user = uuid4()
        admit(settings, user)
        assert accepted(settings, user) is False
        admit(settings, user, "provider")
        settings.redis_admission_global_capacity = 2
        admit(settings, user)
        assert len(list(runtime.client.scan_iter(match=settings.redis_namespace + ":*"))) == 6


def test_real_wrong_type_is_redacted_before_either_bucket_changes():
    with isolated_redis() as (settings, runtime):
        user = uuid4()
        keys, _ = bucket_keys(settings, user)
        runtime.client.set(keys[1], "private-server-error-canary")
        with pytest.raises(RedisUnavailable) as error:
            admit(settings, user)
        assert not runtime.client.exists(keys[0])
        assert runtime.client.get(keys[1]) == b"private-server-error-canary"
        assert "canary" not in str(error.value) and "WRONGTYPE" not in str(error.value)


def test_real_pool_exhaustion_fails_closed_without_extra_connections():
    with isolated_redis(redis_pool_max_connections=1) as (settings, runtime):
        connection = runtime.client.connection_pool.get_connection()
        try:
            with pytest.raises(RedisUnavailable):
                admit(settings, uuid4())
            assert len(runtime.client.connection_pool._in_use_connections) == 1
        finally:
            runtime.client.connection_pool.release(connection)
        admit(settings, uuid4())
