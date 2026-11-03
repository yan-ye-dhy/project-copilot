"""Quiescent cold backup / fresh-volume restore for the dedicated demo only.

No cloud/network tunnel operation, volume removal or existing-volume restore is
implemented. A backup is verified only after a separate isolated restore drill.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import fcntl
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
from uuid import uuid4

from scripts.public_resources import (ROOT, PROJECT, SCOPE, VOLUMES, ResourceError,
                                      check_config, check_containers, check_networks, check_volumes, docker, validate_env, validate_rendered_env, host_bind_paths)

ROLES = ("postgres_data", "qdrant_data", "manifests")
HELPER_IMAGE = "postgres:17.11-bookworm"
MAX_BYTES = 2 * 1024 ** 3


@contextmanager
def operation_lock():
    path = ROOT / "data" / "public-demo-operations.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ResourceError("another public deployment/backup operation is active") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def digest(path):
    result = sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def validate_archive(path):
    with tarfile.open(path, "r:gz") as archive:
        expanded = 0
        for member in archive:
            expanded += max(member.size, 0)
            if expanded > MAX_BYTES:
                raise ResourceError("archive expands beyond storage budget")
            p = Path(member.name)
            if p.is_absolute() or ".." in p.parts or member.isdev() or member.isfifo():
                raise ResourceError("archive contains an unsafe entry")
            if member.issym() or member.islnk():
                # PG tablespace links could target another data volume; do not
                # support them rather than restoring outside the new resource.
                raise ResourceError("archive links require a separately reviewed restore plan")


def verify_live():
    """Run inside a one-off app container after ingress and workers are stopped."""
    from sqlalchemy import text
    from project_copilot.config import load_settings
    from project_copilot.db.session import Database
    from project_copilot.db.migrations import check_model_drift
    from project_copilot.infrastructure.qdrant import create_qdrant_store
    from project_copilot.rag.final_retrieval import FinalRetriever
    from project_copilot.rag.bm25 import BM25Index, read_completed_chunks
    from project_copilot.rag.index_lock import index_lock
    from project_copilot.services.release_control import admission_exclusive, require_closed
    from project_copilot.db.models import Base

    settings = load_settings()
    db = Database(settings.database_url.get_secret_value())
    try:
        check_model_drift(db)
        with db.transaction() as session:
            admission_exclusive(session)
            require_closed(session)
            if not session.scalar(text("SELECT pg_try_advisory_xact_lock(604116220002)")):
                raise ResourceError("a worker execution remains active")
            rows = {}
            for table in sorted(Base.metadata.tables.values(), key=lambda value: value.name):
                # Tables are an ORM metadata allowlist; primary-key ordering is
                # deterministic and includes auth, jobs and all audit tables.
                primary = list(table.primary_key.columns)
                rows[table.name] = [dict(row) for row in session.execute(table.select().order_by(*primary)).mappings()]
            db_digest = sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
            retriever = FinalRetriever(settings)
            try:
                with index_lock(retriever.manifest.path), create_qdrant_store(settings) as store:
                    manifest = retriever.manifest.load()
                    chunks = read_completed_chunks(store._client, manifest) if manifest.documents else []
                    if chunks:
                        snapshot = BM25Index.load(retriever.snapshots.path, manifest)
                        if tuple(chunks) != snapshot.snapshot.chunks:
                            raise ResourceError("manifest, Qdrant and BM25 differ")
                    ids = [str(c.metadata.chunk_id) for c in chunks]
                    records = []
                    for start in range(0, len(ids), 64):
                        for item in store._client.retrieve(settings.qdrant_collection_name, ids[start:start+64],
                                                           with_payload=True, with_vectors=True):
                            records.append(item.model_dump(mode="json"))
                    records.sort(key=lambda item: str(item["id"]))
                    index_digest = sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
                    # Use a stored vector as a read-only probe: no external
                    # Embedding or BGE inference is required. The same query
                    # must produce the same real Qdrant ranking after restore.
                    query_ids = []
                    bm25_ids = []
                    if records:
                        vector = records[0]["vector"][manifest.spec.vector_name]
                        hits = store._client.query_points(manifest.spec.collection_name,
                            query=vector, using=manifest.spec.vector_name, limit=min(len(records), 10),
                            with_payload=False, with_vectors=False).points
                        query_ids = [str(hit.id) for hit in hits]
                        bm25_ids = [str(hit.metadata.chunk_id) for hit in snapshot.search(chunks[0].text[:200], top_k=5)]
                    result = {"database_digest": db_digest, "index_digest": index_digest,
                              "document_count": len(manifest.documents), "chunk_count": len(chunks),
                              "dense_probe_ids": query_ids, "bm25_probe_ids": bm25_ids}
            finally:
                retriever.close()
        return result
    finally:
        db.close()


def compose(env_file, *args):
    return docker("compose", "--env-file", str(env_file), "-f", str(ROOT / "docker-compose.public-demo.yml"), *args)


def assert_no_writers(values=None):
    check_containers(values)
    for service in ("cloudflared", "api", "worker", "migrate"):
        ids = docker("ps", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}",
                     "--filter", f"label=com.docker.compose.service={service}").decode().strip()
        if ids:
            raise ResourceError("an ingress, worker or maintenance container is still running")


def backup(env_file, output):
    values = validate_env(env_file)
    check_config()
    validate_rendered_env(env_file, values)
    check_volumes()
    check_networks()
    check_containers(values)
    if output.exists() or output.parent.is_symlink():
        raise ResourceError("backup output must be a new private directory")
    existing_bytes = sum(p.stat().st_size for p in output.parent.rglob("*") if p.is_file() and not p.is_symlink()) if output.parent.exists() else 0
    if existing_bytes >= MAX_BYTES:
        raise ResourceError("backup root at capacity; preserve unverified evidence and review retention")
    output.mkdir(mode=0o700, parents=True)
    # Never restart these automatically on success or failure.
    for service, timeout in (("cloudflared", 30), ("api", 120), ("worker", 360)):
        compose(env_file, "stop", "-t", str(timeout), service)
    assert_no_writers(values)
    live = json.loads(compose(env_file, "run", "--rm", "--no-deps", "-T", "api", "python", "-m",
                              "scripts.public_backup", "verify-live"))
    assert_no_writers(values)
    for service in ("qdrant", "postgres"):
        compose(env_file, "stop", "-t", "120", service)
    images = {}
    for service in ("postgres", "qdrant"):
        ids = docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}",
                     "--filter", f"label=com.docker.compose.service={service}").decode().split()
        if len(ids) != 1:
            raise ResourceError("expected one dedicated stopped datastore")
        info = json.loads(docker("inspect", ids[0]))[0]
        state = info["State"]
        images[service] = info["Image"]
        # This pinned Qdrant image exits 143 after its SIGTERM graceful-shutdown
        # path. PostgreSQL must complete its own clean stop with code zero.
        permitted = {0, 143} if service == "qdrant" else {0}
        if state["Running"] or state["ExitCode"] not in permitted or state.get("OOMKilled"):
            raise ResourceError("datastore did not stop cleanly; preserve diagnostics")
    manifest = {"format": 1, "scope": SCOPE, "created_at": datetime.now(timezone.utc).isoformat(),
                "verified": False, "diagnostic_hold": False, "postgres_image": HELPER_IMAGE,
                "consistency": live, "images": images, "files": {}}
    try:
        total = 0
        for role in ROLES:
            target = output / f"{role}.tar.gz"
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                process = subprocess.Popen(["docker", "run", "--rm", "--network", "none", "--read-only",
                    "--mount", f"type=volume,src={VOLUMES[role]},dst=/source,readonly", HELPER_IMAGE,
                    "tar", "--numeric-owner", "-C", "/source", "-czf", "-", "."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while block := process.stdout.read(1024 * 1024):
                        total += len(block)
                        if total + existing_bytes > MAX_BYTES:
                            raise ResourceError("backup exceeds storage bound; partial evidence retained")
                        stream.write(block)
                    if process.wait() != 0:
                        raise ResourceError("cold archive failed")
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            if total + existing_bytes > MAX_BYTES:
                raise ResourceError("backup exceeds configured 2 GiB bound; evidence retained")
            validate_archive(target)
            manifest["files"][target.name] = {"sha256": digest(target), "bytes": target.stat().st_size}
        (output / "backup.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.chmod(output / "backup.json", 0o600)
        return {"status": "captured_unverified", "files": len(manifest["files"]), "bytes": total}
    except Exception:
        (output / "FAILED").write_text("Capture failed. Keep private diagnostics; no automatic restart or deletion.\n")
        raise


def restore(backup_dir):
    """Only creates a new UUID-scoped resource set, never starts restored apps."""
    metadata = json.loads((backup_dir / "backup.json").read_text())
    if metadata.get("format") != 1 or metadata.get("scope") != SCOPE or set(metadata.get("files", {})) != {f"{role}.tar.gz" for role in ROLES}:
        raise ResourceError("backup manifest does not match the dedicated format")
    total = 0
    for name, spec in metadata["files"].items():
        path = backup_dir / name
        if path.is_symlink() or not path.is_file() or digest(path) != spec["sha256"] or path.stat().st_size != spec["bytes"]:
            raise ResourceError("backup checksum/size mismatch")
        total += path.stat().st_size
        validate_archive(path)
    if total > MAX_BYTES:
        raise ResourceError("restore exceeds bounded backup size")
    prefix = "pc-restore-" + uuid4().hex
    names = {role: f"{prefix}-{role}" for role in ROLES}
    for role, name in names.items():
        if name in docker("volume", "ls", "--format", "{{.Name}}").decode().splitlines():
            raise ResourceError("fresh restore resource unexpectedly exists")
        docker("volume", "create", "--label", f"io.project-copilot.restore={prefix}", name)
        created = json.loads(docker("volume", "inspect", name))[0]
        if ((created.get("Labels") or {}).get("io.project-copilot.restore") != prefix or
                created.get("Driver") != "local" or created.get("Options")):
            raise ResourceError("fresh restore volume ownership mismatch")
        with (backup_dir / f"{role}.tar.gz").open("rb") as stream:
            result = subprocess.run(["docker", "run", "--rm", "-i", "--network", "none", "--read-only",
                "--mount", f"type=volume,src={name},dst=/restore", HELPER_IMAGE,
                "tar", "--numeric-owner", "--same-owner", "-C", "/restore", "-xzf", "-"], stdin=stream,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise ResourceError("fresh restore failed; preserve fresh volumes for diagnosis")
    result = {"status": "restored_unverified", "resource_id": prefix, "volumes": names,
              "backup_identity": sha256(json.dumps({k: metadata[k] for k in ("format", "scope", "created_at", "files", "images", "consistency")}, sort_keys=True).encode()).hexdigest(),
              "expected_consistency": metadata["consistency"]}
    plan = backup_dir / (prefix + ".json")
    plan.write_text(json.dumps(result, indent=2) + "\n")
    os.chmod(plan, 0o600)
    return {**result, "plan": str(plan)}


def verify_restored(env_file, backup_dir, plan_path):
    try:
        return _verify_restored(env_file, backup_dir, plan_path)
    except Exception:
        metadata_path = backup_dir / "backup.json"
        if metadata_path.is_file() and not metadata_path.is_symlink() and metadata_path.stat().st_size <= 64 * 1024:
            metadata = json.loads(metadata_path.read_text())
            metadata["diagnostic_hold"] = True
            metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
        raise


def _verify_restored(env_file, backup_dir, plan_path):
    """Boot only fresh restored datastores; compare actual DB + vectors + text.

    Uses a derived copy of the public Compose file and no published ports.
    Stops every created container on completion, preserving volumes on failure.
    """
    values = validate_env(env_file)
    if ((backup_dir / "backup.json").is_symlink() or (backup_dir / "backup.json").stat().st_size > 64 * 1024 or
            plan_path.is_symlink() or plan_path.stat().st_size > 64 * 1024):
        raise ResourceError("bounded regular metadata files required")
    metadata = json.loads((backup_dir / "backup.json").read_text())
    plan = json.loads(plan_path.read_text())
    required = {"format", "scope", "created_at", "files", "images", "consistency"}
    if (not required.issubset(metadata) or metadata["format"] != 1 or metadata["scope"] != SCOPE or
            set(metadata["files"]) != {f"{role}.tar.gz" for role in ROLES} or
            plan.get("backup_identity") != sha256(json.dumps({k: metadata[k] for k in required}, sort_keys=True).encode()).hexdigest()):
        raise ResourceError("restore plan is not bound to this backup")
    for name, spec in metadata["files"].items():
        file = backup_dir / name
        if file.is_symlink() or file.stat().st_size != spec["bytes"] or digest(file) != spec["sha256"]:
            raise ResourceError("backup changed after restore")
    prefix = plan.get("resource_id", "")
    if (not __import__("re").fullmatch(r"pc-restore-[0-9a-f]{32}", prefix) or
            set(plan.get("volumes", {})) != set(ROLES) or
            any(name != f"{prefix}-{role}" for role, name in plan["volumes"].items())):
        raise ResourceError("not a fresh restore plan")
    for name in plan["volumes"].values():
        info = json.loads(docker("volume", "inspect", name))[0]
        if (info.get("Labels") or {}).get("io.project-copilot.restore") != prefix:
            raise ResourceError("restore volume ownership mismatch")
    config = json.loads(docker("compose", "-f", str(ROOT / "docker-compose.public-demo.yml"), "config",
                               "--no-interpolate", "--no-env-resolution", "--format", "json"))
    config["name"] = prefix
    for network in config["networks"].values():
        network.pop("name", None)
        network.pop("external", None)
    config["networks"]["backend"]["internal"] = True
    config["services"] = {key: config["services"][key] for key in ("postgres", "qdrant", "api")}
    for key in ROLES:
        config["volumes"][key] = {"name": plan["volumes"][key], "external": True}
    config["volumes"].pop("phoenix_data", None)
    config["services"]["api"].pop("depends_on", None)
    config["services"]["api"].pop("build", None)
    config["services"]["api"]["healthcheck"] = {"disable": True}
    config["services"]["api"]["env_file"] = [{"path": str(env_file.resolve()), "required": True}]
    binds = host_bind_paths(values)
    for mount in config["services"]["api"]["volumes"]:
        if mount["type"] == "bind":
            mount["source"] = str(binds[mount["target"]])
    for name in ("postgres", "qdrant"):
        image = metadata.get("images", {}).get(name, "")
        if not __import__("re").fullmatch("sha256:[0-9a-f]{64}", image):
            raise ResourceError("backup lacks exact datastore image identity")
        config["services"][name]["image"] = image
    config["services"]["api"]["environment"].update({"PROJECT_COPILOT_DEEPSEEK_API_KEY": "", "PROJECT_COPILOT_ZHIPU_API_KEY": ""})
    config["services"]["api"]["image"] = os.environ.get("PROJECT_COPILOT_PUBLIC_API_IMAGE", "project-copilot-api:public-demo")
    with tempfile.TemporaryDirectory(prefix="pc-restore-verify-") as temporary:
        path = Path(temporary) / "compose.json"
        path.write_text(json.dumps(config))
        os.chmod(path, 0o600)
        command = ("compose", "--env-file", str(env_file), "-f", str(path))
        try:
            docker(*command, "up", "-d", "--wait", "postgres", "qdrant")
            actual = json.loads(docker(*command, "run", "--rm", "--no-deps", "-T", "api", "python", "-m",
                                       "scripts.public_backup", "verify-live"))
            if actual != metadata["consistency"] or actual != plan["expected_consistency"]:
                raise ResourceError("restored business/index digests differ")
            metadata["verified"] = True
            metadata["verification"] = {"resource_id": prefix, "consistency": actual,
                                        "verified_at": datetime.now(timezone.utc).isoformat()}
            (backup_dir / "backup.json").write_text(json.dumps(metadata, indent=2) + "\n")
            return {"status": "restore_verified", "consistency": actual}
        except Exception:
            metadata["diagnostic_hold"] = True
            (backup_dir / "backup.json").write_text(json.dumps(metadata, indent=2) + "\n")
            raise
        finally:
            docker(*command, "stop", "-t", "120")


def prune(root, *, max_bytes=MAX_BYTES, keep=7):
    """Remove only older verified sets; held/unverified evidence always survives."""
    if not 1 <= keep <= 7 or not 1 <= max_bytes <= MAX_BYTES:
        raise ResourceError("invalid bounded retention configuration")
    if root.is_symlink() or not root.is_dir():
        raise ResourceError("private regular backup directory required")
    entries = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        file = path / "backup.json"
        if not file.exists():
            continue
        data = json.loads(file.read_text())
        if data.get("format") != 1 or data.get("scope") != SCOPE or set(data.get("files", {})) != {f"{role}.tar.gz" for role in ROLES}:
            raise ResourceError("unknown backup set; preserve for manual review")
        size = sum(item.stat().st_size for item in path.iterdir() if item.is_file())
        entries.append((path, data, size))
    eligible = sorted([item for item in entries if item[1].get("verified") is True and not item[1].get("diagnostic_hold")],
                      key=lambda item: item[1]["created_at"], reverse=True)
    # Partial captures and diagnostic files without a valid manifest still
    # count toward capacity; never erase or hide them to satisfy the budget.
    total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    removed = 0
    for index, (path, data, size) in reversed(list(enumerate(eligible))):
        if index < keep and total <= max_bytes:
            continue
        if len(eligible) - removed <= 1 and total > max_bytes:
            raise ResourceError("cannot meet capacity without deleting the last verified restore point")
        allowed = {"backup.json", *data.get("files", {})}
        if data.get("verification", {}).get("resource_id"):
            allowed.add(data["verification"]["resource_id"] + ".json")
        # Restore plans are retained as provenance. Explicit pruning never
        # deletes a directory containing them or other investigation evidence.
        if {p.name for p in path.iterdir()} != allowed or any(p.is_symlink() for p in path.iterdir()):
            raise ResourceError("unexpected evidence in backup directory; no pruning")
        for item in path.iterdir():
            item.unlink()
        path.rmdir()
        total -= size
        removed += 1
    if total > max_bytes:
        raise ResourceError("held/unverified evidence exceeds storage budget; manual review required")
    return {"removed_verified_sets": removed, "bytes": total}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("verify-live", "backup", "restore", "verify-restored", "prune"))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.public-demo")
    parser.add_argument("--path", type=Path)
    parser.add_argument("--restore-plan", type=Path)
    args = parser.parse_args()
    try:
        if args.operation == "verify-live":
            result = verify_live()
        else:
            if args.path is None:
                parser.error("--path is required")
            with operation_lock():
                if args.operation == "backup":
                    result = backup(args.env_file, args.path)
                elif args.operation == "restore":
                    result = restore(args.path)
                elif args.operation == "verify-restored":
                    if args.restore_plan is None:
                        parser.error("--restore-plan is required")
                    result = verify_restored(args.env_file, args.path, args.restore_plan)
                else:
                    result = prune(args.path)
    except Exception:
        print(json.dumps({"status": "error", "message": "backup/restore stopped safely; keep private diagnostics and do not restart automatically"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
