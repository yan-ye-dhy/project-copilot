"""Verify short-lived host-generated Docker stop inventory for manual recovery.

The signer runs only on the trusted host with Docker access. No HTTP or model
tool exposes signing. The provider reconciliation remains an operator review.
"""
from datetime import datetime, timedelta
from hashlib import sha256
import hmac
import json
import re


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sign_inventory(inventory: dict, secret: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", secret):
        raise ValueError("invalid stop-inventory signing configuration")
    signature = hmac.new(secret.encode("ascii"), b"pc-worker-stop-v1\0" + canonical(inventory).encode(), sha256).hexdigest()
    return {"inventory": inventory, "signature": signature}


def verify_inventory(proof, *, secret, job_id, job_created_at, resource_id, now, review_sha256):
    if not isinstance(proof, dict) or set(proof) != {"inventory", "signature"}:
        raise ValueError("machine-verified worker stop inventory required")
    supplied = proof["signature"]
    if not isinstance(supplied, str) or not re.fullmatch("[0-9a-f]{64}", supplied):
        raise ValueError("invalid worker stop signature")
    expected = sign_inventory(proof["inventory"], secret)["signature"]
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("worker stop signature mismatch")
    inventory = proof["inventory"]
    if (not isinstance(inventory, dict) or set(inventory) != {"version", "job_id", "resource_id", "captured_at", "postgres_volume", "workers", "review_sha256"} or
            inventory["version"] != 1 or inventory["job_id"] != str(job_id) or inventory["resource_id"] != resource_id or
            inventory["resource_id"] != "public-demo-v2" or inventory["postgres_volume"] != "pc-public-v2-postgres" or
            not isinstance(review_sha256, str) or not re.fullmatch("[0-9a-f]{64}", review_sha256) or
            inventory["review_sha256"] != review_sha256):
        raise ValueError("worker stop inventory resource binding mismatch")
    captured = datetime.fromisoformat(inventory["captured_at"])
    if captured.tzinfo is None or not now - timedelta(seconds=60) <= captured <= now + timedelta(seconds=2):
        raise ValueError("worker stop inventory expired")
    workers = inventory["workers"]
    if not isinstance(workers, list) or not 1 <= len(workers) <= 100:
        raise ValueError("no verifiable stopped worker")
    finished_after_job = False
    seen = set()
    for worker in workers:
        if (not isinstance(worker, dict) or set(worker) != {"container_id", "status", "pid", "running", "restarting", "finished_at"} or
                not isinstance(worker["container_id"], str) or not re.fullmatch("[0-9a-f]{64}", worker["container_id"]) or
                worker["container_id"] in seen or worker["status"] != "exited" or worker["pid"] != 0 or
                worker["running"] is not False or worker["restarting"] is not False):
            raise ValueError("worker termination was not proved")
        seen.add(worker["container_id"])
        finished = datetime.fromisoformat(worker["finished_at"].replace("Z", "+00:00"))
        if finished.tzinfo is None or finished > captured:
            raise ValueError("invalid worker termination time")
        finished_after_job |= finished >= job_created_at
    if not finished_after_job:
        raise ValueError("worker was not verified stopped after this job began")
    return sha256(canonical(proof).encode()).hexdigest()
