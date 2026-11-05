"""Actual local OS lock proofs; no database, Docker or supplier calls."""
from pathlib import Path
from uuid import uuid4

import pytest

from project_copilot.services.execution_slots import ExecutionLease, ExecutionProofError


def test_held_os_lock_cannot_be_reacquired_even_in_same_process(tmp_path):
    lease = ExecutionLease.create(tmp_path, uuid4())
    try:
        assert lease.valid_owned()
        assert ExecutionLease.try_acquire(tmp_path, lease.job_id, lease.token, lease.inode) is None
    finally:
        lease.close()
    with ExecutionLease.try_acquire(tmp_path, lease.job_id, lease.token, lease.inode) as recovered:
        assert recovered.valid_owned()
    assert lease.path.exists()  # Evidence is never deleted/recreated on release.


@pytest.mark.parametrize("alteration", ["missing", "header", "inode", "symlink", "public"])
def test_missing_or_changed_lease_is_not_proof_of_termination(tmp_path, alteration):
    lease = ExecutionLease.create(tmp_path, uuid4())
    lease.close()
    expected_inode = lease.inode
    if alteration == "missing":
        lease.path.unlink()
    elif alteration == "header":
        lease.path.write_text("untrusted lease body")
    elif alteration == "inode":
        expected_inode += 1
    elif alteration == "symlink":
        original = tmp_path / "original"
        lease.path.rename(original)
        lease.path.symlink_to(original)
    else:
        lease.path.chmod(0o644)
    assert ExecutionLease.try_acquire(tmp_path, lease.job_id, lease.token, expected_inode) is None


def test_shared_directory_and_symlink_root_cannot_supply_proof(tmp_path):
    root = tmp_path / "manifest"
    lease = ExecutionLease.create(root, uuid4())
    lease.close()
    (root / ".job-executions").chmod(0o755)
    assert ExecutionLease.try_acquire(root, lease.job_id, lease.token, lease.inode) is None
    with pytest.raises(ExecutionProofError):
        ExecutionLease.create(root, uuid4())
    linked = tmp_path / "linked"
    linked.symlink_to(root, target_is_directory=True)
    with pytest.raises(ExecutionProofError):
        ExecutionLease.create(linked, uuid4())


def test_replaced_path_while_active_invalidates_lease_without_releasing_original_lock(tmp_path):
    lease = ExecutionLease.create(tmp_path, uuid4())
    try:
        original = lease.path.with_suffix(".original")
        lease.path.rename(original)
        lease.path.write_bytes(original.read_bytes())
        lease.path.chmod(0o600)
        assert not lease.valid_owned()
        assert ExecutionLease.try_acquire(tmp_path, lease.job_id, lease.token, lease.inode) is None
    finally:
        lease.close()
