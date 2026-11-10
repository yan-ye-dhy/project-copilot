"""Worker resource ownership with model substitutes and actual local OS locks.

No weights, GPU driver, Redis, Docker, database or paid provider is used.
"""
import os
from concurrent.futures import Future
import select
import subprocess
import sys
from threading import Event, Thread
from types import SimpleNamespace

from pydantic import SecretStr
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure import redis_runtime
from project_copilot.infrastructure.resource_limits import GpuModelOwner, ModelResourceBusy, ModelResourceError
from project_copilot.rag import final_retrieval as final
from project_copilot.services.personal_profile import personal_runtime_settings, worker_resource_scope


def settings(tmp_path, *, device="cpu", **kwargs):
    return Settings(_env_file=None, manifest_dir=tmp_path,
                    final_rerank_device=device, final_rerank_dtype="float16" if device == "cuda" else "float32", **kwargs)


def test_personal_worker_reuses_derived_settings_and_closes_base_and_derived_resources(tmp_path, monkeypatch):
    base = settings(tmp_path, redis_enabled=True, redis_url="redis://synthetic:6379/0")
    constructed, closed = [], []
    class Retriever:
        def __init__(self, configuration):
            constructed.append(("retriever", id(configuration)))
            self.identity = id(configuration)
        def close(self):
            closed.append(("retriever", self.identity))
    class Redis:
        def __init__(self, configuration):
            constructed.append(("redis", id(configuration)))
            self.identity = id(configuration)
        def close(self):
            closed.append(("redis", self.identity))
    monkeypatch.setattr(final, "FinalRetriever", Retriever)
    monkeypatch.setattr(redis_runtime, "RedisRuntime", Redis)
    with worker_resource_scope(base):
        final.get_final_retriever(base)
        redis_runtime.get_redis_runtime(base)
        derived = [personal_runtime_settings(base) for _ in range(20)]
        assert len({id(item) for item in derived}) == 1
        assert derived[0].zhipu_timeout_seconds == base.personal_agent_model_timeout_seconds
        assert base.zhipu_timeout_seconds != derived[0].zhipu_timeout_seconds
        for item in derived:
            final.get_final_retriever(item)
            redis_runtime.get_redis_runtime(item)
        assert len(constructed) == 4
    assert sorted(closed) == sorted(constructed)
    assert id(base) not in final._instances and id(derived[0]) not in final._instances
    assert id(base) not in redis_runtime._runtimes and id(derived[0]) not in redis_runtime._runtimes


@pytest.mark.parametrize("target", ["base", "derived", "other"])
def test_worker_scope_rejects_modified_or_different_credentials_before_reusing_resources(tmp_path, target):
    base = settings(tmp_path, deepseek_api_key="first-synthetic-credential")
    with worker_resource_scope(base) as resources:
        derived = personal_runtime_settings(base)
        if target == "other":
            candidate = base.model_copy(update={"deepseek_api_key": SecretStr("second-synthetic-credential")})
        else:
            candidate = base
            changed = base if target == "base" else derived
            changed.deepseek_api_key = SecretStr("second-synthetic-credential")
        with pytest.raises(RuntimeError, match="configuration changed"):
            personal_runtime_settings(candidate)
        if target != "other":
            with pytest.raises(RuntimeError, match="configuration changed"):
                resources.validate()


def test_resource_cleanup_continues_after_one_close_failure_and_preserves_business_error(tmp_path, monkeypatch, caplog):
    base = settings(tmp_path)
    calls = []
    def close_retriever(configuration):
        calls.append(("retriever", configuration))
        raise RuntimeError("private-credential-in-close-error")
    monkeypatch.setattr(final, "close_final_retriever", close_retriever)
    monkeypatch.setattr(redis_runtime, "close_redis_runtime", lambda configuration: calls.append(("redis", configuration)))
    with pytest.raises(ValueError, match="business failure"):
        with worker_resource_scope(base):
            derived = personal_runtime_settings(base)
            raise ValueError("business failure")
    assert calls == [("retriever", derived), ("redis", derived), ("retriever", base), ("redis", base)]
    assert "RuntimeError" in caplog.text and "private-credential" not in caplog.text
    with worker_resource_scope(base):
        assert personal_runtime_settings(base) is not derived


def test_retriever_identity_cache_rejects_mutated_credentials_instead_of_reusing_client_settings(tmp_path, monkeypatch):
    base = settings(tmp_path, zhipu_api_key="first-synthetic")
    made = []
    monkeypatch.setattr(final, "FinalRetriever", lambda configuration: made.append(configuration) or SimpleNamespace(close=lambda: None))
    try:
        original = final.get_final_retriever(base)
        assert final.get_final_retriever(base) is original
        base.zhipu_api_key = SecretStr("second-synthetic")
        with pytest.raises(final.FinalRetrievalError, match="配置已变更"):
            final.get_final_retriever(base)
        other = base.model_copy(deep=True)
        assert final.get_final_retriever(other) is not original
        assert len(made) == 2
    finally:
        final.close_final_retriever(base)
        if "other" in locals():
            final.close_final_retriever(other)


def test_gpu_owner_keeps_one_persistent_private_inode_and_rejects_same_process_contenders(tmp_path):
    first = GpuModelOwner.acquire(tmp_path)
    path = tmp_path / ".model-resources/gpu-reranker.lock"
    inode = path.stat().st_ino
    try:
        with pytest.raises(ModelResourceBusy):
            GpuModelOwner.acquire(tmp_path)
    finally:
        first.close()
    second = GpuModelOwner.acquire(tmp_path)
    second.close()
    assert path.exists() and path.stat().st_ino == inode


@pytest.mark.parametrize("damage", ["directory_permissions", "file_permissions", "symlink"])
def test_gpu_owner_rejects_untrusted_lock_evidence(tmp_path, damage):
    owner = GpuModelOwner.acquire(tmp_path)
    owner.close()
    directory = tmp_path / ".model-resources"
    path = directory / "gpu-reranker.lock"
    if damage == "directory_permissions":
        directory.chmod(0o755)
    elif damage == "file_permissions":
        path.chmod(0o644)
    else:
        path.rename(directory / "original.lock")
        path.symlink_to(directory / "original.lock")
    with pytest.raises(ModelResourceError):
        GpuModelOwner.acquire(tmp_path)


def test_real_process_death_releases_gpu_owner_without_deleting_inode(tmp_path):
    source = '''
import sys
from project_copilot.infrastructure.resource_limits import GpuModelOwner
owner = GpuModelOwner.acquire(sys.argv[1])
print("OWNED", flush=True)
sys.stdin.readline()
owner.close()
'''
    child = subprocess.Popen([sys.executable, "-u", "-c", source, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready and child.stdout.readline().strip() == "OWNED"
        inode = (tmp_path / ".model-resources/gpu-reranker.lock").stat().st_ino
        with pytest.raises(ModelResourceBusy):
            GpuModelOwner.acquire(tmp_path)
        child.kill()
        child.wait(timeout=5)
        successor = GpuModelOwner.acquire(tmp_path)
        successor.close()
        assert (tmp_path / ".model-resources/gpu-reranker.lock").stat().st_ino == inode
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


def test_gpu_ownership_is_lazy_and_a_contender_never_starts_model_loading(tmp_path, monkeypatch):
    first, second = final.SharedReranker(settings(tmp_path, device="cuda")), final.SharedReranker(settings(tmp_path, device="cuda"))
    calls = []
    monkeypatch.setattr(first.scorer, "score", lambda *_: calls.append("first"))
    monkeypatch.setattr(second.scorer, "score", lambda *_: calls.append("second"))
    probe = GpuModelOwner.acquire(tmp_path)
    probe.close()  # Construction has no GPU ownership or model load.
    try:
        first.prepare()
        with pytest.raises(final.FinalRetrievalError):
            second.prepare()
        assert calls == ["first"]
        first.close()
        second.prepare()  # Explicit later request, never an automatic retry.
        assert calls == ["first", "second"]
    finally:
        first.close()
        second.close()


def test_gpu_timeout_and_close_keep_owner_until_real_work_drains(tmp_path, monkeypatch):
    runtime = final.SharedReranker(settings(tmp_path, device="cuda", final_rerank_timeout_seconds=0.03))
    started, finish, closed = Event(), Event(), Event()
    def slow_score(*_):
        started.set()
        assert finish.wait(5)
    monkeypatch.setattr(runtime.scorer, "score", slow_score)
    closer = None
    try:
        with pytest.raises(final.FinalRetrievalError, match="超时"):
            runtime.prepare()
        assert started.is_set() and runtime._pending == 1
        closer = Thread(target=lambda: (runtime.close(), closed.set()))
        closer.start()
        assert not closed.wait(0.03)
        with pytest.raises(ModelResourceBusy):
            GpuModelOwner.acquire(tmp_path)
        finish.set()
        closer.join(5)
        assert closed.is_set() and runtime._pending == 0
        owner = GpuModelOwner.acquire(tmp_path)
        owner.close()
    finally:
        finish.set()
        if closer is not None:
            closer.join(5)
        runtime.close()


def test_reranker_rejects_extra_submissions_without_growing_executor_queue(tmp_path, monkeypatch):
    runtime = final.SharedReranker(settings(tmp_path, final_rerank_timeout_seconds=5))
    started, finish = Event(), Event()
    failures = []
    monkeypatch.setattr(runtime.scorer, "score", lambda *_: (started.set(), finish.wait(5)))
    def run():
        try:
            runtime.prepare()
        except BaseException as exc:
            failures.append(type(exc).__name__)
    worker = Thread(target=run)
    worker.start()
    try:
        assert started.wait(2)
        for _ in range(20):
            with pytest.raises(final.FinalRetrievalError, match="繁忙"):
                runtime.prepare()
        assert runtime._pending == 1 and runtime._executor._work_queue.qsize() == 0
        finish.set()
        worker.join(5)
        assert not worker.is_alive() and failures == []
    finally:
        finish.set()
        worker.join(5)
        runtime.close()


def test_completed_future_with_delayed_callback_does_not_reject_next_serial_request(tmp_path, monkeypatch):
    runtime = final.SharedReranker(settings(tmp_path))
    submitted = []
    class DelayedCallback(Future):
        def add_done_callback(self, callback):
            self.callback = callback
    def submit(*_):
        future = DelayedCallback()
        future.set_result(None)
        submitted.append(future)
        return future
    monkeypatch.setattr(final, "tracked_submit", submit)
    try:
        runtime.prepare()
        runtime.prepare()
        assert len(submitted) == 2 and runtime._pending == 0
        submitted[0].callback(submitted[0])
        assert runtime._pending_future is submitted[1]
    finally:
        runtime.close()


def test_gpu_cleanup_failure_keeps_owner_until_explicit_successful_cleanup(tmp_path, monkeypatch):
    runtime = final.SharedReranker(settings(tmp_path, device="cuda"))
    def rejected():
        raise RuntimeError("controlled allocator cleanup failure")
    cuda = SimpleNamespace(empty_cache=rejected)
    monkeypatch.setattr(runtime.scorer, "score", lambda *_: None)
    runtime.prepare()
    runtime.scorer._torch = SimpleNamespace(cuda=cuda)
    try:
        with pytest.raises(RuntimeError, match="allocator"):
            runtime.close()
        with pytest.raises(ModelResourceBusy):
            GpuModelOwner.acquire(tmp_path)
        cuda.empty_cache = lambda: None
        runtime.close()
        owner = GpuModelOwner.acquire(tmp_path)
        owner.close()
    finally:
        cuda.empty_cache = lambda: None
        runtime.close()


def test_worker_cli_passes_pool_budget_and_cleans_derived_and_base_resources(tmp_path, monkeypatch):
    import scripts.ai_worker as worker
    base = settings(tmp_path, public_demo=True, database_url="postgresql+psycopg://synthetic:synthetic@localhost:5434/controlled",
                    public_proxy_secret="synthetic-worker-resource-" * 3, auth_public_origin="https://synthetic.example",
                    database_pool_size=3, database_max_overflow=0)
    events = []
    class Database:
        def __init__(self, _url, **kwargs):
            events.append(("database", kwargs))
        def close(self):
            events.append(("database_close",))
    def run(_database, active):
        assert active is base
        first = personal_runtime_settings(active)
        assert personal_runtime_settings(active) is first
        events.append(("run", id(first)))
        return True
    monkeypatch.setattr(sys, "argv", ["ai_worker", "--once"])
    monkeypatch.setattr(worker, "_STOPPING", Event())
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker, "load_settings", lambda: base)
    monkeypatch.setattr(worker, "Database", Database)
    monkeypatch.setattr(worker, "expire_unclaimed", lambda *_, **__: None)
    monkeypatch.setattr(worker, "process_one", run)
    monkeypatch.setattr(final, "close_final_retriever", lambda active: events.append(("retriever_close", id(active))))
    monkeypatch.setattr(redis_runtime, "close_redis_runtime", lambda active: events.append(("redis_close", id(active))))
    worker.main()
    assert events[0] == ("database", {"connect_timeout": 5, "pool_timeout": 5, "pool_size": 3, "max_overflow": 0})
    derived_id = events[1][1]
    assert events[2:] == [("retriever_close", derived_id), ("redis_close", derived_id),
                          ("retriever_close", id(base)), ("redis_close", id(base)), ("database_close",)]
    base.database_pool_size = 2
    monkeypatch.setattr(worker, "Database", lambda *_args, **_kwargs: pytest.fail("pool budget rejected before engine construction"))
    with pytest.raises(SystemExit) as rejected:
        worker.main()
    assert rejected.value.code == 2
