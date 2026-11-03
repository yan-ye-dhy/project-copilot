"""Host inventory and signed recovery proof are synthetic; no Docker/model calls."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from scripts.public_maintenance import collect_stopped_workers, verify_review_artifact
from project_copilot.services.worker_stop import sign_inventory, verify_inventory


def inventory(job_id):
    now = datetime.now(timezone.utc)
    return {"version": 1, "job_id": str(job_id), "resource_id": "public-demo-v2",
        "captured_at": now.isoformat(), "postgres_volume": "pc-public-v2-postgres",
        "review_sha256": "e" * 64,
        "workers": [{"container_id": "f" * 64, "status": "exited", "pid": 0, "running": False,
                     "restarting": False, "finished_at": (now - timedelta(seconds=1)).isoformat()}]}


def verify(proof, job_id):
    return verify_inventory(proof, secret="a" * 32, job_id=job_id,
        job_created_at=datetime.now(timezone.utc) - timedelta(hours=1), resource_id="public-demo-v2",
        now=datetime.now(timezone.utc), review_sha256="e" * 64)


def test_signature_and_job_binding_cannot_be_replaced_by_boolean_attestation():
    job_id = uuid4()
    proof = sign_inventory(inventory(job_id), "a" * 32)
    assert len(verify(proof, job_id)) == 64
    with pytest.raises(ValueError):
        verify({"original_execution_terminated": True}, job_id)
    with pytest.raises(ValueError):
        verify(proof, uuid4())
    changed = deepcopy(proof)
    changed["inventory"]["workers"][0]["running"] = True
    with pytest.raises(ValueError):
        verify(changed, job_id)


@pytest.mark.parametrize("mutation", ("stale", "running", "pid", "no_workers", "wrong_volume", "before_job", "wrong_review"))
def test_even_signed_invalid_or_stale_inventory_fails_closed(mutation):
    job_id = uuid4()
    data = inventory(job_id)
    if mutation == "stale":
        data["captured_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    elif mutation == "running":
        data["workers"][0]["running"] = True
    elif mutation == "pid":
        data["workers"][0]["pid"] = 123
    elif mutation == "no_workers":
        data["workers"] = []
    elif mutation == "wrong_volume":
        data["postgres_volume"] = "production-postgres"
    elif mutation == "wrong_review":
        data["review_sha256"] = "c" * 64
    else:
        data["workers"][0]["finished_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with pytest.raises(ValueError):
        verify(sign_inventory(data, "a" * 32), job_id)


@pytest.mark.parametrize("untrusted", (False, True))
def test_host_inventory_checks_all_manifest_consumers(untrusted):
    expected = {"Id": "f" * 64, "Config": {"Labels": {
        "com.docker.compose.project": "project-copilot-public-demo", "com.docker.compose.service": "worker",
        "io.project-copilot.scope": "public-demo-v2"}},
        "State": {"Status": "exited", "Pid": 0, "Running": False, "Restarting": False,
                  "FinishedAt": datetime.now(timezone.utc).isoformat()}}
    if untrusted:
        expected["Config"]["Labels"]["com.docker.compose.project"] = "other-project"

    def fake_docker(*args):
        if args[0] == "ps":
            assert args[-1] == "volume=pc-public-v2-manifests"
            return b"container-id\n"
        assert args == ("inspect", "container-id")
        return json.dumps([expected]).encode()

    call = lambda: collect_stopped_workers(uuid4(), docker=fake_docker,
        volumes={"manifests": "pc-public-v2-manifests", "postgres_data": "pc-public-v2-postgres"},
        scope="public-demo-v2", project="project-copilot-public-demo", label="io.project-copilot.scope", review_sha256="e" * 64)
    if untrusted:
        with pytest.raises(ValueError):
            call()
    else:
        assert call()["workers"][0]["pid"] == 0


def test_review_artifact_hashes_private_bytes_and_refuses_changed_or_missing_file(tmp_path):
    from hashlib import sha256

    artifact = tmp_path / "provider-and-business-review.txt"
    content = b"Synthetic provider terminal receipt and verified database binding."
    artifact.write_bytes(content)
    artifact.chmod(0o600)
    digest = sha256(content).hexdigest()
    receipt = {"evidence_reference_sha256": digest}
    assert verify_review_artifact(artifact, receipt) == digest
    artifact.write_bytes(b"Changed review")
    with pytest.raises(ValueError):
        verify_review_artifact(artifact, receipt)
    with pytest.raises(OSError):
        verify_review_artifact(tmp_path / "missing", receipt)


@pytest.mark.parametrize("kind", ("public_permissions", "symlink", "empty", "oversized"))
def test_review_artifact_is_private_regular_and_bounded(tmp_path, kind):
    from hashlib import sha256

    artifact = tmp_path / "review"
    artifact.write_bytes(b"synthetic review")
    artifact.chmod(0o600)
    if kind == "public_permissions":
        artifact.chmod(0o644)
    elif kind == "symlink":
        target = tmp_path / "target"
        artifact.rename(target)
        artifact.symlink_to(target)
    elif kind == "empty":
        artifact.write_bytes(b"")
    else:
        artifact.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises((ValueError, OSError)):
        verify_review_artifact(artifact, {"evidence_reference_sha256": sha256(b"synthetic review").hexdigest()})
