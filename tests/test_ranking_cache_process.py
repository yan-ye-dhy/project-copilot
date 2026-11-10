"""Actual process exit does not release a Redis lease before its own TTL."""
import argparse
import multiprocessing
import os
import time
from uuid import uuid4

from pydantic import SecretStr
import pytest

from project_copilot.infrastructure.ranking_cache import CacheLease, RankingCache
from project_copilot.infrastructure.redis_runtime import get_redis_runtime
from redis_support import isolated_redis
from scripts.week1_backend_lab import _synthetic_manifest, test_redis_url


def _acquire_and_exit(settings, identity, sender):
    """Crash-like exit: no context/finally/client close or lease release runs."""
    cache = RankingCache(settings, runtime=get_redis_runtime(settings))
    lease = cache.acquire(identity)
    if lease is None:
        sender.send(None)
        os._exit(2)
    sender.send((lease.owner, lease.fence))
    sender.close()
    os._exit(0)


def test_actual_owner_process_exit_requires_ttl_before_new_owner_can_publish():
    if os.environ.get("PROJECT_COPILOT_RUN_REDIS_INTEGRATION") != "1":
        pytest.skip("actual Redis/process fault test requires explicit opt-in")
    target = os.environ.get("PROJECT_COPILOT_TEST_REDIS_URL")
    try:
        test_redis_url(target)
    except argparse.ArgumentTypeError:
        pytest.fail("process fault test requires an explicit isolated loopback Redis target")
    with isolated_redis(redis_cache_enabled=True, redis_cache_hmac_key=SecretStr(uuid4().hex + uuid4().hex),
                        redis_cache_lock_ttl_seconds=0.5, redis_cache_ttl_seconds=300,
                        redis_cache_negative_ttl_seconds=10, redis_cache_ttl_jitter_ratio=0) as (settings, runtime):
        cache = RankingCache(settings, runtime=runtime)
        manifest, _ = _synthetic_manifest()
        identity = cache.identity("synthetic-process-exit", manifest, pipeline={"lab": "process-exit-v1"})
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_acquire_and_exit, args=(settings, identity, sender))
        process.start()
        sender.close()
        try:
            assert receiver.poll(10), "child did not report its acquired lease"
            original = receiver.recv()
            assert original is not None
            process.join(10)
            assert not process.is_alive() and process.exitcode == 0
            first = CacheLease(*original)
            assert cache.acquire(identity) is None, "process exit unexpectedly released the live lease"
            replacement = None
            deadline = time.monotonic() + 2
            while replacement is None and time.monotonic() < deadline:
                time.sleep(0.01)
                replacement = cache.acquire(identity)
            assert replacement is not None and replacement.fence > first.fence
            assert cache.publish(identity, replacement, [], negative=True) is True
            assert cache.release(identity, first) is False
            assert cache.publish(identity, first, [], negative=True) is False
            assert cache.get(identity, set(), allow_negative=True) == []
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join(5)
            process.close()
