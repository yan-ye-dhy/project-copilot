"""Bounded local backend experiments. Planning is the default; no service startup."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import statistics
import sys
from threading import Barrier
import time
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr

from project_copilot.config import Settings
from project_copilot.infrastructure.redis_runtime import close_redis_runtime, get_redis_runtime
from project_copilot.services.redis_admission import RateLimited, admit


def test_redis_url(value):
    """Reject non-lab targets before any client construction, without echoing URLs."""
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
        valid = (parsed.scheme == "redis" and loopback and parsed.port in {16379, 16380}
                 and parsed.path == "/15" and not parsed.query and not parsed.fragment
                 and not any(ord(character) < 33 for character in value))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError("仅允许loopback端口16379/16380、数据库15的隔离Redis地址")
    return SecretStr(value)


# This validator is a CLI helper, not a pytest test function.
test_redis_url.__test__ = False


def _positive(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("参数必须为正整数") from None
    if number < 1:
        raise argparse.ArgumentTypeError("参数必须为正整数")
    return number


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="experiment")
    for name, description in (("rate", "measure atomic global/account admission"),
                              ("cache", "compare paired synthetic calculations with and without ranking cache"),
                              ("locks", "demonstrate the ranking cache's actual lease protocol")):
        command = commands.add_parser(name, help=description)
        command.add_argument("--execute", action="store_true", help="execute against the explicitly selected isolated Redis")
        command.add_argument("--redis-url", type=test_redis_url, help="explicit loopback test endpoint; never recorded")
        command.add_argument("--output", type=Path, help="write a new JSON file; existing files are preserved")
        if name == "rate":
            command.add_argument("--concurrency", nargs="+", type=_positive, default=[10, 50, 100])
            command.add_argument("--samples", type=_positive, default=100, help="attempts per concurrency case; total at most 500")
            command.add_argument("--capacity", type=_positive, default=20, help="global tokens available in each independent case")
            command.add_argument("--with-cache", action="store_true", help="also compare 100 paired cache inputs; shares the 500-attempt budget")
        elif name == "cache":
            command.add_argument("--concurrency", type=int, choices=(10, 50, 100), default=10)
            command.add_argument("--samples", type=_positive, default=100, help="same input count in each mode, at least 100 and at most 250")
    queue = commands.add_parser("queue", help="two CPU processes and crash boundaries in a newly owned PostgreSQL test database")
    queue.add_argument("--execute", action="store_true")
    queue.add_argument("--output", type=Path)
    return parser


def run_queue(arguments):
    from scripts.week1_queue_lab import run_queue as experiment
    return experiment(arguments)


def latency_summary(milliseconds):
    """Linear interpolation at rank p*(n-1), including meaningful small samples."""
    if not milliseconds:
        return {"count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None}
    ordered = sorted(milliseconds)
    def percentile(fraction):
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 6)
    return {"count": len(ordered), "mean_ms": round(statistics.fmean(ordered), 6),
            "p50_ms": percentile(0.5), "p95_ms": percentile(0.95), "p99_ms": percentile(0.99)}


@contextmanager
def _isolated_runtime(url, *, pool_size, **overrides):
    namespace = "pc-lab-" + uuid4().hex
    values = dict(environment="test", public_demo=False, database_url=None,
                  zhipu_api_key=None, deepseek_api_key=None, public_proxy_secret=None, auth_public_origin=None,
                  tracing_enabled=False, redis_enabled=True, redis_url=url,
                  redis_cache_enabled=False, redis_cache_hmac_key=None, redis_admission_enabled=False,
                  redis_namespace=namespace, redis_pool_max_connections=pool_size,
                  redis_socket_timeout_seconds=0.5)
    values.update(overrides)
    settings = Settings(_env_file=None, **values)
    runtime = get_redis_runtime(settings)
    try:
        runtime.ping()
        yield settings, runtime
    finally:
        try:
            prefix = (namespace + ":").encode()
            for key in runtime.client.scan_iter(match=namespace + ":*", count=100):
                if not isinstance(key, bytes) or not key.startswith(prefix):
                    raise RuntimeError("isolated namespace mismatch")
                runtime.client.delete(key)
        finally:
            close_redis_runtime(settings)


def _rate_case(url, concurrency, samples, capacity):
    with _isolated_runtime(url, pool_size=concurrency, redis_admission_enabled=True,
                           redis_admission_global_capacity=capacity,
                           redis_admission_global_refill_per_second=0.000001,
                           redis_admission_user_capacity=1,
                           redis_admission_user_refill_per_second=0.000001) as (settings, _runtime):
        barrier = Barrier(concurrency + 1)
        def attempt(index):
            user_id = uuid4()  # Every attempt uses a distinct synthetic account.
            if index < concurrency:
                barrier.wait(timeout=30)
            started = time.perf_counter()
            try:
                admit(settings, user_id, scope="jobs")
                outcome = "admitted"
            except RateLimited:
                outcome = "quota_rejected"
            except Exception:
                # Raw errors can contain a password or request data.
                outcome = "unexpected_error"
            return outcome, (time.perf_counter() - started) * 1000

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(attempt, index) for index in range(samples)]
            started = time.perf_counter()
            barrier.wait(timeout=30)
            results = [future.result() for future in futures]
            elapsed = time.perf_counter() - started
        counts = {status: sum(outcome == status for outcome, _ in results)
                  for status in ("admitted", "quota_rejected", "unexpected_error")}
        return {"concurrency": concurrency, "samples": samples, "global_capacity": capacity,
                "pool_max_connections": concurrency, "elapsed_seconds": round(elapsed, 6),
                "rps": round(samples / elapsed, 6), "admitted_rps": round(counts["admitted"] / elapsed, 6),
                **counts, "quota_rejection_rate": counts["quota_rejected"] / samples,
                "unexpected_error_rate": counts["unexpected_error"] / samples,
                "tokens_not_overissued": counts["admitted"] <= capacity,
                "latency_all": latency_summary([latency for _, latency in results]),
                "latency_admitted": latency_summary([latency for outcome, latency in results if outcome == "admitted"]),
                "latency_quota_rejected": latency_summary([latency for outcome, latency in results if outcome == "quota_rejected"]),
                "latency_unexpected_error": latency_summary([latency for outcome, latency in results if outcome == "unexpected_error"])}


def run_rate(arguments):
    report = {"cases": [_rate_case(arguments.redis_url, concurrency, arguments.samples, arguments.capacity)
                       for concurrency in arguments.concurrency],
            "measurement_scope": "local Redis admission only; excludes HTTP, PostgreSQL and models",
            "latency_clock": "worker entry to admission return; excludes executor queue wait",
            "warmup": "one Redis PING/connection; additional connection setup is measured",
            "refill_per_second": 0.000001, "accounts": "distinct synthetic UUID per attempt"}
    if arguments.with_cache:
        report["cache_comparison"] = run_cache(arguments, concurrency=max(arguments.concurrency), samples=100)
    return report


def _synthetic_manifest():
    from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest
    from uuid import NAMESPACE_DNS, uuid5

    chunk_id = uuid5(NAMESPACE_DNS, "project-copilot-week1-cache-lab-chunk")
    manifest = IndexManifest(spec=DenseIndexSpec(collection_name="pc_week1_cache_lab",
        embedding_model="synthetic-lab", dimensions=1, splitter_version="lab-v1"),
        documents=(CompletedDocument(document_id=uuid5(NAMESPACE_DNS, "project-copilot-week1-cache-lab-document"),
            content_hash="0" * 64, source_name="synthetic-lab.txt", chunk_ids=(chunk_id,)),))
    return manifest, chunk_id


def _cache_summary(results, elapsed):
    counts = {name: sum(result[name] for result in results) for name in (
        "cache_hits", "initial_misses", "compute_calls", "lease_busy", "cache_failures", "fallback_computes", "unexpected_error")}
    return {"samples": len(results), "elapsed_seconds": round(elapsed, 6),
            "rps": round(len(results) / elapsed, 6), **counts,
            "unexpected_error_rate": counts["unexpected_error"] / len(results),
            "results_match": all(result["results_match"] for result in results),
            "latency": latency_summary([result["latency_ms"] for result in results])}


def _cache_mode(settings, runtime, inputs, concurrency, *, enabled):
    from project_copilot.infrastructure.ranking_cache import RankingCache, RankingRecord

    manifest, chunk_id = _synthetic_manifest()
    expected = [RankingRecord(chunk_id, 1.0)]
    barrier = Barrier(concurrency + 1)
    def attempt(index, query, scenario):
        if index < concurrency:
            barrier.wait(timeout=30)
        started = time.perf_counter()
        metrics = {name: 0 for name in ("cache_hits", "initial_misses", "compute_calls", "lease_busy",
                                        "cache_failures", "fallback_computes", "unexpected_error")}
        def compute():
            metrics["compute_calls"] += 1
            time.sleep(0.008)  # Explicit synthetic read-only work, never a model/GPU call.
            return expected
        try:
            if not enabled:
                records = compute()
            else:
                # last_status belongs to one request, so concurrent operations
                # cannot overwrite another request's cache diagnostics.
                cache = RankingCache(settings, runtime=runtime)
                identity = cache.identity(query, manifest, pipeline={"lab": "synthetic-eight-ms-v1"})
                records = cache.get(identity, {chunk_id})
                if records is not None:
                    metrics["cache_hits"] = 1
                else:
                    metrics["initial_misses"] = 1
                    lease = cache.acquire(identity)
                    if lease is not None:
                        records = compute()
                        if not cache.publish(identity, lease, records):
                            metrics["cache_failures"] += 1
                    elif cache.last_status == "unavailable":
                        metrics["cache_failures"] += 1
                        metrics["fallback_computes"] = 1
                        records = compute()
                    else:
                        metrics["lease_busy"] = 1
                        deadline = time.perf_counter() + 2
                        while records is None and time.perf_counter() < deadline:
                            time.sleep(0.001)
                            records = cache.get(identity, {chunk_id})
                            if cache.last_status == "unavailable":
                                metrics["cache_failures"] += 1
                                break
                        if records is not None:
                            metrics["cache_hits"] = 1
                        else:
                            metrics["fallback_computes"] = 1
                            records = compute()
            matches = records == expected
        except Exception:
            metrics["unexpected_error"] = 1
            matches = False
        finished = time.perf_counter()
        return {**metrics, "scenario": scenario, "results_match": matches,
                "started": started, "finished": finished, "latency_ms": (finished - started) * 1000}

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(attempt, index, query, scenario) for index, (query, scenario) in enumerate(inputs)]
        started = time.perf_counter()
        barrier.wait(timeout=30)
        results = [future.result() for future in futures]
        elapsed = time.perf_counter() - started
    summary = _cache_summary(results, elapsed)
    summary["groups"] = {}
    for scenario in ("distinct_keys", "shared_hotspot"):
        selected = [result for result in results if result["scenario"] == scenario]
        # Groups overlap in time at high concurrency; their independent RPS
        # values are diagnostic windows and must not be added together.
        group_elapsed = max(result["finished"] for result in selected) - min(result["started"] for result in selected)
        summary["groups"][scenario] = _cache_summary(selected, group_elapsed)
    return summary


def run_cache(arguments, *, concurrency=None, samples=None):
    concurrency = arguments.concurrency if concurrency is None else concurrency
    samples = arguments.samples if samples is None else samples
    distinct = samples // 2
    inputs = [(f"synthetic-key-{index}", "distinct_keys") for index in range(distinct)]
    inputs += [("synthetic-shared-hotspot", "shared_hotspot")] * (samples - distinct)
    with _isolated_runtime(arguments.redis_url, pool_size=concurrency,
                           redis_cache_enabled=True, redis_cache_hmac_key=SecretStr(uuid4().hex + uuid4().hex),
                           redis_cache_lock_ttl_seconds=5) as (settings, runtime):
        disabled = _cache_mode(settings, runtime, inputs, concurrency, enabled=False)
        enabled = _cache_mode(settings, runtime, inputs, concurrency, enabled=True)
    return {"concurrency": concurrency, "paired_inputs": samples, "total_attempts": samples * 2,
            "synthetic_compute_ms": 8, "disabled": disabled, "enabled": enabled,
            "checks": {"same_input_sequence": True, "results_match": disabled["results_match"] and enabled["results_match"],
                       "disabled_computes_every_input": disabled["compute_calls"] == samples,
                       "enabled_hotspot_computed_once": enabled["groups"]["shared_hotspot"]["compute_calls"] == 1,
                       "no_unexpected_errors": disabled["unexpected_error"] == enabled["unexpected_error"] == 0,
                       "cache_available": enabled["cache_failures"] == enabled["fallback_computes"] == 0},
            "measurement_scope": "8ms synthetic read-only work and real Redis metadata; excludes models, GPU, HTTP and business databases",
            "latency_clock": "worker entry to result; excludes executor queue wait",
            "counter_semantics": "initial_misses and cache_hits overlap when a lease waiter later reads the published value",
            "group_rps": "each group's own overlapping execution window; do not sum group RPS",
            "redis_retry_boundary": "No automatic retries. A plain GET may use a short bounded read retry; never retry ambiguous EVAL or SET outcomes."}


def run_locks(arguments):
    from project_copilot.infrastructure.ranking_cache import RankingCache
    from project_copilot.schemas.retrieval import DenseIndexSpec, IndexManifest

    lease_seconds = 0.15
    with _isolated_runtime(arguments.redis_url, pool_size=2,
                           redis_cache_enabled=True, redis_cache_hmac_key=SecretStr(uuid4().hex + uuid4().hex),
                           redis_cache_lock_ttl_seconds=lease_seconds) as (settings, runtime):
        cache = RankingCache(settings, runtime=runtime)
        manifest = IndexManifest(spec=DenseIndexSpec(collection_name="pc_week1_lock_lab",
            embedding_model="synthetic-lab", dimensions=1, splitter_version="lab-v1"))
        identity = cache.identity("synthetic-lock-lab-query", manifest, pipeline={"lab": "lease-v1"})
        started = time.perf_counter()
        events = []
        def event(name):
            events.append({"event": name, "elapsed_ms": round((time.perf_counter() - started) * 1000, 6)})
        first = cache.acquire(identity)
        if first is None:
            raise RuntimeError("isolated initial cache lease unavailable")
        event("A_acquired")
        blocked = cache.acquire(identity) is None
        event("B_blocked_before_expiry")
        second = None
        deadline = time.perf_counter() + 2
        while second is None and time.perf_counter() < deadline:
            time.sleep(0.01)
            second = cache.acquire(identity)
        if second is None:
            raise RuntimeError("isolated cache lease did not expire")
        event("B_acquired_after_expiry")
        old_release_rejected = cache.release(identity, first) is False
        event("A_stale_release_rejected")
        old_publish_rejected = cache.publish(identity, first, [], negative=True) is False
        event("A_stale_publish_rejected")
        current_published = cache.publish(identity, second, [], negative=True)
        event("B_published")
        value_preserved = cache.get(identity, set(), allow_negative=True) == []
        return {"lease_seconds": lease_seconds, "events": events,
                "checks": {"B_blocked_before_expiry": blocked,
                           "B_fence_increased": second.fence > first.fence,
                           "A_cannot_release_B_lease": old_release_rejected,
                           "A_cannot_publish_under_B_lease": old_publish_rejected,
                           "B_published": current_published,
                           "B_value_read_back": value_preserved},
                "elapsed_seconds": round(time.perf_counter() - started, 6),
                "fences": {"A": first.fence, "B": second.fence},
                "implementation": "project_copilot.infrastructure.ranking_cache.RankingCache"}


def _write_report(report, output):
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(encoded, end="")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            stream.write(encoded)


def main(argv=None):
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.experiment is None:
        parser.print_help()
        return 0
    if arguments.experiment == "rate":
        total_samples = len(arguments.concurrency) * arguments.samples + (200 if arguments.with_cache else 0)
        if (any(value > 100 for value in arguments.concurrency) or arguments.samples > 500
                or total_samples > 500
                or max(arguments.concurrency) > arguments.samples or arguments.capacity > 500):
            parser.error("并发须<=100、每轮样本不少于并发、总样本<=500、容量<=500")
        if len(set(arguments.concurrency)) != len(arguments.concurrency):
            parser.error("并发档位不能重复")
    elif arguments.experiment == "cache":
        if not 100 <= arguments.samples <= 250 or arguments.concurrency > arguments.samples:
            parser.error("缓存比较每种模式须100..250样本、总样本<=500且不少于并发")
    if arguments.execute and arguments.experiment != "queue" and arguments.redis_url is None:
        parser.error("执行实验必须显式提供--redis-url隔离地址")
    if arguments.output is not None and arguments.output.exists():
        print("实验报告未保存；现有文件保持，请选择新的输出路径", file=sys.stderr)
        return 1
    report = {"format": 1, "experiment": arguments.experiment,
              "mode": "measured" if arguments.execute else "planned",
              "recorded_at": datetime.now(timezone.utc).isoformat(), "isolated_namespace": True}
    if not arguments.execute:
        report["network_accessed"] = False
        if arguments.experiment == "rate":
            report["plan"] = {"concurrency": arguments.concurrency, "samples_per_case": arguments.samples,
                              "total_samples": total_samples, "with_cache": arguments.with_cache,
                              "global_capacity": arguments.capacity}
        elif arguments.experiment == "cache":
            report["plan"] = {"concurrency": arguments.concurrency, "paired_inputs": arguments.samples,
                              "total_samples": arguments.samples * 2, "synthetic_compute_ms": 8}
        elif arguments.experiment == "queue":
            report.update(run_queue(arguments))
    else:
        try:
            report.update({"rate": run_rate, "cache": run_cache, "locks": run_locks, "queue": run_queue}[arguments.experiment](arguments))
        except Exception:
            # Do not print exceptions, connection URLs, credentials or contents.
            print("隔离实验未完成，请检查测试服务与本地配置", file=sys.stderr)
            return 1
    try:
        _write_report(report, arguments.output)
    except (OSError, ValueError):
        print("实验报告未保存；现有文件保持，请选择新的输出路径", file=sys.stderr)
        return 1
    if arguments.execute and arguments.experiment == "rate":
        failed = any(not case["tokens_not_overissued"] or case["unexpected_error"] for case in report["cases"])
        if "cache_comparison" in report:
            failed = failed or not all(report["cache_comparison"]["checks"].values())
        return int(failed)
    if arguments.execute and arguments.experiment in {"locks", "cache", "queue"}:
        return int(not all(report["checks"].values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
