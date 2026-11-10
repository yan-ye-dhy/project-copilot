"""Opt-in Redis cases own only a random namespace, never a database/server."""
from contextlib import contextmanager
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from project_copilot.infrastructure.redis_runtime import close_redis_runtime, get_redis_runtime


def redis_settings(**overrides):
    values = {
        "redis_enabled": True,
        "redis_url": SecretStr("redis://127.0.0.1:6379/15"),
        "redis_namespace": "pc-test-" + uuid4().hex,
        "redis_socket_timeout_seconds": 0.5,
        "redis_pool_max_connections": 16,
        "redis_admission_enabled": True,
        "redis_admission_global_capacity": 30,
        "redis_admission_global_refill_per_second": 1,
        "redis_admission_user_capacity": 5,
        "redis_admission_user_refill_per_second": 0.2,
        **overrides,
    }
    return SimpleNamespace(**values)


@contextmanager
def isolated_redis(**overrides):
    if os.environ.get("PROJECT_COPILOT_RUN_REDIS_INTEGRATION") != "1":
        pytest.skip("real Redis tests require explicit opt-in")
    settings = redis_settings(
        redis_url=SecretStr(os.environ.get("PROJECT_COPILOT_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")),
        **overrides,
    )
    runtime = get_redis_runtime(settings)
    try:
        runtime.ping()
        yield settings, runtime
    finally:
        try:
            # Delete only this case's unpredictable prefix. No FLUSHDB,
            # FLUSHALL, server startup, shutdown or configuration changes.
            active = get_redis_runtime(settings)
            prefix = (settings.redis_namespace + ":").encode()
            for key in active.client.scan_iter(match=settings.redis_namespace + ":*", count=100):
                if not isinstance(key, bytes) or not key.startswith(prefix):
                    raise AssertionError("Redis test cleanup escaped its namespace")
                active.client.delete(key)
        finally:
            close_redis_runtime(settings)
