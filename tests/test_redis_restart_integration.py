"""Actual native Redis outage/restart in a newly owned temporary directory.

Never stops the shared test service. Opt in with a native redis-server binary;
Docker-only CI can report this test as skipped instead of fabricating a restart.
"""
import os
from pathlib import Path
import socket
import subprocess
import time
from uuid import uuid4

import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure.redis_runtime import RedisUnavailable, close_redis_runtime, get_redis_runtime
from project_copilot.services.redis_admission import RateLimited, admit


def test_actual_redis_outage_fails_closed_and_aof_restart_preserves_tokens(tmp_path):
    binary = os.environ.get("PROJECT_COPILOT_TEST_REDIS_SERVER_BIN")
    if os.environ.get("PROJECT_COPILOT_RUN_REDIS_INTEGRATION") != "1" or not binary:
        pytest.skip("独立Redis进程重启需显式提供测试用redis-server，不能重启共享或业务服务")
    binary = Path(binary).resolve(strict=True)
    assert binary.name == "redis-server" and os.access(binary, os.X_OK)
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    settings = Settings(_env_file=None, environment="test", public_demo=False,
        database_url=None, deepseek_api_key=None, zhipu_api_key=None,
        redis_enabled=True, redis_admission_enabled=True, redis_cache_enabled=False,
        redis_url=f"redis://127.0.0.1:{port}/15", redis_namespace="pc-test-" + uuid4().hex,
        redis_socket_timeout_seconds=.1, redis_admission_global_capacity=2,
        redis_admission_user_capacity=2, redis_admission_global_refill_per_second=.000001,
        redis_admission_user_refill_per_second=.000001)
    runtime = get_redis_runtime(settings)
    process = None
    with (tmp_path / "redis.log").open("wb") as log:
        def start():
            child = subprocess.Popen([str(binary), "--bind", "127.0.0.1", "--port", str(port),
                "--dir", str(tmp_path), "--save", "", "--appendonly", "yes", "--appendfsync", "always",
                "--maxmemory", "16mb", "--maxmemory-policy", "noeviction"],
                cwd=tmp_path, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise AssertionError("owned test Redis failed to start")
                try:
                    if runtime.ping():
                        return child
                except RedisUnavailable:
                    time.sleep(.02)
            child.terminate(); child.wait(timeout=5)
            raise AssertionError("owned test Redis readiness timed out")
        try:
            process = start()
            user = uuid4()
            admit(settings, user)
            process.terminate(); process.wait(timeout=5)
            with pytest.raises(RedisUnavailable):
                admit(settings, user)
            process = start()
            admit(settings, user)
            with pytest.raises(RateLimited):
                admit(settings, user)
        finally:
            close_redis_runtime(settings)
            if process is not None and process.poll() is None:
                process.terminate(); process.wait(timeout=5)
