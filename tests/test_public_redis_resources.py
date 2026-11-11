"""Optional Redis resource checks use synthetic Compose/inspect output only."""
from copy import deepcopy
import json
import sys

import pytest
import yaml

from scripts import public_backup as backup
from scripts import public_resources as resources


def volume(name, role):
    return {"Name": name, "Driver": "local", "Options": None,
            "Labels": {resources.LABEL: resources.SCOPE, "io.project-copilot.role": role}}


def redis_config():
    base = yaml.safe_load((resources.ROOT / "docker-compose.public-demo.yml").read_text())
    overlay = yaml.safe_load((resources.ROOT / "deploy/public-demo.redis.yml").read_text())
    base["volumes"].update(overlay["volumes"])
    base["services"]["redis"] = overlay["services"]["redis"]
    base["services"]["redis"]["volumes"] = [{"type": "volume", "source": "redis_data", "target": "/data"}]
    for role in ("api", "worker"):
        base["services"][role] = deepcopy(base["services"][role])
        base["services"][role]["environment"].update(overlay["services"][role]["environment"])
        base["services"][role]["depends_on"].update(overlay["services"][role]["depends_on"])
    return base


def redis_container():
    return {"Config": {"Image": resources.REDIS_IMAGE, "Cmd": resources.REDIS_COMMAND,
                       "Labels": {resources.LABEL: resources.SCOPE,
                                  "com.docker.compose.project": resources.PROJECT,
                                  "com.docker.compose.service": "redis"}},
            "Mounts": [{"Type": "volume", "Name": resources.REDIS_VOLUMES["redis_data"], "Destination": "/data"}],
            "HostConfig": {"PortBindings": {}, "PublishAllPorts": False},
            "NetworkSettings": {"Networks": {resources.NETWORKS["backend"]: {}}, "Ports": {"6379/tcp": None}}}


def inspect_only(monkeypatch, container, *, hidden_project=False):
    calls = []
    def fake(*args):
        calls.append(args)
        if args[0] == "ps":
            return b"redis-id\n" if not hidden_project or args[-1] == "volume=pc-public-v2-redis" else b""
        assert args == ("inspect", "redis-id")
        return json.dumps([container]).encode()
    monkeypatch.setattr(resources, "docker", fake)
    monkeypatch.setattr(resources, "inspect_volume", lambda name: volume(name, "redis_data"))
    return calls


def test_overlay_keeps_base_resources_and_authoritative_backup_roles_unchanged():
    base = yaml.safe_load((resources.ROOT / "docker-compose.public-demo.yml").read_text())
    overlay = yaml.safe_load((resources.ROOT / "deploy/public-demo.redis.yml").read_text())
    assert {role: spec["name"] for role, spec in base["volumes"].items()} == resources.VOLUMES
    assert len(resources.VOLUMES) == 4
    assert set(backup.ROLES) == {"postgres_data", "qdrant_data", "manifests"}
    assert "redis_data" not in backup.ROLES
    assert set(overlay) == {"services", "volumes"}
    assert set(overlay["services"]) == {"redis", "api", "worker"}
    assert overlay["volumes"] == {"redis_data": {"name": "pc-public-v2-redis", "external": True}}
    for role in ("api", "worker"):
        assert set(overlay["services"][role]) == {"environment", "depends_on"}
        assert overlay["services"][role]["environment"] == {
            "PROJECT_COPILOT_REDIS_ENABLED": "true", "PROJECT_COPILOT_REDIS_ADMISSION_ENABLED": "true",
            "PROJECT_COPILOT_REDIS_URL": "redis://redis:6379/0"}
    assert overlay["services"]["redis"]["image"] == resources.REDIS_IMAGE
    assert overlay["services"]["redis"]["command"] == resources.REDIS_COMMAND
    assert overlay["services"]["redis"]["networks"] == ["backend"]
    assert not overlay["services"]["redis"].get("ports")


def test_default_volume_check_does_not_require_or_initialize_redis(monkeypatch):
    seen = []
    roles = {name: role for role, name in resources.VOLUMES.items()}
    def existing(name):
        seen.append(name)
        return volume(name, roles[name])
    monkeypatch.setattr(resources, "inspect_volume", existing)
    monkeypatch.setattr(resources, "docker", lambda *_: pytest.fail("default checks must not mutate"))
    resources.check_volumes()
    assert seen == [*resources.VOLUMES.values()]


def test_explicit_initialization_creates_only_missing_dedicated_redis_volume(monkeypatch):
    existing = {name: volume(name, role) for role, name in resources.VOLUMES.items()}
    created = []
    monkeypatch.setattr(resources, "inspect_volume", existing.get)
    def create(*args):
        assert args == ("volume", "create", "--label", f"{resources.LABEL}={resources.SCOPE}",
                        "--label", "io.project-copilot.role=redis_data", "pc-public-v2-redis")
        created.append(args[-1])
        existing[args[-1]] = volume(args[-1], "redis_data")
        return b"pc-public-v2-redis\n"
    monkeypatch.setattr(resources, "docker", create)
    resources.check_volumes(initialize=True, with_redis=True)
    resources.check_volumes(initialize=True, with_redis=True)
    assert created == ["pc-public-v2-redis"]


@pytest.mark.parametrize("damage", ["owner", "role", "driver", "options"])
def test_redis_volume_conflict_is_detected_before_any_initialization(monkeypatch, damage):
    info = volume("pc-public-v2-redis", "redis_data")
    if damage == "owner":
        info["Labels"][resources.LABEL] = "another-project"
    elif damage == "role":
        info["Labels"]["io.project-copilot.role"] = "postgres_data"
    elif damage == "driver":
        info["Driver"] = "remote"
    else:
        info["Options"] = {"device": "/private"}
    monkeypatch.setattr(resources, "inspect_volume", lambda name: info if name == info["Name"] else None)
    monkeypatch.setattr(resources, "docker", lambda *_: pytest.fail("must inspect all ownership before creating"))
    with pytest.raises(resources.ResourceError):
        resources.check_volumes(initialize=True, with_redis=True)


def test_explicit_redis_check_requires_initialization_instead_of_implicit_creation(monkeypatch):
    roles = {name: role for role, name in resources.VOLUMES.items()}
    monkeypatch.setattr(resources, "inspect_volume", lambda name: volume(name, roles[name]) if name in roles else None)
    monkeypatch.setattr(resources, "docker", lambda *_: pytest.fail("check is read only"))
    with pytest.raises(resources.ResourceError, match="initialization"):
        resources.check_volumes(with_redis=True)


def test_configuration_uses_overlay_only_when_explicitly_selected(monkeypatch):
    calls = []
    def fake(*args):
        calls.append(args)
        config = redis_config() if str(resources.ROOT / "deploy/public-demo.redis.yml") in args else yaml.safe_load(
            (resources.ROOT / "docker-compose.public-demo.yml").read_text())
        return json.dumps(config).encode()
    monkeypatch.setattr(resources, "docker", fake)
    resources.check_config()
    resources.check_config(with_redis=True)
    assert str(resources.ROOT / "deploy/public-demo.redis.yml") not in calls[0]
    assert str(resources.ROOT / "deploy/public-demo.redis.yml") in calls[1]


def test_compose_noninterpolated_environment_lists_are_checked_without_weakening_policy(monkeypatch):
    config = redis_config()
    for role in ("api", "worker"):
        config["services"][role]["environment"] = [f"{key}={value}" for key, value in config["services"][role]["environment"].items()]
    monkeypatch.setattr(resources, "docker", lambda *_: json.dumps(config).encode())
    resources.check_config(with_redis=True)
    config["services"]["worker"]["environment"].append("PROJECT_COPILOT_REDIS_ENABLED=false")
    with pytest.raises(resources.ResourceError, match="environment"):
        resources.check_config(with_redis=True)


@pytest.mark.parametrize("damage", ["image", "command", "scope", "network", "ports", "volume_name", "external", "mount", "url", "enabled", "admission"])
def test_optional_rendered_configuration_rejects_redis_policy_changes(monkeypatch, damage):
    config = redis_config()
    redis = config["services"]["redis"]
    if damage == "image":
        redis["image"] = "redis:latest"
    elif damage == "command":
        redis["command"] = ["redis-server"]
    elif damage == "scope":
        redis["labels"][resources.LABEL] = "wrong"
    elif damage == "network":
        redis["networks"] = ["backend", "egress"]
    elif damage == "ports":
        redis["ports"] = [{"target": 6379, "published": "6379"}]
    elif damage == "volume_name":
        config["volumes"]["redis_data"]["name"] = "old-redis"
    elif damage == "external":
        config["volumes"]["redis_data"]["external"] = False
    elif damage == "mount":
        redis["volumes"][0]["source"] = "postgres_data"
    else:
        key = {"url": "URL", "enabled": "ENABLED", "admission": "ADMISSION_ENABLED"}[damage]
        config["services"]["worker"]["environment"]["PROJECT_COPILOT_REDIS_" + key] = "wrong"
    monkeypatch.setattr(resources, "docker", lambda *_: json.dumps(config).encode())
    with pytest.raises(resources.ResourceError):
        resources.check_config(with_redis=True)


def test_rendered_environment_includes_overlay_and_preserves_database_binding(monkeypatch, tmp_path):
    path = tmp_path / "private.env"
    values = {"PROJECT_COPILOT_DATABASE_URL": "postgresql+psycopg://synthetic@postgres:5432/project_copilot_public",
              "PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD": "synthetic"}
    config = redis_config()
    for role in ("api", "worker", "migrate"):
        config["services"][role] = deepcopy(config["services"][role])
        config["services"][role]["env_file"] = [{"path": str(path)}]
        config["services"][role]["environment"]["PROJECT_COPILOT_DATABASE_URL"] = values["PROJECT_COPILOT_DATABASE_URL"]
    config["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] = "synthetic"
    calls = []
    def fake(*args):
        calls.append(args)
        return json.dumps(config).encode()
    monkeypatch.setattr(resources, "docker", fake)
    resources.validate_rendered_env(path, values, with_redis=True)
    assert len(calls) == 2 and all(str(resources.ROOT / "deploy/public-demo.redis.yml") in call for call in calls)
    config["services"]["worker"]["environment"]["PROJECT_COPILOT_REDIS_URL"] = "redis://unreviewed:6379/0"
    with pytest.raises(resources.ResourceError, match="Redis configuration"):
        resources.validate_rendered_env(path, values, with_redis=True)


@pytest.mark.parametrize("declaration", ["${PROJECT_COPILOT_PUBLIC_ENV_FILE:-.env.public-demo}",
    str(resources.ROOT / "${PROJECT_COPILOT_PUBLIC_ENV_FILE:-.env.public-demo}")])
def test_compose_240_env_file_declaration_is_checked_before_interpolation(monkeypatch, tmp_path, declaration):
    path = tmp_path / "reviewed.env"
    values = {"PROJECT_COPILOT_PUBLIC_ENV_FILE": str(path), "PROJECT_COPILOT_DATABASE_URL": "synthetic-database",
              "PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD": "synthetic-password"}
    raw = redis_config()
    rendered = deepcopy(raw)
    for role in ("api", "worker", "migrate"):
        raw["services"][role] = deepcopy(raw["services"][role])
        raw["services"][role]["env_file"] = [{"path": declaration}]
        rendered["services"][role].pop("env_file", None)
        rendered["services"][role]["environment"]["PROJECT_COPILOT_DATABASE_URL"] = values["PROJECT_COPILOT_DATABASE_URL"]
    rendered["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] = values["PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD"]
    calls = []
    def fake(*args):
        calls.append(args)
        return json.dumps(raw if "--no-interpolate" in args else rendered).encode()
    monkeypatch.setattr(resources, "docker", fake)
    monkeypatch.setenv("PROJECT_COPILOT_PUBLIC_ENV_FILE", "/untrusted/exported.env")
    resources.validate_rendered_env(path, values, with_redis=True)
    assert "--no-interpolate" in calls[0] and "--no-env-resolution" in calls[0]
    assert "--no-interpolate" not in calls[1]


@pytest.mark.parametrize("declaration", ["${OTHER_PRIVATE_FILE}", "${PROJECT_COPILOT_PUBLIC_ENV_FILE}",
    "${PROJECT_COPILOT_PUBLIC_ENV_FILE:-another.env}", "/unexpected.env"])
def test_only_exact_reviewed_env_file_expression_or_matching_literal_path_is_allowed(tmp_path, declaration):
    path = tmp_path / "reviewed.env"
    values = {"PROJECT_COPILOT_PUBLIC_ENV_FILE": str(path)}
    try:
        resolved = resources._env_file_path(declaration, values)
    except resources.ResourceError:
        return
    assert resolved != path.resolve()


def test_env_file_selection_is_still_unique_and_cannot_follow_another_private_file(monkeypatch, tmp_path):
    config = redis_config()
    for role in ("api", "worker", "migrate"):
        config["services"][role] = deepcopy(config["services"][role])
        config["services"][role]["env_file"] = [{"path": str(tmp_path / "other.env")}]
    monkeypatch.setattr(resources, "docker", lambda *_: json.dumps(config).encode())
    with pytest.raises(resources.ResourceError, match="env-file differs"):
        resources.validate_rendered_env(tmp_path / "reviewed.env", {})
    config["services"]["api"]["env_file"] = [{"path": str(tmp_path / "reviewed.env")}] * 2
    with pytest.raises(resources.ResourceError, match="env-file differs"):
        resources.validate_rendered_env(tmp_path / "reviewed.env", {})


def test_old_maintenance_and_backup_container_checks_accept_valid_optional_redis(monkeypatch):
    calls = inspect_only(monkeypatch, redis_container())
    resources.check_containers()
    assert any(call[-1] == "volume=pc-public-v2-redis" for call in calls)


@pytest.mark.parametrize("damage", ["project", "scope", "service", "image", "command", "volume", "extra_volume", "bind", "network", "no_network", "port_binding", "published_port", "publish_all"])
def test_optional_redis_container_must_match_owned_role_and_isolation(monkeypatch, damage):
    info = redis_container()
    if damage in {"project", "scope", "service"}:
        key = {"project": "com.docker.compose.project", "scope": resources.LABEL,
               "service": "com.docker.compose.service"}[damage]
        info["Config"]["Labels"][key] = "unknown"
    elif damage == "image":
        info["Config"]["Image"] = "redis:latest"
    elif damage == "command":
        info["Config"]["Cmd"] = ["redis-server"]
    elif damage == "volume":
        info["Mounts"][0]["Name"] = resources.VOLUMES["postgres_data"]
    elif damage == "extra_volume":
        info["Mounts"].append({"Type": "volume", "Name": resources.VOLUMES["postgres_data"], "Destination": "/extra"})
    elif damage == "bind":
        info["Mounts"].append({"Type": "bind", "Source": "/private", "Destination": "/app/release", "RW": False})
    elif damage in {"network", "no_network"}:
        info["NetworkSettings"]["Networks"] = {} if damage == "no_network" else {resources.NETWORKS["egress"]: {}}
    elif damage == "port_binding":
        info["HostConfig"]["PortBindings"] = {"6379/tcp": [{"HostPort": "6379"}]}
    elif damage == "published_port":
        info["NetworkSettings"]["Ports"]["6379/tcp"] = [{"HostPort": "6379"}]
    else:
        info["HostConfig"]["PublishAllPorts"] = True
    inspect_only(monkeypatch, info, hidden_project=damage == "project")
    with pytest.raises(resources.ResourceError):
        resources.check_containers()


def test_actual_redis_volume_ownership_is_checked_even_for_old_callers(monkeypatch):
    inspect_only(monkeypatch, redis_container())
    info = volume("pc-public-v2-redis", "postgres_data")
    monkeypatch.setattr(resources, "inspect_volume", lambda _: info)
    with pytest.raises(resources.ResourceError, match="ownership"):
        resources.check_containers()


def test_non_redis_service_never_gains_optional_redis_volume_permission(monkeypatch):
    info = redis_container()
    info["Config"]["Labels"]["com.docker.compose.service"] = "api"
    info["Mounts"] = [{"Type": "volume", "Name": resources.VOLUMES["manifests"], "Destination": "/app/data/manifests"},
                      {"Type": "volume", "Name": "pc-public-v2-redis", "Destination": "/redis"}]
    inspect_only(monkeypatch, info)
    with pytest.raises(resources.ResourceError, match="unexpected persistent"):
        resources.check_containers()


@pytest.mark.parametrize("with_redis", [False, True])
def test_cli_propagates_explicit_redis_choice_and_reports_only_selected_volumes(monkeypatch, capsys, with_redis):
    seen = []
    monkeypatch.setattr(sys, "argv", ["public_resources", "initialize", *(["--with-redis"] if with_redis else [])])
    monkeypatch.setattr(resources, "validate_env", lambda _: {})
    monkeypatch.setattr(resources, "host_bind_paths", lambda _: {})
    monkeypatch.setattr(resources, "check_config", lambda **kwargs: seen.append(("config", kwargs)))
    monkeypatch.setattr(resources, "validate_rendered_env", lambda *_, **kwargs: seen.append(("render", kwargs)))
    monkeypatch.setattr(resources, "check_volumes", lambda **kwargs: seen.append(("volumes", kwargs)))
    monkeypatch.setattr(resources, "check_networks", lambda **_: None)
    monkeypatch.setattr(resources, "check_containers", lambda *_: None)
    assert resources.main() == 0
    assert seen == [("config", {"with_redis": with_redis}), ("render", {"with_redis": with_redis}),
                    ("volumes", {"initialize": True, "with_redis": with_redis})]
    assert json.loads(capsys.readouterr().out)["volumes"] == resources.selected_volumes(with_redis=with_redis)
