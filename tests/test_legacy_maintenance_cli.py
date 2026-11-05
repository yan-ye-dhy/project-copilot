"""Host-only retirement wrapper with fake Docker/credentials; no real services."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from scripts import public_maintenance as cli, public_resources as resources
from project_copilot.services import public_maintenance as maintenance


JOB_ID = UUID("3f72446c-6aed-470f-85da-774004b97555")
RISK = "ACCEPT UNVERIFIED PROVIDER RISK " + str(JOB_ID)


@pytest.fixture
def host(monkeypatch, capsys):
    events, commands, prompts = [], [], []
    answers = iter(())
    for name, value in (("LEGACY_JOB_ID", JOB_ID), ("LEGACY_RISK_PHRASE", RISK)):
        if hasattr(maintenance, name):
            assert getattr(maintenance, name) == value
        else:
            monkeypatch.setattr(maintenance, name, value, raising=False)
    monkeypatch.setattr(resources, "validate_env", lambda _: {"PROJECT_COPILOT_PUBLIC_PROXY_SECRET": "synthetic_" + "a" * 40})
    for name in ("check_config", "check_volumes", "check_containers"):
        monkeypatch.setattr(resources, name, lambda: None)

    def answer(prompt):
        prompts.append(prompt)
        events.append("prompt")
        return next(answers)

    def password(_):
        events.append("password")
        return "synthetic-private-password"

    def docker(*args, input=None):
        events.append("docker")
        assert args[0] == "compose"
        payload = json.loads(input)
        commands.append((args, payload))
        assert "synthetic-private-password" not in args
        return json.dumps({"status": "ok", "result": {"job_id": str(JOB_ID), "state": "unknown"}}).encode()

    def stopped(job_id, **kwargs):
        events.append("inventory")
        assert job_id == JOB_ID
        assert events.index("password") < events.index("inventory")
        now = datetime.now(timezone.utc).isoformat()
        return {"version": 1, "job_id": str(job_id), "resource_id": "public-demo-v2",
            "captured_at": now, "postgres_volume": "pc-public-v2-postgres",
            "review_sha256": kwargs["review_sha256"], "workers": [{"container_id": "f" * 64,
                "status": "exited", "pid": 0, "running": False, "restarting": False, "finished_at": now}]}

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.getpass, "getpass", password)
    monkeypatch.setattr(resources, "docker", docker)
    monkeypatch.setattr(cli, "collect_stopped_workers", stopped)

    def run(args, replies=(), *, interactive=True):
        nonlocal answers
        answers = iter(replies)
        monkeypatch.setattr(cli.sys, "argv", ["public_maintenance", *args])
        with monkeypatch.context() as patch:
            patch.setattr(cli.sys.stdin, "isatty", lambda: interactive)
            patch.setattr(cli.sys.stdout, "isatty", lambda: interactive)
            return cli.main()

    return SimpleNamespace(run=run, events=events, commands=commands, prompts=prompts)


def files(tmp_path):
    from hashlib import sha256
    artifact = tmp_path / "review.txt"
    artifact.write_text("Synthetic local execution and business review; provider outcome is unverified.")
    artifact.chmod(0o600)
    evidence = tmp_path / "receipt.json"
    evidence.write_text(json.dumps({"provider_outcome": "unverified",
        "evidence_reference_sha256": sha256(artifact.read_bytes()).hexdigest()}))
    evidence.chmod(0o600)
    return ["--evidence", str(evidence), "--review-artifact", str(artifact)]


def test_legacy_retirement_requires_exact_risk_consent_and_fresh_stop_inventory(host, tmp_path, capsys):
    assert host.run(["retire-legacy-job", "--job-id", str(JOB_ID), "--personal-image", *files(tmp_path)],
                    [RISK, "Admin"]) == 0
    args, payload = host.commands[0]
    assert args[args.index("-f") + 1] == str(resources.ROOT / "docker-compose.public-demo.yml")
    assert str(resources.ROOT / "deploy/personal-demo.image.yml") in args
    assert args[-6:] == ("-T", "api", "python", "-m", "scripts.public_maintenance", "--internal")
    assert "--no-deps" in args and args[args.index("--pull") + 1] == "never"
    assert payload["operation"] == "retire-legacy-job" and payload["risk_acceptance"] == RISK
    assert payload["username"] == "admin"
    assert payload["worker_stop_proof"]["inventory"]["review_sha256"] == payload["evidence"]["evidence_reference_sha256"]
    assert host.events.index("password") < host.events.index("inventory") < host.events.index("docker")
    assert any("费用仍未核实" in prompt and "不会退款或重试" in prompt for prompt in host.prompts)
    assert "synthetic-private-password" not in capsys.readouterr().out


@pytest.mark.parametrize("consent", ["", "terminal_no_retry", RISK.lower(), RISK + " "])
def test_legacy_retirement_rejects_missing_or_inexact_consent(host, tmp_path, consent):
    assert host.run(["retire-legacy-job", "--job-id", str(JOB_ID), *files(tmp_path)], [consent]) == 1
    assert not host.commands and "inventory" not in host.events and "password" not in host.events


def test_legacy_retirement_rejects_other_job_before_docker_or_credentials(host):
    assert host.run(["retire-legacy-job", "--job-id", str(uuid4())]) == 1
    assert host.events == [] and host.commands == []


def test_legacy_retirement_rejects_noninteractive_host(host):
    assert host.run(["retire-legacy-job", "--job-id", str(JOB_ID)], interactive=False) == 1
    assert host.events == [] and host.commands == []


def test_inspection_uses_fixed_personal_overlay_and_private_output(host, tmp_path):
    output = tmp_path / "private" / "inspection.json"
    assert host.run(["inspect-job", "--job-id", str(JOB_ID), "--personal-image", "--output", str(output)], ["admin"]) == 0
    assert json.loads(output.read_text())["result"]["state"] == "unknown"
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    assert str(resources.ROOT / "deploy/personal-demo.image.yml") in host.commands[0][0]
    assert "inventory" not in host.events


def test_existing_resolve_prompt_remains_distinct_from_unverified_risk(host, tmp_path):
    assert host.run(["resolve-job", "--job-id", str(JOB_ID), *files(tmp_path)], [RISK]) == 1
    assert not host.commands
    assert any("输入 terminal_no_retry" in prompt for prompt in host.prompts)


def test_output_option_cannot_be_used_for_mutation(host, tmp_path):
    assert host.run(["retire-legacy-job", "--job-id", str(JOB_ID), "--output", str(tmp_path / "result.json")]) == 1
    assert not host.events and not host.commands


def test_personal_image_flag_has_no_arbitrary_compose_path(host):
    with pytest.raises(SystemExit) as error:
        host.run(["inspect-job", "--job-id", str(JOB_ID), "--personal-image", "other.yml"])
    assert error.value.code == 2 and not host.commands


def test_private_inspection_output_cannot_overwrite_or_follow_links(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    output = parent / "inspection.json"
    cli.write_private_output(output, {"status": "ok", "result": {"state": "unknown"}})
    original = output.read_bytes()
    with pytest.raises(FileExistsError):
        cli.write_private_output(output, {"changed": True})
    assert output.read_bytes() == original
    linked = parent / "link.json"
    linked.symlink_to(output)
    with pytest.raises(OSError):
        cli.write_private_output(linked, {"changed": True})
    assert output.read_bytes() == original


def test_private_inspection_output_refuses_shared_or_linked_parent(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        cli.write_private_output(shared / "out.json", {"status": "ok"})
    linked = tmp_path / "link"
    linked.symlink_to(shared, target_is_directory=True)
    with pytest.raises(ValueError):
        cli.write_private_output(linked / "new" / "out.json", {"status": "ok"})
    assert not (shared / "new").exists()


def test_internal_dispatch_requires_admin_credentials_and_carries_explicit_risk(monkeypatch):
    from pydantic import SecretStr
    actor_id = uuid4()
    session = object()
    events = []

    class FakeDatabase:
        def __init__(self, _):
            pass

        @contextmanager
        def transaction(self):
            yield session

        def close(self):
            events.append("close")

    class FakeAuth:
        def __init__(self, actual):
            assert actual is session

        def _verify_admin(self, username, password):
            assert username == "admin" and password == "synthetic-password"
            events.append("authenticate")
            return SimpleNamespace(id=actor_id)

    def retire(actual, actor, job, evidence, **kwargs):
        assert actual is session and actor == actor_id and job == JOB_ID
        assert events[-1] == "authenticate"
        if kwargs["risk_acceptance"] != RISK:
            raise ValueError("explicit risk acceptance is required")
        assert kwargs["worker_stop_proof"] == {"synthetic": True}
        return {"slot_released": True}

    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(public_demo=True, environment="production",
        public_resource_id="public-demo-v2", database_url=SecretStr("synthetic-test-dsn")))
    monkeypatch.setattr(cli, "Database", FakeDatabase)
    monkeypatch.setattr(cli, "AuthService", FakeAuth)
    monkeypatch.setattr(maintenance, "retire_legacy_unknown", retire, raising=False)
    payload = {"operation": "retire-legacy-job", "job_id": str(JOB_ID), "username": "admin",
        "password": "synthetic-password", "evidence": {}, "worker_stop_proof": {"synthetic": True}}
    with pytest.raises(ValueError):
        cli.internal(payload)
    assert cli.internal({**payload, "risk_acceptance": RISK}) == {"slot_released": True}
    assert events == ["authenticate", "close", "authenticate", "close"]
