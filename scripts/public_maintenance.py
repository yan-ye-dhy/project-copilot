"""Host-only administrator inspection, bounded retention and verified retirement.

No command replays a job. Passwords use getpass and subprocess stdin, never
arguments or files. Recovery requires CLOSED, no execution lease, machine-
verified stopped containers and a separately reviewed provider/business receipt.
"""
import argparse
from datetime import datetime, timezone
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.services.auth import AuthService
from project_copilot.services.public_maintenance import cleanup, inspect_job, resolve_no_retry
from project_copilot.services.worker_stop import sign_inventory


def collect_stopped_workers(job_id, *, docker, volumes, scope, project, label, review_sha256):
    """Inspect all consumers of the fixed manifest volume across project names.

    API and migration processes can hold threads/locks too. They must be stopped
    during recovery; this invocation creates its own maintenance container only
    after inventory capture. The supported deployment never runs a host worker.
    """
    if scope != "public-demo-v2":
        raise ValueError("recovery requires the dedicated public resource scope")
    if not isinstance(review_sha256, str) or not re.fullmatch("[0-9a-f]{64}", review_sha256):
        raise ValueError("verified review artifact digest required")
    ids = docker("ps", "-aq", "--filter", "volume=" + volumes["manifests"]).decode().split()
    workers = []
    for cid in ids:
        info = json.loads(docker("inspect", cid))[0]
        labels = info.get("Config", {}).get("Labels") or {}
        state = info.get("State", {})
        service = labels.get("com.docker.compose.service")
        if labels.get(label) != scope or labels.get("com.docker.compose.project") != project or service not in {"api", "worker", "migrate"}:
            raise ValueError("untrusted process consumes the public manifest volume")
        if state.get("Running") or state.get("Restarting") or state.get("Pid") != 0 or state.get("Status") != "exited":
            raise ValueError("stop API, worker and migration containers before recovery")
        if service == "worker":
            workers.append({"container_id": info["Id"], "status": state["Status"], "pid": state["Pid"],
                "running": state["Running"], "restarting": state["Restarting"], "finished_at": state["FinishedAt"]})
    if not workers:
        raise ValueError("no stopped worker is available to verify")
    return {"version": 1, "job_id": str(job_id), "resource_id": scope,
        "captured_at": datetime.now(timezone.utc).isoformat(), "postgres_volume": volumes["postgres_data"],
        "workers": sorted(workers, key=lambda row: row["container_id"]), "review_sha256": review_sha256}


def verify_review_artifact(path, evidence):
    """Hash a bounded private regular file without transmitting its contents.

    The artifact may be the operator's provider receipt and database review.
    Its meaning still requires human review; byte identity is machine-verified.
    """
    if not isinstance(evidence, dict) or not isinstance(evidence.get("evidence_reference_sha256"), str):
        raise ValueError("review artifact reference is missing")
    expected = evidence["evidence_reference_sha256"]
    if not re.fullmatch("[0-9a-f]{64}", expected):
        raise ValueError("invalid review artifact reference")
    limit = 8 * 1024 * 1024
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 1 <= info.st_size <= limit:
            raise ValueError("review artifact must be a private bounded regular file")
        data = stream.read(limit + 1)
        if not 1 <= len(data) <= limit:
            raise ValueError("review artifact exceeds the size bound")
    digest = sha256(data).hexdigest()
    if digest != expected:
        raise ValueError("review artifact differs from the recovery receipt")
    return digest


def write_private_output(path, document):
    """Save an inspection once; never follow a link or overwrite existing data."""
    path = Path(path).absolute()
    parent = path.parent
    # Refuse linked ancestors before mkdir can create directories elsewhere.
    if any(item.is_symlink() for item in (parent, *parent.parents)):
        raise ValueError("inspection output parent cannot contain symlinks")
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("inspection output directory must be private")
        output = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=descriptor)
        with os.fdopen(output, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def internal(payload):
    settings = load_settings()
    if (not settings.public_demo or settings.environment != "production" or settings.database_url is None or
            settings.public_resource_id != "public-demo-v2"):
        raise ValueError("maintenance requires the dedicated public environment")
    database = Database(settings.database_url.get_secret_value())
    try:
        with database.transaction() as session:
            admin = AuthService(session)._verify_admin(payload["username"], payload["password"])
            actor_id = admin.id
            operation = payload["operation"]
            if operation == "inspect-job":
                return inspect_job(session, actor_id, UUID(payload["job_id"]))
            if operation == "cleanup":
                if payload.get("apply") is not True:
                    raise ValueError("explicit cleanup approval required")
                return cleanup(session, actor_id)
            if operation == "resolve-job":
                return resolve_no_retry(session, actor_id, UUID(payload["job_id"]), payload["evidence"],
                    worker_stop_proof=payload["worker_stop_proof"], settings=settings)
            if operation == "retire-legacy-job":
                from project_copilot.services.public_maintenance import retire_legacy_unknown
                return retire_legacy_unknown(session, actor_id, UUID(payload["job_id"]), payload["evidence"],
                    risk_acceptance=payload.get("risk_acceptance"),
                    worker_stop_proof=payload["worker_stop_proof"], settings=settings)
            raise ValueError("unsupported maintenance operation")
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect-job", "cleanup", "resolve-job", "retire-legacy-job"), nargs="?")
    parser.add_argument("--job-id", type=UUID)
    parser.add_argument("--env-file", default=".env.public-demo")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--review-artifact", type=Path, help="私有供应商及业务核实工件，内容仅在本机校验摘要")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--personal-image", action="store_true",
                        help="use the fixed deploy/personal-demo.image.yml image selection")
    parser.add_argument("--output", type=Path, help="inspect-job only: create a private inspection JSON without overwriting")
    parser.add_argument("--internal", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.internal:
            if args.output is not None or args.personal_image:
                raise ValueError("host-only output/image options")
            # Bounded stdin is populated only by the trusted host wrapper.
            raw = sys.stdin.buffer.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ValueError("maintenance input too large")
            result = internal(json.loads(raw))
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError("interactive host terminal required")
            if args.operation is None or args.operation != "cleanup" and args.job_id is None:
                raise ValueError("operation/job identity required")
            if args.operation == "cleanup" and not args.apply:
                raise ValueError("cleanup requires --apply")
            if args.output is not None and args.operation != "inspect-job":
                raise ValueError("--output is only available for inspect-job")
            if args.operation == "retire-legacy-job":
                from project_copilot.services.public_maintenance import LEGACY_JOB_ID
                if args.job_id != LEGACY_JOB_ID:
                    raise ValueError("legacy retirement is restricted to the reviewed task")
            from scripts import public_resources as resources
            values = resources.validate_env(args.env_file)
            resources.check_config()
            resources.check_volumes()
            resources.check_containers()
            payload = {"operation": args.operation, "job_id": str(args.job_id) if args.job_id else None,
                       "apply": args.apply}
            retiring = args.operation in {"resolve-job", "retire-legacy-job"}
            if retiring:
                if (args.evidence is None or args.evidence.is_symlink() or not args.evidence.is_file() or
                        args.evidence.stat().st_size > 32 * 1024 or args.evidence.stat().st_mode & 0o077):
                    raise ValueError("private evidence receipt required")
                payload["evidence"] = json.loads(args.evidence.read_text(encoding="utf-8"))
                if args.review_artifact is None:
                    raise ValueError("--review-artifact is required for recovery")
                review_sha256 = verify_review_artifact(args.review_artifact, payload["evidence"])
                if args.operation == "retire-legacy-job":
                    from project_copilot.services.public_maintenance import LEGACY_RISK_PHRASE
                    phrase = input("仅处理指定历史任务。必须已核实原归属、本地全部执行已终止及业务副作用；"
                        "供应商最终执行状态与费用仍未核实，不会退款或重试。"
                        f"如接受此剩余风险，请完整输入 {LEGACY_RISK_PHRASE}：")
                    if phrase != LEGACY_RISK_PHRASE:
                        raise ValueError("explicit unverified-provider risk acceptance missing")
                    payload["risk_acceptance"] = phrase
                elif input("已核实供应商最终状态和数据库业务结果，输入 terminal_no_retry：").strip() != "terminal_no_retry":
                    raise ValueError("recovery approval missing")
            payload["username"] = input("管理员账号：").strip().lower()
            payload["password"] = getpass.getpass("管理员密码：")
            if retiring:
                # Capture after interactive prompts so the proof is fresh when
                # the maintenance container acquires its exclusive DB locks.
                inventory = collect_stopped_workers(args.job_id, docker=resources.docker, volumes=resources.VOLUMES,
                    scope=resources.SCOPE, project=resources.PROJECT, label=resources.LABEL, review_sha256=review_sha256)
                payload["worker_stop_proof"] = sign_inventory(inventory, values.get("PROJECT_COPILOT_PUBLIC_PROXY_SECRET") or "")
            compose = ["compose", "--env-file", str(args.env_file), "-f",
                       str(resources.ROOT / "docker-compose.public-demo.yml")]
            if args.personal_image:
                overlay = resources.ROOT / "deploy/personal-demo.image.yml"
                if overlay.is_symlink() or not overlay.is_file():
                    raise ValueError("reviewed personal image selection is missing")
                compose.extend(("-f", str(overlay)))
            delivered = json.loads(resources.docker(*compose, "run", "--rm", "--no-deps", "--pull", "never", "-T", "api",
                "python", "-m", "scripts.public_maintenance", "--internal",
                input=json.dumps(payload).encode("utf-8")))
            if delivered.get("status") != "ok":
                raise ValueError("maintenance container refused the request")
            result = delivered["result"]
        document = {"status": "ok", "result": result}
        if args.output is not None:
            write_private_output(args.output, document)
        print(json.dumps(document, ensure_ascii=False))
        return 0
    except Exception:
        # Provider messages, DSNs, receipts and credentials are never logged.
        print(json.dumps({"status": "error", "message": "maintenance refused; verify CLOSED, stopped workers, resource ownership and reviewed evidence"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
