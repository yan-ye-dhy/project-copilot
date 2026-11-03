"""Public physical names never follow Compose project overrides or old mounts."""
import json
from pathlib import Path

import pytest
import yaml

from scripts import public_resources as resources


def volume(name, role, **extra):
    return {"Name": name, "Driver": "local", "Options": None,
            "Labels": {resources.LABEL: resources.SCOPE, "io.project-copilot.role": role}, **extra}


def test_public_physical_volumes_are_fixed_external_names():
    config = yaml.safe_load((resources.ROOT / "docker-compose.public-demo.yml").read_text())
    assert {role: item["name"] for role, item in config["volumes"].items()} == resources.VOLUMES
    assert all(item["external"] for item in config["volumes"].values())
    assert all("${" not in item["name"] for item in config["volumes"].values())
    assert {role: item["name"] for role, item in config["networks"].items()} == resources.NETWORKS
    assert all(item["external"] for item in config["networks"].values())


def test_unowned_or_remote_volume_is_never_adopted():
    role, name = next(iter(resources.VOLUMES.items()))
    with pytest.raises(resources.ResourceError):
        resources.validate_volume(volume(name, role, Labels={}), name, role)
    with pytest.raises(resources.ResourceError):
        resources.validate_volume(volume(name, role, Options={"device": "/private"}), name, role)


def test_initialization_checks_all_ownership_before_creating_anything(monkeypatch):
    names = list(resources.VOLUMES.values())
    monkeypatch.setattr(resources, "inspect_volume", lambda name: None if name != names[-1] else volume(name, "manifests", Labels={}))
    monkeypatch.setattr(resources, "docker", lambda *args, **kwargs: pytest.fail("must not mutate"))
    with pytest.raises(resources.ResourceError):
        resources.check_volumes(initialize=True)


def test_env_refuses_original_database_and_project_override(tmp_path, monkeypatch):
    path = tmp_path / "private.env"
    path.write_text("PROJECT_COPILOT_DATABASE_URL=postgresql+psycopg://project_copilot_public:synthetic@postgres:5432/project_copilot_public\nPROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD=synthetic\n")
    path.chmod(0o600)
    assert resources.validate_env(path)
    monkeypatch.setenv("PROJECT_COPILOT_DATABASE_URL", "postgresql+psycopg://old:old@localhost:5433/old")
    with pytest.raises(resources.ResourceError):
        resources.validate_env(path)
    monkeypatch.delenv("PROJECT_COPILOT_DATABASE_URL")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "project-copilot")
    with pytest.raises(resources.ResourceError):
        resources.validate_env(path)
    monkeypatch.delenv("COMPOSE_PROJECT_NAME")
    path.write_text(path.read_text() + "PROJECT_COPILOT_PUBLIC_ENV_FILE=${UNTRUSTED_PATH}\n")
    with pytest.raises(resources.ResourceError):
        resources.validate_env(path)
    path.write_text(path.read_text().split("PROJECT_COPILOT_PUBLIC_ENV_FILE=")[0])
    path.write_text(path.read_text().replace("@postgres:5432", "@127.0.0.1:5433"))
    with pytest.raises(resources.ResourceError):
        resources.validate_env(path)


def test_actual_container_cannot_mount_original_volume(monkeypatch):
    def docker(*args):
        if args[0] == "ps":
            return b"synthetic-container\n"
        return json.dumps([{"Config": {"Labels": {"com.docker.compose.service": "postgres", "com.docker.compose.project": resources.PROJECT}},
                            "Mounts": [{"Destination": "/var/lib/postgresql/data", "Type": "volume", "Name": "project-copilot_postgres_data"}]}]).encode()
    monkeypatch.setattr(resources, "docker", docker)
    with pytest.raises(resources.ResourceError):
        resources.check_containers()


def test_docker_drops_exported_compose_and_application_overrides(monkeypatch):
    monkeypatch.setenv("PROJECT_COPILOT_DATABASE_URL", "original-database")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "project-copilot")
    seen = {}
    def run(command, **kwargs):
        seen.update(kwargs["env"])
        return type("Result", (), {"returncode": 0, "stdout": b"ok"})()
    monkeypatch.setattr(resources.subprocess, "run", run)
    assert resources.docker("compose", "config") == b"ok"
    assert "PROJECT_COPILOT_DATABASE_URL" not in seen and "COMPOSE_PROJECT_NAME" not in seen


def test_network_initialization_rejects_existing_unowned_network(monkeypatch):
    def docker(*args):
        if args[1] == "ls":
            return b"pc-public-v2-backend\n"
        if args[1] == "inspect":
            return json.dumps([{"Labels": {}, "Driver": "bridge", "Internal": True}]).encode()
        pytest.fail("must not create after detecting conflicting existing network")
    monkeypatch.setattr(resources, "docker", docker)
    with pytest.raises(resources.ResourceError):
        resources.check_networks(initialize=True)


def test_evidence_directory_must_be_private_and_specific(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    evidence = tmp_path / "release"
    evidence.mkdir(mode=0o700)
    values = {"PROJECT_COPILOT_MODEL_CACHE_HOST_DIR": str(models),
              "PROJECT_COPILOT_RELEASE_EVIDENCE_HOST_DIR": str(evidence)}
    assert resources.host_bind_paths(values)["/app/release"] == evidence.resolve()
    evidence.chmod(0o755)
    with pytest.raises(resources.ResourceError):
        resources.host_bind_paths(values)
    values["PROJECT_COPILOT_RELEASE_EVIDENCE_HOST_DIR"] = str(resources.ROOT / "data")
    with pytest.raises(resources.ResourceError):
        resources.host_bind_paths(values)


def test_private_env_cannot_override_compose_project(tmp_path):
    path = tmp_path / "private.env"
    path.write_text("COMPOSE_PROJECT_NAME=project-copilot\n")
    path.chmod(0o600)
    with pytest.raises(resources.ResourceError, match="Compose selectors"):
        resources.validate_env(path)


def test_actual_rendered_project_must_match_preflight(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, "docker", lambda *args: json.dumps({"name": "project-copilot"}).encode())
    with pytest.raises(resources.ResourceError, match="rendered project"):
        resources.validate_rendered_env(tmp_path / "private.env", {})
