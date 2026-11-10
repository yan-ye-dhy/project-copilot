"""Offline admission and pool contracts: no sockets, database or models."""
from concurrent.futures import ThreadPoolExecutor
import sys
from types import SimpleNamespace
from uuid import uuid4

from pydantic import SecretStr
import pytest

from project_copilot.infrastructure import redis_runtime as runtime_module
from project_copilot.infrastructure.redis_runtime import RedisRuntime, RedisUnavailable, close_redis_runtime, get_redis_runtime
from project_copilot.services import redis_admission as admission
from project_copilot.services.redis_admission import RateLimited, admit, bucket_keys
from redis_support import redis_settings


class FakeClient:
    def __init__(self):
        self.result = [1, 0]
        self.calls = []
        self.connection_pool = SimpleNamespace(disconnect=lambda: self.calls.append("disconnect"))

    def eval(self, *arguments):
        self.calls.append(arguments)
        return self.result

    def ping(self):
        self.calls.append("ping")
        return True

    def close(self):
        self.calls.append("close")


def test_disabled_paths_do_not_construct_or_probe_redis(monkeypatch):
    monkeypatch.setattr(runtime_module, "_create_client", lambda _: pytest.fail("disabled Redis constructed a client"))
    settings = redis_settings(redis_enabled=False, redis_admission_enabled=False)
    assert get_redis_runtime(settings) is None
    assert admit(settings, "unvalidated-disabled-identity") is None
    close_redis_runtime(settings)


def test_enabled_admission_without_enabled_runtime_fails_closed():
    with pytest.raises(RedisUnavailable):
        admit(redis_settings(redis_enabled=False), uuid4())


def test_one_runtime_per_settings_and_explicit_close(monkeypatch):
    created = []
    def create(_):
        client = FakeClient()
        created.append(client)
        return client
    monkeypatch.setattr(runtime_module, "_create_client", create)
    settings = redis_settings()
    with ThreadPoolExecutor(max_workers=8) as pool:
        runtimes = list(pool.map(lambda _: get_redis_runtime(settings), range(20)))
    assert len(created) == 1 and all(value is runtimes[0] for value in runtimes)
    assert not created[0].calls  # Construction remains lazy.
    assert runtimes[0].ping() is True
    close_redis_runtime(settings)
    close_redis_runtime(settings)
    assert created[0].calls == ["ping", "close", "disconnect"]
    assert get_redis_runtime(settings) is not runtimes[0]
    close_redis_runtime(settings)


def test_pool_options_disable_automatic_retries_and_bound_connections(monkeypatch):
    captured = {}
    pool = SimpleNamespace(disconnect=lambda: None)
    class Pool:
        @classmethod
        def from_url(cls, url, **kwargs):
            captured.update(url=url, **kwargs)
            return pool
    class Retry:
        def __init__(self, backoff, count):
            self.count = count
    fake_redis = SimpleNamespace(ConnectionPool=Pool, Redis=lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    monkeypatch.setitem(sys.modules, "redis.backoff", SimpleNamespace(NoBackoff=object))
    monkeypatch.setitem(sys.modules, "redis.retry", SimpleNamespace(Retry=Retry))
    settings = redis_settings(redis_pool_max_connections=3, redis_socket_timeout_seconds=0.25)
    runtime = RedisRuntime(settings)
    assert runtime.client.connection_pool is pool
    assert captured["max_connections"] == 3
    assert captured["socket_timeout"] == captured["socket_connect_timeout"] == 0.25
    assert captured["retry"].count == 0 and captured["retry_on_timeout"] is False
    assert captured["health_check_interval"] == 0


@pytest.mark.parametrize("url", [None, "private-canary", "redis://localhost:6379/0?retry_on_timeout=true",
    "redis://localhost:6379/0#private-canary", "http://localhost:6379/0", "redis://localhost/0",
    "redis://localhost:6379/-1", "redis://localhost:99999/0"])
def test_connection_configuration_rejects_url_overrides_and_redacts(url):
    settings = redis_settings(redis_url=SecretStr(url) if isinstance(url, str) else url)
    with pytest.raises(RedisUnavailable) as error:
        runtime_module._connection_configuration(settings)
    assert "private-canary" not in str(error.value)


@pytest.mark.parametrize("database", ["0", "1", "15", "00", "01"])
def test_connection_configuration_matches_settings_database_indices(database):
    url = f"redis://127.0.0.1:6379/{database}"
    assert runtime_module._connection_configuration(redis_settings(redis_url=SecretStr(url)))[0] == url


@pytest.mark.parametrize("operation", ["eval", "ping", "close"])
def test_runtime_errors_are_redacted_and_never_retried(monkeypatch, operation):
    client = FakeClient()
    attempts = []
    def failed(*_):
        attempts.append(1)
        raise TypeError("private-password-and-provider-body-canary")
    setattr(client, operation, failed)
    monkeypatch.setattr(runtime_module, "_create_client", lambda _: client)
    runtime = RedisRuntime(redis_settings())
    with pytest.raises(RedisUnavailable) as error:
        if operation == "eval":
            runtime.eval("script", ["key"], [1])
        else:
            getattr(runtime, operation)()
    assert attempts == [1] and "canary" not in str(error.value)
    assert not isinstance(error.value, ValueError)
    if operation == "close":
        assert client.calls == ["disconnect"]


@pytest.mark.parametrize("milliseconds,expected", [(1, 1), (1000, 1), (1001, 2), (4999, 5)])
def test_retry_after_rounds_up_and_executes_only_once(monkeypatch, milliseconds, expected):
    client = FakeClient()
    client.result = [0, milliseconds]
    monkeypatch.setattr(admission, "get_redis_runtime", lambda _: SimpleNamespace(eval=lambda *args: client.eval(*args)))
    with pytest.raises(RateLimited) as error:
        admit(redis_settings(), uuid4())
    assert error.value.retry_after == expected and len(client.calls) == 1
    assert not isinstance(error.value, ValueError)


@pytest.mark.parametrize("result", [None, [], [1], [1, 0, 0], [True, 0], [0, -1], [0, 0], [1, 5], [2, 0], [0, "1000"]])
def test_malformed_script_result_fails_closed(monkeypatch, result):
    monkeypatch.setattr(admission, "get_redis_runtime", lambda _: SimpleNamespace(eval=lambda *_: result))
    with pytest.raises(RedisUnavailable):
        admit(redis_settings(), uuid4())


def test_keys_bind_scope_config_and_account_without_plain_user_id():
    settings = redis_settings()
    user = uuid4()
    keys, values = bucket_keys(settings, user)
    other, _ = bucket_keys(settings, uuid4())
    provider, _ = bucket_keys(settings, user, "provider")
    assert keys[0] == other[0] and keys[1] != other[1]
    assert keys[0] != provider[0]
    assert all(str(user) not in key and user.hex not in key for key in keys)
    assert keys[0].split("{")[1].split("}")[0] == keys[1].split("{")[1].split("}")[0]
    settings.redis_admission_user_capacity = 6
    changed, _ = bucket_keys(settings, user)
    assert not set(keys) & set(changed)
    assert values == (30, 1, 5, 0.2)


@pytest.mark.parametrize("updates", [{"redis_namespace": "bad:*"}, {"redis_admission_global_capacity": 0},
    {"redis_admission_global_refill_per_second": float("nan")}, {"redis_admission_user_refill_per_second": 0},
    {"redis_admission_user_capacity": True}])
def test_invalid_admission_configuration_never_reaches_redis(monkeypatch, updates):
    monkeypatch.setattr(admission, "get_redis_runtime", lambda _: pytest.fail("invalid configuration reached Redis"))
    with pytest.raises(RedisUnavailable):
        admit(redis_settings(**updates), uuid4())
