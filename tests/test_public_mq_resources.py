"""Synthetic Compose/inspect assertions; never mutate an actual Docker resource."""
from copy import deepcopy
import json
import pytest
import yaml
from scripts import public_resources as resources
from scripts import public_backup


def config():
    base = yaml.safe_load((resources.ROOT / "docker-compose.public-demo.yml").read_text())
    overlay = yaml.safe_load((resources.ROOT / "deploy/public-demo.rabbitmq.yml").read_text())
    base["volumes"].update(overlay["volumes"])
    for role, service in overlay["services"].items():
        if role in base["services"]:
            base["services"][role]["environment"].update(service["environment"])
        else:
            base["services"][role] = deepcopy(service)
    base["services"]["rabbitmq"]["volumes"] = [{"type": "volume", "source": "rabbitmq_data", "target": "/var/lib/rabbitmq"}]
    return base


def test_messaging_has_separate_resources_and_keeps_authoritative_backup_roles():
    assert len(resources.VOLUMES) == 4
    assert "rabbitmq_data" not in public_backup.ROLES
    assert resources.selected_volumes() == resources.VOLUMES
    assert resources.selected_volumes(with_rabbitmq=True)["rabbitmq_data"] == "pc-public-v2-rabbitmq"
    assert str(resources.ROOT / "deploy/public-demo.rabbitmq.yml") not in resources.compose_files()
    assert str(resources.ROOT / "deploy/public-demo.rabbitmq.yml") in resources.compose_files(with_rabbitmq=True)


def test_optional_messaging_compose_passes_review(monkeypatch):
    monkeypatch.setattr(resources, "docker", lambda *_: json.dumps(config()).encode())
    resources.check_config(with_rabbitmq=True)


@pytest.mark.parametrize("damage", ["image", "ports", "network", "volume", "relay_command", "relay_mount", "relay_network", "enabled"])
def test_modified_messaging_boundary_rejected(damage):
    active = config()
    broker, relay = active["services"]["rabbitmq"], active["services"]["outbox-relay"]
    if damage == "image": broker["image"] = "rabbitmq:latest"
    elif damage == "ports": broker["ports"] = ["5672:5672"]
    elif damage == "network": broker["networks"] = ["egress"]
    elif damage == "volume": broker["volumes"][0]["source"] = "postgres_data"
    elif damage == "relay_command": relay["command"] = ["python", "-m", "scripts.ai_worker"]
    elif damage == "relay_mount": relay["volumes"] = ["manifests:/app/data/manifests"]
    elif damage == "relay_network": relay["networks"] = ["backend", "egress"]
    else: active["services"]["worker"]["environment"]["PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED"] = "false"
    with pytest.raises(resources.ResourceError):
        resources._check_rabbitmq_config(active)


def test_rendered_broker_url_and_credentials_must_match_reviewed_private_configuration():
    active = config()
    values = {"PROJECT_COPILOT_RABBITMQ_PASSWORD": "synthetic-password-canary-" * 2,
              "PROJECT_COPILOT_RABBITMQ_ERLANG_COOKIE": "synthetic-cookie-canary-" * 2}
    target = "amqp://project_copilot_mq:" + values["PROJECT_COPILOT_RABBITMQ_PASSWORD"] + "@rabbitmq:5672/project_copilot"
    values["PROJECT_COPILOT_RABBITMQ_URL"] = target
    active["services"]["rabbitmq"]["environment"].update({
        "RABBITMQ_DEFAULT_PASS": values["PROJECT_COPILOT_RABBITMQ_PASSWORD"],
        "RABBITMQ_ERLANG_COOKIE": values["PROJECT_COPILOT_RABBITMQ_ERLANG_COOKIE"]})
    for role in ("api", "worker", "outbox-relay"):
        active["services"][role]["environment"]["PROJECT_COPILOT_RABBITMQ_URL"] = target
    resources._check_rabbitmq_config(active, values=values)
    values["PROJECT_COPILOT_RABBITMQ_URL"] = target.replace("@rabbitmq:", "@other-project:")
    with pytest.raises(resources.ResourceError) as error:
        resources._check_rabbitmq_config(active, values=values)
    assert "canary" not in str(error.value)


def test_initialize_only_creates_new_labelled_mq_volume(monkeypatch):
    def info(name, role):
        return {"Name": name, "Driver": "local", "Options": None,
                "Labels": {resources.LABEL: resources.SCOPE, "io.project-copilot.role": role}}
    volumes = {name: info(name, role) for role, name in resources.VOLUMES.items()}
    created = []
    monkeypatch.setattr(resources, "inspect_volume", volumes.get)
    def create(*args):
        assert args == ("volume", "create", "--label", f"{resources.LABEL}={resources.SCOPE}",
                        "--label", "io.project-copilot.role=rabbitmq_data", "pc-public-v2-rabbitmq")
        created.append(args[-1]); volumes[args[-1]] = info(args[-1], "rabbitmq_data")
        return b""
    monkeypatch.setattr(resources, "docker", create)
    resources.check_volumes(initialize=True, with_rabbitmq=True)
    resources.check_volumes(initialize=True, with_rabbitmq=True)
    assert created == ["pc-public-v2-rabbitmq"]
