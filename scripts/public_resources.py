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
NETWORKS = {"backend": "pc-public-v2-backend", "egress": "pc-public-v2-egress"}
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


def check_volumes(*, initialize=False):
    existing = {}
    for role, name in VOLUMES.items():
        info = inspect_volume(name)
        if info is not None:
            validate_volume(info, name, role)
        existing[role] = info
    for role, name in VOLUMES.items():
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


def validate_rendered_env(path, values):
    command = ("compose", "--env-file", str(path), "-f", str(ROOT / "docker-compose.public-demo.yml"), "config")
    raw = json.loads(docker(*command, "--no-env-resolution", "--format", "json"))
    rendered = json.loads(docker(*command, "--format", "json"))
    if rendered.get("name") != PROJECT or raw.get("name") != PROJECT:
        raise ResourceError("rendered project differs from dedicated public deployment")
    for role in ("api", "worker", "migrate"):
        files = raw["services"][role].get("env_file", [])
        if len(files) != 1 or Path(files[0]["path"]).resolve() != Path(path).resolve():
            raise ResourceError("rendered application env-file differs from reviewed private file")
        if rendered["services"][role]["environment"].get("PROJECT_COPILOT_DATABASE_URL") != values["PROJECT_COPILOT_DATABASE_URL"]:
            raise ResourceError("rendered database target differs from reviewed private file")
    if rendered["services"]["postgres"]["environment"].get("POSTGRES_PASSWORD") != values["PROJECT_COPILOT_PUBLIC_POSTGRES_PASSWORD"]:
        raise ResourceError("rendered datastore credentials differ")


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


def check_config():
    config = json.loads(docker("compose", "-f", str(ROOT / "docker-compose.public-demo.yml"), "config",
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
    for service in config["services"].values():
        if service.get("ports"):
            raise ResourceError("public-demo services must not publish host ports")


def check_containers(values=None):
    binds = host_bind_paths(values) if values is not None else None
    # Enumerate volume consumers too: changing -p must not hide another writer
    # attached to the same fixed physical resources.
    ids = set(docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}").decode().split())
    for volume in VOLUMES.values():
        ids.update(docker("ps", "-aq", "--filter", f"volume={volume}").decode().split())
    for cid in ids:
        info = json.loads(docker("inspect", cid))[0]
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") != PROJECT:
            raise ResourceError("dedicated resource attached to an unexpected Compose project")
        service = labels.get("com.docker.compose.service")
        if service not in {*MOUNTS, "cloudflared"}:
            raise ResourceError("unknown service consumes dedicated resources")
        wanted = MOUNTS.get(service, {})
        actual = {item["Destination"]: item for item in info.get("Mounts", [])}
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
        for item in actual.values():
            if item.get("Type") == "volume" and item.get("Name") not in VOLUMES.values():
                raise ResourceError("unexpected persistent volume attached")
            if item.get("Type") == "bind":
                target = item.get("Destination")
                if target not in {"/app/models/huggingface", "/app/release"} or item.get("RW"):
                    raise ResourceError("unexpected or writable host bind attached")
                if binds is not None and Path(item["Source"]).resolve() != binds[target]:
                    raise ResourceError("actual host bind differs from reviewed private setting")
        attached = set(info.get("NetworkSettings", {}).get("Networks", {}))
        expected = {NETWORKS["backend"]} if service in {"postgres", "qdrant", "phoenix"} else {NETWORKS["egress"]} if service == "cloudflared" else set(NETWORKS.values())
        if attached and attached != expected:
            raise ResourceError("container attached to unexpected network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("check", "initialize"))
    parser.add_argument("--env-file", default=str(ROOT / ".env.public-demo"))
    args = parser.parse_args()
    try:
        values = validate_env(args.env_file)
        host_bind_paths(values)
        check_config()
        validate_rendered_env(args.env_file, values)
        check_volumes(initialize=args.operation == "initialize")
        check_networks(initialize=args.operation == "initialize")
        check_containers(values)
    except (ResourceError, OSError, ValueError, KeyError):
        print(json.dumps({"status": "error", "message": "public resource preflight failed; no old volume was adopted or removed"}))
        return 1
    print(json.dumps({"status": "ok", "scope": SCOPE, "volumes": VOLUMES}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
