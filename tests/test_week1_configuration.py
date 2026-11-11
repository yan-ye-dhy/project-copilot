"""Deployment defaults and synthetic lab entry points do not select production."""
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_lab_compose_has_no_credentials_files_business_mounts_or_persistent_volumes():
    config = yaml.safe_load((ROOT / "docker-compose.week1-lab.yml").read_text())
    assert config["name"] == "pc-week1-lab" and not config.get("volumes")
    assert set(config["services"]) == {"redis", "postgres", "qdrant"}
    for service in config["services"].values():
        assert not service.get("env_file") and not service.get("volumes")
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    assert config["services"]["postgres"]["environment"]["POSTGRES_DB"] == "project_copilot_test"
    assert config["services"]["qdrant"]["profiles"] == ["full-regression"]
    redis_image = config["services"]["redis"]["image"]
    for file in ("docker-compose.redis.yml", "deploy/public-demo.redis.yml"):
        overlay = yaml.safe_load((ROOT / file).read_text())
        assert overlay["services"]["redis"]["image"] == redis_image
        assert not overlay["services"]["redis"].get("ports")
        assert overlay["services"]["redis"]["mem_limit"] == "128m"
        assert overlay["services"]["redis"]["pids_limit"] == 64


def test_queue_cli_default_is_only_a_plan(monkeypatch, capsys):
    from scripts import week1_backend_lab as lab
    from scripts import week1_queue_lab as queue
    monkeypatch.setattr(queue, "Database", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("database opened")))
    assert lab.main(["queue"]) == 0
    output = capsys.readouterr().out
    assert '"network_accessed": false' in output and '"mode": "planned"' in output


def test_redis_lab_ignores_production_secrets_and_mode(monkeypatch):
    from scripts import week1_backend_lab as lab
    from pydantic import SecretStr
    observed = []
    monkeypatch.setenv("PROJECT_COPILOT_ENVIRONMENT", "production")
    monkeypatch.setenv("PROJECT_COPILOT_PUBLIC_DEMO", "true")
    monkeypatch.setenv("PROJECT_COPILOT_DEEPSEEK_API_KEY", "private-canary")
    fake = SimpleNamespace(ping=lambda: True, client=SimpleNamespace(scan_iter=lambda **_: []))
    monkeypatch.setattr(lab, "get_redis_runtime", lambda settings: observed.append(settings) or fake)
    monkeypatch.setattr(lab, "close_redis_runtime", lambda _: None)
    with lab._isolated_runtime(SecretStr("redis://127.0.0.1:16379/15"), pool_size=1):
        pass
    assert observed[0].environment == "test" and not observed[0].public_demo
    assert observed[0].database_url is observed[0].deepseek_api_key is observed[0].zhipu_api_key is None


def test_failure_list_stays_behind_existing_administrator_verification(monkeypatch):
    from contextlib import contextmanager
    from pydantic import SecretStr
    from scripts import public_maintenance as cli
    from project_copilot.services import queue_control
    events = []
    @contextmanager
    def transaction():
        yield object()
    database = SimpleNamespace(transaction=transaction, close=lambda: events.append("close"))
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(public_demo=True, environment="production",
        database_url=SecretStr("synthetic-unused"), public_resource_id="public-demo-v2"))
    monkeypatch.setattr(cli, "Database", lambda _: database)
    class Auth:
        def __init__(self, _): pass
        def _verify_admin(self, username, password):
            events.append("authenticate")
            if username != "synthetic-admin":
                raise PermissionError("not an administrator")
            return SimpleNamespace(id="synthetic-admin-id")
    monkeypatch.setattr(cli, "AuthService", Auth)
    monkeypatch.setattr(queue_control, "inspect_failures", lambda _: events.append("read") or [])
    import pytest
    with pytest.raises(PermissionError):
        cli.internal({"operation": "list-failures", "username": "ordinary", "password": "synthetic-only"})
    assert events == ["authenticate", "close"]
    events.clear()
    assert cli.internal({"operation": "list-failures", "username": "synthetic-admin", "password": "synthetic-only"}) == {
        "jobs": [], "automatic_replay_allowed": False}
    assert events == ["authenticate", "read", "close"]
