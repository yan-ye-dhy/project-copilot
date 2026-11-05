"""Read-only CLOSED preparation inventory; never grants release authorization.

Uses only the standard library. Default checks are local files/Git/resources;
--docker additionally reads explicitly scoped metadata and release state. No
.env is loaded, services started, public HTTP requested, or evidence approved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "project-copilot-public-demo"
SCOPE = "public-demo-v2"
VOLUMES = {"postgres_data": "pc-public-v2-postgres", "qdrant_data": "pc-public-v2-qdrant",
           "phoenix_data": "pc-public-v2-phoenix", "manifests": "pc-public-v2-manifests"}
MOUNTS = {"postgres": ("/var/lib/postgresql/data", VOLUMES["postgres_data"]),
          "qdrant": ("/qdrant/storage", VOLUMES["qdrant_data"]),
          "phoenix": ("/data", VOLUMES["phoenix_data"]),
          "api": ("/app/data/manifests", VOLUMES["manifests"]),
          "worker": ("/app/data/manifests", VOLUMES["manifests"]),
          "migrate": ("/app/data/manifests", VOLUMES["manifests"])}
SERVICES = set(MOUNTS) | {"ingress", "cloudflared"}
ENTRY_SOURCES = ("cloud-functions/proxy.mjs", "cloud-functions/api/[[default]].js", "edgeone.json",
                 "deploy/nginx/public-demo.conf", "deploy/nginx/Dockerfile", "docker-compose.public-demo.yml")
STATE_SQL = ("BEGIN TRANSACTION READ ONLY; "
             "SELECT row_to_json(x) FROM (SELECT state, worker_enabled "
             "FROM release_control WHERE id = 1) x; ROLLBACK;")
# Deliberately exclude Env, command arguments, health logs and error text.
INSPECT_FORMAT = ('{"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
                  '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
                  '"running":{{json .State.Running}},"status":{{json .State.Status}},'
                  '"exit_code":{{json .State.ExitCode}},'
                  '"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},'
                  '"ports":{{json .NetworkSettings.Ports}},"mounts":{{json .Mounts}}}')
VOLUME_FORMAT = ('{"name":{{json .Name}},"scope":{{json (index .Labels "io.project-copilot.scope")}},'
                 '"role":{{json (index .Labels "io.project-copilot.role")}},'
                 '"driver":{{json .Driver}},"options":{{json .Options}}}')


class ProbeUnavailable(RuntimeError):
    """No subprocess stderr/stdout is ever exposed as diagnostic text."""


def run_readonly(args, *, cwd=ROOT):
    """Only call with program-owned argument lists; never use a shell."""
    try:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("PROJECT_COPILOT_", "COMPOSE_"))}
        env["GIT_OPTIONAL_LOCKS"] = "0"  # status must not refresh/write the index.
        result = subprocess.run(args, cwd=cwd, capture_output=True, timeout=15, check=False, env=env)
        if result.returncode or len(result.stdout) > 512 * 1024:
            raise ProbeUnavailable()
        return result.stdout.decode("utf-8", errors="strict")
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise ProbeUnavailable() from None


def item(name, status, **details):
    return {"check": name, "status": status, **details}


def private_path_check(path, *, directory=False):
    """Reject symlink components; inspect target owner/mode, not ancestor ACLs."""
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        return "FAIL"
    try:
        info = path.stat()
    except OSError:
        return "NEEDS_MANUAL_CHECK"
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    access = os.R_OK | os.X_OK if directory else os.R_OK
    searchable = not directory or bool(info.st_mode & stat.S_IXUSR)
    return "PASS" if correct_type and searchable and not info.st_mode & 0o077 and info.st_uid == os.getuid() and os.access(path, access) else "FAIL"


def local_checks(root=ROOT, evidence_dir=None, artifacts=(), runner=run_readonly):
    root = Path(root)
    checks = []
    try:
        head = runner(["git", "rev-parse", "HEAD"], cwd=root).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ProbeUnavailable()
        clean = not runner(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root).strip()
        checks.append(item("git", "PASS" if clean else "FAIL", head=head, clean=clean))
    except ProbeUnavailable:
        checks.append(item("git", "NEEDS_MANUAL_CHECK"))
    entry = [root / name for name in (*ENTRY_SOURCES, "scripts/build_edgeone.mjs")]
    for index, path in enumerate(entry):
        try:
            if path.is_symlink() or not path.is_file():
                raise OSError()
            checks.append(item(f"entry_sha256_{index}", "PASS", file=str(path.relative_to(root)),
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        except OSError:
            checks.append(item(f"entry_sha256_{index}", "NEEDS_MANUAL_CHECK"))
    evidence_dir = Path(evidence_dir) if evidence_dir else root / "data/release-evidence"
    checks.append(item("evidence_directory_private", private_path_check(evidence_dir, directory=True)))
    for index, path in enumerate(artifacts):
        # Private paths/names are not printed; this verifies readability, not
        # semantics, signatures, freshness, screenshots, or operator approval.
        checks.append(item(f"artifact_readable_{index}", private_path_check(path)))
    disk = shutil.disk_usage(root)
    memory = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                memory = int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    checks.append(item("capacity_inventory", "NEEDS_MANUAL_CHECK", cpu_count=os.cpu_count(),
                       available_memory_bytes=memory, free_disk_bytes=disk.free,
                       note="Inventory only; operator must assess model and backup headroom."))
    checks.append(item("cloud_and_evidence_review", "NEEDS_MANUAL_CHECK",
                       note="Cloud settings, true Origin/CSRF, quotas and evidence semantics are not verified locally."))
    return checks


def container_summary(info):
    """Validate only scoped services and emit non-sensitive bounded fields."""
    if not isinstance(info, dict):
        raise ProbeUnavailable()
    service = info.get("service")
    if info.get("project") != PROJECT or service not in SERVICES:
        raise ProbeUnavailable()
    status, running, exit_code = info.get("status"), info.get("running"), info.get("exit_code")
    if (type(running) is not bool or type(exit_code) is not int or not 0 <= exit_code <= 255 or
            status not in {"created", "restarting", "running", "removing", "paused", "exited", "dead"} or
            info.get("health") not in {"healthy", "unhealthy", "starting", None}):
        raise ProbeUnavailable()
    return {"service": service, "running": running, "status": status,
            "health": info.get("health"), "exit_code": exit_code}


def docker_checks(runner=run_readonly):
    checks = []
    try:
        raw_ids = runner(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).split()
        if not raw_ids or any(not re.fullmatch(r"[0-9a-f]{12,64}", cid) for cid in raw_ids):
            raise ProbeUnavailable()
        records = {}
        for cid in raw_ids:
            info = json.loads(runner(["docker", "inspect", "--format", INSPECT_FORMAT, cid]))
            summary = container_summary(info)
            service = summary["service"]
            if service in records:
                raise ProbeUnavailable()
            records[service] = (cid, info, summary)
        checks.append(item("container_inventory", "PASS", containers=[r[2] for r in records.values()],
                           note="API/ingress unhealthy while CLOSED is expected; not readiness approval."))
        all_mounts_valid = True
        no_ports = True
        for _, info, summary in records.values():
            mounts = info.get("mounts") or []
            ports = info.get("ports") or {}
            if not isinstance(mounts, list) or not all(isinstance(m, dict) for m in mounts) or not isinstance(ports, dict):
                raise ProbeUnavailable()
            expected = MOUNTS.get(summary["service"])
            if expected:
                all_mounts_valid &= any(m.get("Destination") == expected[0] and m.get("Type") == "volume" and m.get("Name") == expected[1] for m in mounts)
            all_mounts_valid &= all(m.get("Name") in VOLUMES.values() for m in mounts if m.get("Type") == "volume")
            no_ports &= not any(ports.values())
        volume_valid = True
        for role, name in VOLUMES.items():
            volume = json.loads(runner(["docker", "volume", "inspect", "--format", VOLUME_FORMAT, name]))
            if not isinstance(volume, dict):
                raise ProbeUnavailable()
            volume_valid &= (volume.get("name") == name and volume.get("scope") == SCOPE and volume.get("role") == role
                             and volume.get("driver") == "local" and not volume.get("options"))
        checks.append(item("dedicated_volume_labels_and_mounts", "PASS" if all_mounts_valid and volume_valid else "FAIL",
                           note="Does not replace public_resources check of networks and other volume consumers."))
        checks.append(item("no_project_host_ports", "PASS" if no_ports else "FAIL",
                           note="Project containers only; separately inspect host listeners and other tunnel processes."))
        migrate = records.get("migrate")
        checks.append(item("migration_exit", "PASS" if migrate and migrate[2]["status"] == "exited" and not migrate[2]["running"] and migrate[2]["exit_code"] == 0 else "NEEDS_MANUAL_CHECK"))
        stopped = all(not records[s][2]["running"] and records[s][2]["status"] in {"created", "exited"}
                      for s in ("worker", "cloudflared") if s in records)
        checks.append(item("worker_tunnel_stopped", "PASS" if stopped else "FAIL",
                           note="Checks scoped containers, not externally launched host processes."))
        api = records.get("api")
        cache_ok = False
        if api:
            for mount in api[1].get("mounts") or []:
                if mount.get("Destination") == "/app/models/huggingface" and mount.get("Type") == "bind" and not mount.get("RW"):
                    cache = Path(mount.get("Source", ""))
                    cache_ok = cache.is_absolute() and cache.is_dir() and any(cache.glob("models--BAAI--bge-reranker-v2-m3/snapshots/*/config.json"))
        checks.append(item("readonly_model_cache_files", "PASS" if cache_ok else "NEEDS_MANUAL_CHECK",
                           note="File presence only; frozen revision/completeness/inference not established."))
        postgres = records.get("postgres")
        if postgres and postgres[2]["running"] and all_mounts_valid and volume_valid:
            result = json.loads(runner(["docker", "exec", postgres[0], "psql", "-X", "-w", "-U", "project_copilot_public",
                                        "-d", "project_copilot_public", "-qAt", "-c", STATE_SQL]))
            if not isinstance(result, dict):
                raise ProbeUnavailable()
            closed = result.get("state") == "CLOSED" and result.get("worker_enabled") is False
            checks.append(item("closed_worker_disabled", "PASS" if closed else "FAIL",
                               state=result.get("state") if result.get("state") in {"CLOSED", "STAGING", "PUBLIC"} else "unknown",
                               worker_enabled=result.get("worker_enabled") is True))
        else:
            checks.append(item("closed_worker_disabled", "NEEDS_MANUAL_CHECK"))
    except (ProbeUnavailable, ValueError, TypeError, KeyError, OSError):
        checks.append(item("docker_inventory_incomplete", "NEEDS_MANUAL_CHECK",
                           note="Docker unavailable, missing resources or unexpected metadata; no stderr or private config displayed."))
    checks.append(item("host_tunnel_and_listener_review", "NEEDS_MANUAL_CHECK",
                       note="Operator must verify Windows/WSL listeners and independently launched tunnel/worker processes."))
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="read scoped Docker metadata and release state; never start services")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    checks = local_checks(evidence_dir=args.evidence_dir, artifacts=args.artifact)
    checks += docker_checks() if args.docker else [item("docker_checks_not_requested", "NEEDS_MANUAL_CHECK")]
    # Intentionally no READY_FOR_STAGING_AUTHORIZATION verdict or artifact.
    print(json.dumps({"mode": "read_only_closed_inventory", "checks": checks,
                      "staging_authorization": "NOT_EVALUATED"}, ensure_ascii=False))
    return 1 if any(c["status"] == "FAIL" for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
