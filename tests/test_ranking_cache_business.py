"""Retrieval fallback contracts without Redis sockets or real models."""
from pydantic import SecretStr

from project_copilot.infrastructure import ranking_cache as caching
from project_copilot.infrastructure.redis_runtime import RedisUnavailable
from test_final_retrieval import system, ingest


def test_redis_read_failure_runs_full_retrieval_without_acquiring_or_filling(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "synthetic.txt", "缓存故障合成知识。")
    active = system[0]
    active.redis_enabled = active.redis_cache_enabled = True
    active.redis_cache_hmac_key = SecretStr("synthetic-secret-key-" * 2)
    attempts = []
    class Unavailable:
        def eval(self, script, *_):
            attempts.append(script)
            raise RedisUnavailable()
    monkeypatch.setattr(caching, "get_redis_runtime", lambda _: Unavailable())
    first = system[3].search("知识")
    assert first == system[3].search("知识")
    assert len(system[3].reranker.seen) == 2
    assert attempts == [caching.GET_LUA, caching.GET_LUA]


def test_public_missing_account_uses_original_path_without_shared_local_cache(system, tmp_path, monkeypatch):
    ingest(system, tmp_path, "synthetic.txt", "公开权限scope合成知识。")
    active = system[0]
    active.redis_enabled = active.redis_cache_enabled = active.public_demo = True
    active.redis_cache_hmac_key = SecretStr("synthetic-secret-key-" * 2)
    monkeypatch.setattr(caching, "get_redis_runtime", lambda _: (_ for _ in ()).throw(AssertionError("unscoped Redis")))
    assert system[3].search("知识") == system[3].search("知识")
    assert len(system[3].reranker.seen) == 2
