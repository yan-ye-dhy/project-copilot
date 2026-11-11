"""Optional deployment isolation and bounded relay shutdown contract."""
from pathlib import Path
import json
import os
import subprocess
from threading import Event
from types import SimpleNamespace

from pydantic import SecretStr
import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_optional_broker_overlays_are_pinned_private_and_independently_stored():
    local = yaml.safe_load((ROOT / "docker-compose.rabbitmq.yml").read_text())
    public = yaml.safe_load((ROOT / "deploy/public-demo.rabbitmq.yml").read_text())
    lab = yaml.safe_load((ROOT / "docker-compose.messaging-lab.yml").read_text())
    image = public["services"]["rabbitmq"]["image"]
    assert "rabbitmq:4.2.9@sha256:" in image
    for config in (local, public, lab):
        rabbit = config["services"]["rabbitmq"]
        assert rabbit["image"] == image and rabbit["mem_limit"] == "512m"
        assert rabbit["pids_limit"] == 256
        assert "consumer_timeout 1800000" in rabbit["environment"]["RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS"]
    assert not local["services"]["rabbitmq"].get("ports")
    assert not public["services"]["rabbitmq"].get("ports")
    assert public["volumes"]["rabbitmq_data"] == {"name": "pc-public-v2-rabbitmq", "external": True}
    assert local["services"]["rabbitmq"]["volumes"] == ["messaging_rabbitmq:/var/lib/rabbitmq"]
    worker = local["services"]["worker"]
    assert worker["extends"] == {"file": "docker-compose.deploy.yml", "service": "migrate"}
    assert worker["command"] == ["python", "-m", "scripts.ai_worker"]
    assert worker["healthcheck"]["disable"] is True and not worker.get("ports")
    for config in (local, public):
        assert config["services"]["worker"]["stop_grace_period"] == "1260s"
        relay = config["services"]["outbox-relay"]
        assert relay["command"] == ["python", "-m", "scripts.outbox_relay"]
        assert not relay.get("ports") and not relay.get("volumes")
        assert relay["read_only"] is True and relay["stop_grace_period"] == "60s"
    assert lab["name"] == "pc-messaging-lab" and not lab.get("volumes")
    for service in lab["services"].values():
        assert not service.get("env_file") and not service.get("volumes")
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])


@pytest.mark.parametrize("gpu", [False, True], ids=["cpu", "gpu"])
def test_offline_compose_renders_a_complete_private_worker_with_shared_resources(gpu):
    """An explicitly selected Compose binary only renders synthetic config."""
    binary = os.environ.get("PROJECT_COPILOT_TEST_COMPOSE_BIN")
    if not binary:
        pytest.skip("offline Compose rendering requires an explicitly selected binary")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PROJECT_COPILOT_", "COMPOSE_"))}
    env.update(PROJECT_COPILOT_RABBITMQ_PASSWORD="synthetic-review-only",
        PROJECT_COPILOT_RABBITMQ_ERLANG_COOKIE="synthetic-review-only",
        PROJECT_COPILOT_RABBITMQ_URL="amqp://synthetic:synthetic@rabbitmq:5672/project_copilot",
        PROJECT_COPILOT_ENV_FILE=".env.example")
    command = [binary, "--env-file", str(ROOT / ".env.example"),
               "-f", str(ROOT / "docker-compose.yml"), "-f", str(ROOT / "docker-compose.deploy.yml")]
    if gpu:
        command.extend(["-f", str(ROOT / "docker-compose.gpu.yml")])
    command.extend(["-f", str(ROOT / "docker-compose.rabbitmq.yml"),
                    *(["-f", str(ROOT / "docker-compose.rabbitmq.gpu.yml")] if gpu else []),
                    "config", "--no-env-resolution", "--format", "json"])
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "synthetic offline Compose configuration failed"
    rendered = json.loads(result.stdout)
    api, worker = (rendered["services"][name] for name in ("api", "worker"))
    assert worker["image"] and worker["build"]
    migrate = rendered["services"]["migrate"]
    assert worker["image"] == api["image"] == migrate["image"]
    assert worker["build"] == api["build"] == migrate["build"]
    assert worker["command"] == ["python", "-m", "scripts.ai_worker"]
    assert worker["user"] == api["user"]
    assert worker["volumes"] == api["volumes"]
    assert worker["read_only"] is True and worker["tmpfs"]
    assert worker["healthcheck"]["disable"] is True
    assert not worker.get("ports") and worker["stop_grace_period"] == "21m0s"
    for field in ("PROJECT_COPILOT_DATABASE_URL", "PROJECT_COPILOT_QDRANT_URL",
                  "PROJECT_COPILOT_MANIFEST_DIR", "PROJECT_COPILOT_FINAL_MODEL_CACHE_DIR"):
        assert worker["environment"][field] == api["environment"][field]
    for field in ("PROJECT_COPILOT_FINAL_RERANK_DEVICE", "PROJECT_COPILOT_FINAL_RERANK_DTYPE"):
        assert worker["environment"][field] == api["environment"][field] == migrate["environment"][field]
    if gpu:
        assert worker["gpus"] == api["gpus"]
        assert worker["environment"]["PROJECT_COPILOT_FINAL_RERANK_DEVICE"] == "cuda"
    else:
        assert not worker.get("gpus")
        assert worker["environment"]["PROJECT_COPILOT_FINAL_RERANK_DEVICE"] == "cpu"
    assert worker["environment"]["PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED"] == "true"
    assert set(worker["networks"]) == {"default", "messaging_backend"}
    for dependency in ("postgres", "qdrant", "phoenix", "rabbitmq"):
        assert worker["depends_on"][dependency]["condition"] == "service_healthy"
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


def test_relay_checks_stop_between_each_publish_attempt(monkeypatch):
    from scripts import outbox_relay as cli
    stopped = Event()
    monkeypatch.setattr(cli, "_STOPPING", stopped)
    monkeypatch.setattr(cli.signal, "signal", lambda *_: None)
    config = SimpleNamespace(message_queue_enabled=True, database_url=SecretStr("synthetic-unused"),
        database_connect_timeout_seconds=1, database_pool_timeout_seconds=1, database_pool_size=1,
        database_max_overflow=0, outbox_relay_batch_size=100, outbox_lease_seconds=60,
        outbox_retry_base_seconds=5, outbox_retry_max_seconds=300)
    monkeypatch.setattr(cli, "load_settings", lambda: config)
    events = []
    monkeypatch.setattr(cli, "Database", lambda *_a, **_k: SimpleNamespace(close=lambda: events.append("closed")))
    class Publisher:
        def __init__(self, _): pass
        def __enter__(self): return self
        def __exit__(self, *_): events.append("publisher-closed")
        def pump(self): events.append("pump")
    monkeypatch.setattr(cli, "RabbitPublisher", Publisher)
    def publish(*_a, **_k):
        events.append("attempt")
        stopped.set()
        return True
    monkeypatch.setattr(cli, "relay_once", publish)
    assert cli.main(["--once"]) == 0
    assert events == ["attempt", "pump", "publisher-closed", "closed"]
