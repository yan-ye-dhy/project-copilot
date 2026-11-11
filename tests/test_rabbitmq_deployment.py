"""Optional deployment isolation and bounded relay shutdown contract."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from pydantic import SecretStr
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
    for config in (local, public):
        relay = config["services"]["outbox-relay"]
        assert relay["command"] == ["python", "-m", "scripts.outbox_relay"]
        assert not relay.get("ports") and not relay.get("volumes")
        assert relay["read_only"] is True and relay["stop_grace_period"] == "60s"
    assert lab["name"] == "pc-messaging-lab" and not lab.get("volumes")
    for service in lab["services"].values():
        assert not service.get("env_file") and not service.get("volumes")
        assert all(port.startswith("127.0.0.1:") for port in service["ports"])


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
