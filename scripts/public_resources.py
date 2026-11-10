"""Inspect/initialize dedicated public-demo resources without adopting old volumes.

Only creates missing, fixed v2 volumes. Never removes, migrates or overwrites a
volume. The deployment name is intentionally independent of Compose project.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "public-demo-v2"
LABEL = "io.project-copilot.scope"
PROJECT = "project-copilot-public-demo"
VOLUMES = {"postgres_data": "pc-public-v2-postgres", "qdrant_data": "pc-public-v2-qdrant",
           "phoenix_data": "pc-public-v2-phoenix", "manifests": "pc-public-v2-manifests"}
# Redis is optional acceleration/admission state, outside the authoritative
# backup roles imported by public_backup and existing maintenance commands.
REDIS_VOLUMES = {"redis_data": "pc-public-v2-redis"}
REDIS_IMAGE = "redis:8.2.3-alpine@sha256:08ad0b1d280850169a790dba1393ff7a90aef951fc19632cf4d3ce4f78e679ba"
REDIS_COMMAND = ["redis-server", "--appendonly", "yes", "--appendfsync", "always", "--maxmemory", "64mb",
                 "--maxmemory-policy", "noeviction", "--save", ""]
NETWORKS = {"backend": "pc-public-v2-backend", "egress": "pc-public-v2-egress",
            "ingress": "pc-public-v2-ingress"}
MOUNTS = {"postgres": {"/var/lib/postgresql/data": VOLUMES["postgres_data"]},
          "qdrant": {"/qdrant/storage": VOLUMES["qdrant_data"]},
          "phoenix": {"/data": VOLUMES["phoenix_data"]},
          "api": {"/app/data/manifests": VOLUMES["manifests"]},
          "worker": {"/app/data/manifests": VOLUMES["manifests"]},
          "migrate": {"/app/data/manifests": VOLUMES["manifests"]}}


class ResourceError(RuntimeError):
    pass


def docker(*args, input=None):
    # Compose interpolation normally prefers exported shell values over the
    # selected env-file. Never let stale development DSNs or project/network
    # selectors override the reviewed private file.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PROJECT_COPILOT_", "COMPOSE_"))}
    result = subprocess.run(["docker", *args], input=input, capture_output=True, check=False, env=env)
    if result.returncode:
        # Daemon errors can contain expanded config or env; never relay them.
        raise ResourceError("Docker operation failed; inspect private local diagnostics")
    return result.stdout


def inspect_volume(name):
    # Listing first distinguishes absence from daemon failure without parsing
    # translated stderr or accidentally adopting a differently labelled volume.
    names = docker("volume", "ls", "--format", "{{.Name}}").decode().splitlines()
    return json.loads(docker("volume", "inspect", name))[0] if name in names else None


def validate_volume(info, name, role):
    labels = info.get("Labels") or {}
    if info.get("Name") != name or labels.get(LABEL) != SCOPE or labels.get("io.project-copilot.role") != role:
        raise ResourceError("volume ownership mismatch; refusing adoption or mutation")
    if info.get("Driver") != "local" or info.get("Options"):
        raise ResourceError("only ordinary local dedicated volumes are supported")


def selected_volumes(*, with_redis=False):
    return {**VOLUMES, **(REDIS_VOLUMES if with_redis else {})}


def compose_files(*, with_redis=False):
    files = ("-f", str(ROOT / "docker-compose.public-demo.yml"))
    return files + (("-f", str(ROOT / "deploy/public-demo.redis.yml")) if with_redis else ())


def check_volumes(*, initialize=False, with_redis=False):
    volumes = selected_volumes(with_redis=with_redis)
    existing = {}
    for role, name in volumes.items():
        info = inspect_volume(name)
        if info is not None:
            validate_volume(info, name, role)
        existing[role] = info
    for role, name in volumes.items():
        info = existing[role]
        if info is None:
            if not initialize:
                raise ResourceError("dedicated volume missing; explicit initialization required")
            docker("volume", "create", "--label", f"{LABEL}={SCOPE}",
                   "--label", f"io.project-copilot.role={role}", name)
            info = inspect_volume(name)
        validate_volume(info, name, role)


def validate_env(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ResourceError("environment file must be private regular file (chmod 600)")
    values = dotenv_values(path, interpolate=False)
    if any(key.startswith("COMPOSE_") for key in values):
        raise ResourceError("Compose selectors are forbidden in the private application settings file")
    if any(value is not None and "$" in value for value in values.values()):
        raise ResourceError("Compose interpolation expressions are not allowed in private settings")
    try:
        url = make_url(values.get("PROJECT_COPILOT_DATABASE_URL") or "")
    except Exception:
        raise ResourceError("invalid isolated database URL") from None
    if (url.drivername != "postgresql+psycopg" or url.host != "postgres" or url.port != 5432 or
            url.username != "project_copilot_public" or url.database != "project_copilot_public" or url.query or
            not url.password or url.password != values.get("PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD")):
        raise ResourceError("database URL must target only the dedicated postgres service")
    selected = os.environ.get("PROJECT_COPILOT_PUBLIC_ENV_FILE")
    if os.environ.get("COMPOSE_PROJECT_NAME", PROJECT) != PROJECT or (selected is not None and Path(selected).resolve() != path.resolve()):
        raise ResourceError("unexpected project/environment override")
    for key, value in values.items():
        if key in os.environ and os.environ[key] != (value or ""):
            raise ResourceError("exported application settings conflict with private environment file")
    return values


def host_bind_paths(values):
    result = {}
    for key, target, fallback in (
        ("PROJECT_COPILOT_MODEL_CACHE_HOST_DIR", "/app/models/huggingface", "models/huggingface"),
        ("PROJECT_COPILOT_RELEASE_EVIDENCE_HOST_DIR", "/app/release", "data/release-evidence"),
    ):
        source = Path(values.get(key) or ROOT / fallback)
        if not source.is_absolute():
            source = ROOT / source
        resolved = source.resolve()
        if not resolved.is_dir():
            raise ResourceError("reviewed read-only host directory is missing")
        if target == "/app/release" and (resolved in {Path('/'), ROOT, ROOT.parent, ROOT / 'data', ROOT / 'models'} or resolved.stat().st_mode & 0o077):
            raise ResourceError("release evidence must be a dedicated private directory (mode 700)")
        result[target] = resolved
    return result


def validate_rendered_env(path, values, *, with_redis=False):
    command = ("compose", "--env-file", str(path), *compose_files(with_redis=with_redis), "config")
    # Compose 2.40 drops env_file after interpolation even with
    # --no-env-resolution. Preserve its declaration before inspecting it.
    raw = json.loads(docker(*command, "--no-interpolate", "--no-env-resolution", "--format", "json"))
    rendered = json.loads(docker(*command, "--format", "json"))
    if rendered.get("name") != PROJECT or raw.get("name") != PROJECT:
        raise ResourceError("rendered project differs from dedicated public deployment")
    for role in ("api", "worker", "migrate"):
        files = raw["services"][role].get("env_file", [])
        if len(files) != 1 or _env_file_path(files[0]["path"], values) != Path(path).resolve():
            raise ResourceError("rendered application env-file differs from reviewed private file")
        if rendered["services"][role]["environment"].get("PROJECT_COPILOT_DATABASE_URL") != values["PROJECT_COPILOT_DATABASE_URL"]:
            raise ResourceError("rendered database target differs from reviewed private file")
    if rendered["services"]["postgres"]["environment"].get("POSTGRES_PASSWORD") != values["PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD"]:
        raise ResourceError("rendered datastore credentials differ")
    if with_redis:
        _check_redis_config(rendered)


def _env_file_path(declaration, values):
    # Only the base deployment's exact expression may select the file. Never
    # interpolate arbitrary variables or consult exported process credentials.
    expression = "${PROJECT_COPILOT_PUBLIC_ENV_FILE:-.env.public-demo}"
    if not isinstance(declaration, str):
        raise ResourceError("invalid application env-file declaration")
    if declaration in {expression, str(ROOT / expression)}:
        declaration = values.get("PROJECT_COPILOT_PUBLIC_ENV_FILE") or ".env.public-demo"
    if not isinstance(declaration, str) or "$" in declaration:
        raise ResourceError("unsupported application env-file expression")
    selected = Path(declaration)
    return (selected if selected.is_absolute() else ROOT / selected).resolve()


def check_networks(*, initialize=False):
    listed = docker("network", "ls", "--format", "{{.Name}}").decode().splitlines()
    existing = {}
    for role, name in NETWORKS.items():
        info = json.loads(docker("network", "inspect", name))[0] if name in listed else None
        if info is not None:
            if ((info.get("Labels") or {}).get(LABEL) != SCOPE or
                    (info.get("Labels") or {}).get("io.project-copilot.role") != role or
                    info.get("Driver") != "bridge" or bool(info.get("Internal")) != (role == "backend")):
                raise ResourceError("dedicated network ownership or isolation mismatch")
            for cid in info.get("Containers", {}):
                consumer = json.loads(docker("inspect", cid))[0]
                if (consumer.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") != PROJECT:
                    raise ResourceError("unexpected project shares dedicated network")
        existing[role] = info
    for role, name in NETWORKS.items():
        if existing[role] is None:
            if not initialize:
                raise ResourceError("dedicated network missing; initialization required")
            options = ["--internal"] if role == "backend" else []
            docker("network", "create", *options, "--label", f"{LABEL}={SCOPE}",
                   "--label", f"io.project-copilot.role={role}", name)


def service_networks(service):
    return ({"backend"} if service in {"postgres", "qdrant", "phoenix", "redis"}
            else {"ingress"} if service == "cloudflared"
            else {"backend", "ingress"} if service == "ingress"
            else {"backend", "egress"})


def _check_redis_config(config):
    volume = config["volumes"].get("redis_data", {})
    redis = config["services"].get("redis", {})
    if volume.get("name") != REDIS_VOLUMES["redis_data"] or not volume.get("external"):
        raise ResourceError("optional Redis must use its dedicated external volume")
    if (redis.get("image") != REDIS_IMAGE or redis.get("command") != REDIS_COMMAND
            or (redis.get("labels") or {}).get(LABEL) != SCOPE
            or set(redis.get("networks", {})) != {"backend"} or redis.get("ports")):
        raise ResourceError("optional Redis image, role or isolation differs")
    mounts = redis.get("volumes", [])
    if (len(mounts) != 1 or mounts[0].get("type") != "volume"
            or mounts[0].get("source") != "redis_data" or mounts[0].get("target") != "/data"):
        raise ResourceError("optional Redis storage role differs")
    for role in ("api", "worker"):
        environment = config["services"][role].get("environment", {})
        # The noninterpolated JSON form in Compose 2.40 can retain KEY=value
        # lists rather than the mapping emitted after interpolation.
        if isinstance(environment, list):
            pairs = [item.split("=", 1) for item in environment if isinstance(item, str) and "=" in item]
            if len(pairs) != len(environment) or len({pair[0] for pair in pairs}) != len(pairs):
                raise ResourceError("invalid application Redis environment")
            environment = dict(pairs)
        if not isinstance(environment, dict):
            raise ResourceError("invalid application Redis environment")
        if (environment.get("PROJECT_COPILOT_REDIS_ENABLED") != "true"
                or environment.get("PROJECT_COPILOT_REDIS_ADMISSION_ENABLED") != "true"
                or environment.get("PROJECT_COPILOT_REDIS_URL") != "redis://redis:6379/0"):
            raise ResourceError("application Redis configuration differs from optional overlay")


def check_config(*, with_redis=False):
    config = json.loads(docker("compose", *compose_files(with_redis=with_redis), "config",
                               "--no-interpolate", "--no-env-resolution", "--format", "json"))
    if config.get("name") != PROJECT:
        raise ResourceError("unexpected Compose project")
    for key, name in VOLUMES.items():
        spec = config["volumes"][key]
        if spec.get("name") != name or not spec.get("external"):
            raise ResourceError("dedicated physical volume name changed")
    for role, name in NETWORKS.items():
        if config["networks"][role].get("name") != name or not config["networks"][role].get("external"):
            raise ResourceError("network physical name must be fixed and external")
    ingress = config["services"].get("ingress", {})
    if ingress.get("scale", 1) != 1 or ingress.get("deploy", {}).get("replicas", 1) != 1:
        raise ResourceError("ingress shared zones require exactly one container")
    for name, service in config["services"].items():
        if set(service.get("networks", {})) != service_networks(name):
            raise ResourceError("rendered service network boundary changed")
        if service.get("ports"):
            raise ResourceError("public-demo services must not publish host ports")
    if with_redis:
        _check_redis_config(config)


def check_containers(values=None):
    binds = host_bind_paths(values) if values is not None else None
    # Enumerate volume consumers too: changing -p must not hide another writer
    # attached to the same fixed physical resources.
    ids = set(docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").decode().split())
    # Old maintenance/backup entry points also inspect a present optional Redis,
    # without adding it to their capture/restore roles or requiring it to exist.
    for volume in selected_volumes(with_redis=True).values():
        ids.update(docker("ps", "-aq", "--filter", f"volume={volume}").decode().split())
    running_ingress = 0
    for cid in ids:
        info = json.loads(docker("inspect", cid))[0]
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != PROJECT:
            raise ResourceError("dedicated resource attached to an unexpected Compose project")
        service = labels.get("com.docker.compose.service")
        if service == "ingress" and info.get("State", {}).get("Running"):
            running_ingress += 1
            if running_ingress > 1:
                raise ResourceError("only one shared-zone ingress instance is supported")
        if service not in {*MOUNTS, "cloudflared", "ingress", "redis"}:
            raise ResourceError("unknown service consumes dedicated resources")
        is_redis = service == "redis"
        if is_redis:
            redis_volume = inspect_volume(REDIS_VOLUMES["redis_data"])
            if redis_volume is None:
                raise ResourceError("optional Redis volume missing")
            validate_volume(redis_volume, REDIS_VOLUMES["redis_data"], "redis_data")
            if (labels.get(LABEL) != SCOPE or info.get("Config", {}).get("Image") != REDIS_IMAGE
                    or info.get("Config", {}).get("Cmd") != REDIS_COMMAND
                    or info.get("HostConfig", {}).get("PortBindings")
                    or info.get("HostConfig", {}).get("PublishAllPorts")
                    or any((info.get("NetworkSettings", {}).get("Ports") or {}).values())):
                raise ResourceError("optional Redis container image, role or published ports differ")
        wanted = {"/data": REDIS_VOLUMES["redis_data"]} if is_redis else MOUNTS.get(service, {})
        actual = {item["Destination"]: item for item in info.get("Mounts", [])}
        if is_redis and set(actual) != {"/data"}:
            raise ResourceError("optional Redis must mount only its dedicated data volume")
        for target, volume in wanted.items():
            item = actual.get(target, {})
            if item.get("Type") != "volume" or item.get("Name") != volume:
                raise ResourceError("container mounts do not match dedicated resources")
        if service in {"api", "worker", "migrate"} and binds is not None:
            for target, source in binds.items():
                item = actual.get(target, {})
                if (item.get("Type") != "bind" or item.get("RW") or
                        Path(item.get("Source", "/")).resolve() != source):
                    raise ResourceError("application read-only host mount is missing or differs")
        allowed_volumes = REDIS_VOLUMES.values() if is_redis else VOLUMES.values()
        for item in actual.values():
            if item.get("Type") == "volume" and item.get("Name") not in allowed_volumes:
                raise ResourceError("unexpected persistent volume attached")
            if item.get("Type") == "bind":
                target = item.get("Destination")
                if is_redis or target not in {"/app/models/huggingface", "/app/release"} or item.get("RW"):
                    raise ResourceError("unexpected or writable host bind attached")
                if binds is not None and Path(item["Source"]).resolve() != binds[target]:
                    raise ResourceError("actual host bind differs from reviewed private setting")
        attached = set(info.get("NetworkSettings", {}).get("Networks", {}))
        expected = {NETWORKS[role] for role in service_networks(service)}
        if (attached or is_redis) and attached != expected:
            raise ResourceError("container attached to unexpected network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "initialize"))
    parser.add_argument("--env-file", default=str(ROOT / ".env.public-demo"))
    parser.add_argument("--with-redis", action="store_true", help="include the optional dedicated Redis overlay and volume")
    args = parser.parse_args()
    try:
        values = validate_env(args.env_file)
        host_bind_paths(values)
        check_config(with_redis=args.with_redis)
        validate_rendered_env(args.env_file, values, with_redis=args.with_redis)
        check_volumes(initialize=args.operation == "initialize", with_redis=args.with_redis)
        check_networks(initialize=args.operation == "initialize")
        check_containers(values)
    except (ResourceError, OSError, ValueError, KeyError):
        print(json.dumps({"status": "error", "message": "public resource preflight failed; no old volume was adopted or removed"}))
        return 1
    print(json.dumps({"status": "ok", "scope": SCOPE, "volumes": selected_volumes(with_redis=args.with_redis)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
