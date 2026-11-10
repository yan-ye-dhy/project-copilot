"""The local lab defaults to plans and rejects production targets before I/O."""
from contextlib import contextmanager
import json
from threading import Lock

from pydantic import SecretStr
import pytest

from scripts import week1_backend_lab as lab
from project_copilot.services.redis_admission import RateLimited


def forbid_network(monkeypatch):
    monkeypatch.setattr(lab, "get_redis_runtime", lambda *_: pytest.fail("default CLI opened Redis"))
    monkeypatch.setattr(lab, "admit", lambda *_a, **_k: pytest.fail("default CLI attempted admission"))


@pytest.mark.parametrize("arguments", [[], ["rate"], ["locks"], ["cache"], ["rate", "--with-cache"],
    ["rate", "--redis-url", "redis://127.0.0.1:16379/15"]])
def test_planning_never_constructs_clients(monkeypatch, capsys, arguments):
    forbid_network(monkeypatch)
    assert lab.main(arguments) == 0
    if arguments:
        report = json.loads(capsys.readouterr().out)
        assert report["mode"] == "planned" and report["network_accessed"] is False
        assert "cases" not in report


@pytest.mark.parametrize("url", ["redis://production:6379/0", "redis://127.0.0.1:6379/15",
    "redis://127.0.0.1:16379/0", "redis://127.0.0.1:16379/15?password=private-canary",
    "redis://private-canary@198.51.100.1:16379/15", "redis://127.0.0.1:16379/15#private-canary",
    "rediss://127.0.0.1:16379/15"])
def test_non_lab_targets_are_rejected_without_secret_echo(monkeypatch, capsys, url):
    forbid_network(monkeypatch)
    with pytest.raises(SystemExit) as error:
        lab.main(["rate", "--execute", "--redis-url", url])
    assert error.value.code == 2 and "private-canary" not in capsys.readouterr().err


@pytest.mark.parametrize("url", ["redis://127.0.0.1:16379/15", "redis://localhost:16380/15",
    "redis://[::1]:16379/15", "redis://account:private-canary@127.0.0.1:16379/15"])
def test_allowed_urls_are_secret_values(url):
    value = lab.test_redis_url(url)
    assert isinstance(value, SecretStr) and value.get_secret_value() == url
    assert "private-canary" not in repr(value)


@pytest.mark.parametrize("options", [["--execute"], ["--samples", "501"],
    ["--concurrency", "101", "--samples", "101"], ["--concurrency", "10", "--samples", "5"],
    ["--concurrency", "10", "50", "100", "--samples", "200"], ["--concurrency", "10", "10"],
    ["--capacity", "501"], ["--samples", "0"]])
def test_invalid_or_unbounded_requests_fail_before_io(monkeypatch, options):
    forbid_network(monkeypatch)
    with pytest.raises(SystemExit) as error:
        lab.main(["rate", *options])
    assert error.value.code == 2


def test_latency_quantiles_and_empty_groups():
    result = lab.latency_summary([1, 2, 3, 4, 5])
    assert result == {"count": 5, "mean_ms": 3, "p50_ms": 3, "p95_ms": 4.8, "p99_ms": 4.96}
    assert lab.latency_summary([])["p95_ms"] is None
    assert lab.latency_summary([7])["p99_ms"] == 7


def test_rate_report_separates_quota_denials_from_unexpected_errors(monkeypatch):
    @contextmanager
    def runtime(*_args, **_kwargs):
        yield object(), None
    monkeypatch.setattr(lab, "_isolated_runtime", runtime)
    lock = Lock()
    attempts = []
    def controlled(_settings, user, **_kwargs):
        with lock:
            attempts.append(user)
            position = len(attempts)
        if position == 2:
            raise RateLimited(1)
        if position == 3:
            raise RuntimeError("private-canary-provider-response")
    monkeypatch.setattr(lab, "admit", controlled)
    report = lab._rate_case(SecretStr("unused"), 4, 4, 2)
    assert report["admitted"] == 2 and report["quota_rejected"] == report["unexpected_error"] == 1
    assert report["unexpected_error_rate"] == report["quota_rejection_rate"] == 0.25
    assert report["tokens_not_overissued"] is True and report["rps"] > 0
    assert report["latency_all"]["count"] == 4 and len(set(attempts)) == 4
    assert "private-canary" not in json.dumps(report)


def test_measured_output_is_saved_without_url_or_secret(monkeypatch, capsys, tmp_path):
    report = {"cases": [{"tokens_not_overissued": True, "unexpected_error": 0}]}
    monkeypatch.setattr(lab, "run_rate", lambda _: report)
    output = tmp_path / "measured.json"
    assert lab.main(["rate", "--execute", "--redis-url", "redis://user:private-canary@127.0.0.1:16379/15",
                     "--output", str(output)]) == 0
    saved = json.loads(output.read_text())
    assert saved["mode"] == "measured" and saved["cases"] == report["cases"]
    assert "private-canary" not in output.read_text() + capsys.readouterr().out


def test_existing_output_is_preserved(monkeypatch, tmp_path):
    forbid_network(monkeypatch)
    output = tmp_path / "existing.json"
    output.write_text("retained")
    assert lab.main(["rate", "--output", str(output)]) == 1
    assert output.read_text() == "retained"


def test_execution_failure_is_redacted(monkeypatch, capsys):
    def failed(_):
        raise RuntimeError("redis://private-password-canary@127.0.0.1:16379/15")
    monkeypatch.setattr(lab, "run_rate", failed)
    assert lab.main(["rate", "--execute", "--redis-url", "redis://127.0.0.1:16379/15"]) == 1
    captured = capsys.readouterr()
    assert "private-password-canary" not in captured.err + captured.out


def test_measured_unexpected_error_returns_failure_without_disguising_quota(monkeypatch, capsys):
    monkeypatch.setattr(lab, "run_rate", lambda _: {"cases": [{"tokens_not_overissued": True, "unexpected_error": 1}]})
    assert lab.main(["rate", "--execute", "--redis-url", "redis://127.0.0.1:16379/15"]) == 1
    assert json.loads(capsys.readouterr().out)["mode"] == "measured"


def test_existing_output_prevents_experiment_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "run_rate", lambda _: pytest.fail("existing report should be checked before Redis"))
    output = tmp_path / "retained.json"
    output.write_text("previous measurement")
    assert lab.main(["rate", "--execute", "--redis-url", "redis://127.0.0.1:16379/15",
                     "--output", str(output)]) == 1
    assert output.read_text() == "previous measurement"


def test_locks_uses_real_cache_adapter_and_checks_stale_owner(monkeypatch):
    from types import SimpleNamespace
    from project_copilot.infrastructure import ranking_cache

    @contextmanager
    def runtime(*_args, **_kwargs):
        yield object(), object()
    monkeypatch.setattr(lab, "_isolated_runtime", runtime)
    first = SimpleNamespace(owner="a", fence=10)
    second = SimpleNamespace(owner="b", fence=11)
    calls = []
    class Cache:
        def __init__(self, *_args, **_kwargs):
            self.leases = iter([first, None, second])
        def identity(self, query, manifest, *, pipeline):
            calls.append("identity")
            assert manifest.documents == ()
            return "identity"
        def acquire(self, _identity):
            calls.append("acquire")
            return next(self.leases)
        def release(self, _identity, lease):
            calls.append(("release", lease.owner))
            return False
        def publish(self, _identity, lease, records, *, negative):
            calls.append(("publish", lease.owner))
            assert negative is True and records == []
            return lease is second
        def get(self, *_args, **_kwargs):
            return []
    monkeypatch.setattr(ranking_cache, "RankingCache", Cache)
    monkeypatch.setattr(lab.time, "sleep", lambda _: None)
    report = lab.run_locks(SimpleNamespace(redis_url=SecretStr("unused")))
    assert all(report["checks"].values())
    assert calls[-3:] == [("release", "a"), ("publish", "a"), ("publish", "b")]
    assert report["fences"] == {"A": 10, "B": 11}
    assert "owner" not in json.dumps(report)


@pytest.mark.parametrize("arguments", [["cache", "--samples", "99"], ["cache", "--samples", "251"],
    ["cache", "--concurrency", "101"], ["rate", "--with-cache", "--samples", "101"]])
def test_cache_budget_and_concurrency_are_bounded_before_network(monkeypatch, arguments):
    forbid_network(monkeypatch)
    with pytest.raises(SystemExit) as error:
        lab.main(arguments)
    assert error.value.code == 2


def test_cache_pair_uses_identical_inputs_and_real_metadata_adapter(monkeypatch):
    from types import SimpleNamespace
    from project_copilot.infrastructure import ranking_cache
    shared = {}
    owners = set()
    mutex = Lock()
    class Cache:
        def __init__(self, *_args, **_kwargs):
            self.last_status = "miss"
        def identity(self, query, *_args, **_kwargs):
            return query
        def get(self, identity, *_args, **_kwargs):
            with mutex:
                return shared.get(identity)
        def acquire(self, identity):
            with mutex:
                if identity in owners:
                    self.last_status = "busy"
                    return None
                owners.add(identity)
                return object()
        def publish(self, identity, _lease, records):
            with mutex:
                shared[identity] = records
                owners.remove(identity)
            return True
    @contextmanager
    def runtime(*_args, **_kwargs):
        yield object(), object()
    monkeypatch.setattr(lab, "_isolated_runtime", runtime)
    monkeypatch.setattr(ranking_cache, "RankingCache", Cache)
    report = lab.run_cache(SimpleNamespace(redis_url=SecretStr("unused"), samples=100, concurrency=10))
    assert report["paired_inputs"] == 100 and report["total_attempts"] == 200
    assert report["disabled"]["compute_calls"] == 100
    assert report["enabled"]["compute_calls"] == 51
    assert report["enabled"]["cache_hits"] == 49
    assert all(report["checks"].values()) and not owners
    assert set(report["enabled"]["groups"]) == {"distinct_keys", "shared_hotspot"}
    assert "excludes models, GPU, HTTP" in report["measurement_scope"]


def test_rate_with_cache_dispatch_uses_maximum_concurrency_and_hundred_inputs(monkeypatch):
    from types import SimpleNamespace
    observed = []
    monkeypatch.setattr(lab, "_rate_case", lambda *_args: {})
    def cache(arguments, **kwargs):
        observed.append(kwargs)
        return {"paired_inputs": kwargs["samples"]}
    monkeypatch.setattr(lab, "run_cache", cache)
    arguments = SimpleNamespace(redis_url=SecretStr("unused"), concurrency=[10, 50, 100], samples=100,
                                capacity=20, with_cache=True)
    assert lab.run_rate(arguments)["cache_comparison"]["paired_inputs"] == 100
    assert observed == [{"concurrency": 100, "samples": 100}]
