"""Synthetic read-only command/metadata tests; no daemon, DB or cloud contact."""
import ast
import json
import os
from pathlib import Path
import subprocess

import pytest

from scripts import staging_preflight as probe


def make_local(tmp_path):
    for rel in (*probe.ENTRY_SOURCES, "scripts/build_edgeone.mjs"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic only")
    evidence = tmp_path / "data/release-evidence"
    evidence.mkdir(parents=True, mode=0o700)
    return evidence


def git_runner(args, **kwargs):
    assert args[0] == "git"
    return "a" * 40 if args[1] == "rev-parse" else ""


def keyed(checks):
    return {row["check"]: row for row in checks}


def test_local_collects_hashes_and_private_readability_without_loading_env(tmp_path):
    evidence = make_local(tmp_path)
    (tmp_path / ".env.public-demo").write_text("secret-sentinel-do-not-load")
    artifact = evidence / "private.json"
    artifact.write_text("secret-sentinel-do-not-print")
    artifact.chmod(0o600)
    checks = keyed(probe.local_checks(tmp_path, artifacts=[artifact], runner=git_runner))
    assert checks["git"]["clean"] is True
    assert checks["evidence_directory_private"]["status"] == "PASS"
    assert checks["artifact_readable_0"]["status"] == "PASS"
    assert len(checks["entry_sha256_0"]["sha256"]) == 64
    assert len([row for row in checks.values() if row["check"].startswith("entry_sha256_")]) == 7
    output = json.dumps(checks)
    assert "secret-sentinel" not in output
    assert str(artifact) not in output
    assert checks["cloud_and_evidence_review"]["status"] == "NEEDS_MANUAL_CHECK"
    assert checks["capacity_inventory"]["status"] == "NEEDS_MANUAL_CHECK"


def test_git_dirty_does_not_print_filenames_or_contents(tmp_path):
    make_local(tmp_path)
    def dirty(args, **_):
        return "a" * 40 if args[1] == "rev-parse" else " M super-secret-name\n"
    checks = keyed(probe.local_checks(tmp_path, runner=dirty))
    assert checks["git"]["status"] == "FAIL"
    assert "super-secret" not in json.dumps(checks)


def test_inventory_entry_sources_match_release_gate_without_importing_business():
    tree = ast.parse((probe.ROOT / "src/project_copilot/services/entry_protection.py").read_text())
    declaration = next(node for node in tree.body if isinstance(node, ast.Assign) and
                       any(isinstance(target, ast.Name) and target.id == "ENTRY_SOURCES" for target in node.targets))
    assert probe.ENTRY_SOURCES == ast.literal_eval(declaration.value)


def test_private_directory_requires_owner_search_permission(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o600)
    try:
        assert probe.private_path_check(private, directory=True) == "FAIL"
    finally:
        private.chmod(0o700)


@pytest.mark.parametrize("kind", ["world_readable", "symlink", "parent_symlink", "missing"])
def test_private_path_fails_or_needs_review(tmp_path, kind):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    path = parent / "evidence.json"
    path.write_text("not approval")
    path.chmod(0o600)
    if kind == "world_readable":
        path.chmod(0o644)
    elif kind == "symlink":
        target = parent / "link.json"
        target.symlink_to(path)
        path = target
    elif kind == "parent_symlink":
        target = tmp_path / "link"
        target.symlink_to(parent, target_is_directory=True)
        path = target / "evidence.json"
    else:
        path = parent / "absent"
    assert probe.private_path_check(path) == ("NEEDS_MANUAL_CHECK" if kind == "missing" else "FAIL")


def docker_fixture(tmp_path, *, worker=False, closed=True, port=False, wrong_volume=False, migration_exit=0, migration_status="exited", worker_status=None):
    cache = tmp_path / "models--BAAI--bge-reranker-v2-m3/snapshots/fake"
    cache.mkdir(parents=True)
    (cache / "config.json").write_text("{}")
    records = {}
    for index, service in enumerate(["postgres", "qdrant", "phoenix", "api", "ingress", "migrate", "worker"]):
        cid = f"{index + 1:012x}"
        mounts = []
        if service in probe.MOUNTS:
            target, name = probe.MOUNTS[service]
            mounts.append({"Destination": target, "Type": "volume", "Name": "old-volume" if wrong_volume else name})
        if service == "api":
            mounts.append({"Destination": "/app/models/huggingface", "Type": "bind", "RW": False, "Source": str(tmp_path)})
        records[cid] = {"service": service, "project": probe.PROJECT, "running": worker if service == "worker" else service != "migrate",
                        "status": migration_status if service == "migrate" else (worker_status or ("running" if worker else "exited")) if service == "worker" else "running",
                        "health": "unhealthy" if service in {"api", "ingress"} else "healthy", "exit_code": migration_exit if service == "migrate" else 0,
                        "mounts": mounts, "ports": {"8002/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8002"}]} if port and service == "api" else {}}
    calls = []
    def runner(args, **_):
        calls.append(args)
        if args[:3] == ["docker", "ps", "-aq"]:
            return "\n".join(records)
        if args[:2] == ["docker", "inspect"]:
            assert args[3] == probe.INSPECT_FORMAT
            assert ".Config.Env" not in args[3] and ".State.Health.Log" not in args[3]
            return json.dumps(records[args[-1]])
        if args[:3] == ["docker", "volume", "inspect"]:
            role = next(role for role, name in probe.VOLUMES.items() if name == args[-1])
            return json.dumps({"name": args[-1], "scope": probe.SCOPE, "role": role, "driver": "local", "options": {}})
        if args[:2] == ["docker", "exec"]:
            assert args[2] in records and records[args[2]]["service"] == "postgres"
            assert args[-1] == probe.STATE_SQL
            assert "BEGIN TRANSACTION READ ONLY" in args[-1]
            assert "ROLLBACK" in args[-1] and "UPDATE" not in args[-1]
            return json.dumps({"state": "CLOSED" if closed else "STAGING", "worker_enabled": False})
        pytest.fail(f"unexpected subprocess: {args[:2]}")
    return runner, calls


def test_docker_reads_fixed_scope_only_and_expected_unhealthy_not_bypassed(tmp_path):
    runner, calls = docker_fixture(tmp_path)
    checks = keyed(probe.docker_checks(runner))
    assert checks["closed_worker_disabled"]["status"] == "PASS"
    assert checks["worker_tunnel_stopped"]["status"] == "PASS"
    assert checks["dedicated_volume_labels_and_mounts"]["status"] == "PASS"
    assert checks["no_project_host_ports"]["status"] == "PASS"
    assert checks["migration_exit"]["status"] == "PASS"
    assert checks["readonly_model_cache_files"]["status"] == "PASS"
    assert checks["container_inventory"]["containers"][3]["health"] == "unhealthy"
    assert checks["host_tunnel_and_listener_review"]["status"] == "NEEDS_MANUAL_CHECK"
    assert all(call[0] == "docker" and call[1] in {"ps", "inspect", "volume", "exec"} for call in calls)
    assert ".env" not in json.dumps(calls)
    assert str(tmp_path) not in json.dumps(checks)


@pytest.mark.parametrize("variant,check", [("worker", "worker_tunnel_stopped"), ("closed", "closed_worker_disabled"),
                                          ("port", "no_project_host_ports"), ("wrong_volume", "dedicated_volume_labels_and_mounts")])
def test_detects_unsafe_live_state_without_mutating(tmp_path, variant, check):
    kwargs = {variant: False if variant == "closed" else True}
    runner, calls = docker_fixture(tmp_path, **kwargs)
    result = keyed(probe.docker_checks(runner))
    assert result[check]["status"] == "FAIL"
    if variant == "wrong_volume":
        assert not any(c[1] == "exec" for c in calls)


def test_failed_migration_is_not_accepted(tmp_path):
    runner, _ = docker_fixture(tmp_path, migration_exit=1)
    assert keyed(probe.docker_checks(runner))["migration_exit"]["status"] != "PASS"


def test_created_migration_with_zero_exit_never_counts_as_run(tmp_path):
    runner, _ = docker_fixture(tmp_path, migration_status="created")
    assert keyed(probe.docker_checks(runner))["migration_exit"]["status"] != "PASS"


@pytest.mark.parametrize("status", ["paused", "restarting", "dead", "removing"])
def test_worker_not_considered_stopped_in_unsafe_states(tmp_path, status):
    runner, _ = docker_fixture(tmp_path, worker_status=status)
    assert keyed(probe.docker_checks(runner))["worker_tunnel_stopped"]["status"] == "FAIL"


@pytest.mark.parametrize("field,value", [("running", None), ("running", "false"), ("status", None),
                                         ("status", "invented"), ("exit_code", None), ("exit_code", False)])
def test_missing_or_invalid_container_state_metadata_never_means_stopped(field, value):
    info = {"service": "worker", "project": probe.PROJECT, "running": False,
            "status": "exited", "exit_code": 0, "health": None}
    info[field] = value
    with pytest.raises(probe.ProbeUnavailable):
        probe.container_summary(info)


def test_daemon_failure_redacted_and_not_pass():
    def unavailable(*_, **__):
        raise probe.ProbeUnavailable("password-and-tunnel-must-not-print")
    checks = probe.docker_checks(unavailable)
    assert keyed(checks)["docker_inventory_incomplete"]["status"] == "NEEDS_MANUAL_CHECK"
    assert "password-and-tunnel" not in json.dumps(checks)


def test_unexpected_daemon_payload_is_not_a_pass_or_traceback():
    def malformed(args, **_):
        return "123456789abc" if args[1] == "ps" else '"sensitive-unexpected-string"'
    checks = probe.docker_checks(malformed)
    assert keyed(checks)["docker_inventory_incomplete"]["status"] == "NEEDS_MANUAL_CHECK"
    assert "sensitive-unexpected" not in json.dumps(checks)


def test_subprocess_no_shell_sanitized_env_timeout_and_suppressed_output(monkeypatch):
    monkeypatch.setenv("PROJECT_COPILOT_PROXY_SECRET", "do-not-pass")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "development")
    def fake(args, **kwargs):
        assert kwargs["timeout"] == 15 and kwargs.get("shell") is None
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert not any(k.startswith(("PROJECT_COPILOT_", "COMPOSE_")) for k in kwargs["env"])
        return subprocess.CompletedProcess(args, 1, b"secret-value", b"another-secret")
    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(probe.ProbeUnavailable) as error:
        probe.run_readonly(["git", "status"])
    assert not str(error.value)


def test_default_cli_does_not_invoke_docker_or_emit_authorization(monkeypatch, capsys):
    monkeypatch.setattr(probe, "local_checks", lambda **_: [probe.item("local", "PASS")])
    monkeypatch.setattr(probe, "docker_checks", lambda: pytest.fail("default invoked Docker"))
    assert probe.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "read_only_closed_inventory"
    assert output["staging_authorization"] == "NOT_EVALUATED"
    assert keyed(output["checks"])["docker_checks_not_requested"]["status"] == "NEEDS_MANUAL_CHECK"
    assert "format" not in output and "checks" not in output["checks"][0]
