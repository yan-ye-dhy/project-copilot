"""Backup safety and retention without touching Docker or private data."""
import io
import json
from pathlib import Path
import tarfile

import pytest

from scripts import public_backup as backup
from scripts.public_resources import ResourceError


@pytest.mark.parametrize("name,kind", [("../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE), ("pg_tblspc/link", tarfile.SYMTYPE)])
def test_restore_rejects_paths_and_links(tmp_path, name, kind):
    path = tmp_path / "dangerous.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        item = tarfile.TarInfo(name)
        item.type = kind
        if kind == tarfile.SYMTYPE:
            item.linkname = "/outside"
        archive.addfile(item)
    with pytest.raises(ResourceError):
        backup.validate_archive(path)


def test_restore_validates_hash_before_any_volume_creation(tmp_path, monkeypatch):
    files = {f"{role}.tar.gz": {"sha256": "0" * 64, "bytes": 1} for role in backup.ROLES}
    (tmp_path / "backup.json").write_text(json.dumps({"format": 1, "scope": backup.SCOPE, "files": files}))
    for name in files:
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(backup, "docker", lambda *args, **kwargs: pytest.fail("must not reach Docker"))
    with pytest.raises(ResourceError):
        backup.restore(tmp_path)


def make_set(root, index, *, verified=True, hold=False):
    path = root / str(index)
    path.mkdir()
    files = {}
    for role in backup.ROLES:
        file = path / f"{role}.tar.gz"
        file.write_bytes(b"x" * 10)
        files[file.name] = {"sha256": backup.digest(file), "bytes": 10}
    (path / "backup.json").write_text(json.dumps({"format": 1, "scope": backup.SCOPE, "files": files,
        "verified": verified, "diagnostic_hold": hold, "created_at": f"2026-11-{index:02d}"}))
    return path


def test_retention_keeps_latest_seven_verified_and_all_unverified_or_held(tmp_path):
    for i in range(1, 10):
        make_set(tmp_path, i)
    unverified = make_set(tmp_path, 10, verified=False)
    held = make_set(tmp_path, 11, hold=True)
    result = backup.prune(tmp_path)
    assert result["removed_verified_sets"] == 2
    assert not (tmp_path / "1").exists() and not (tmp_path / "2").exists()
    assert unverified.exists() and held.exists()


def test_capacity_does_not_delete_unknown_evidence(tmp_path):
    path = make_set(tmp_path, 1, verified=False)
    with pytest.raises(ResourceError):
        backup.prune(tmp_path, max_bytes=1)
    assert path.exists()


def test_unknown_extra_diagnostic_file_blocks_pruning(tmp_path):
    path = make_set(tmp_path, 1)
    (path / "investigation.txt").write_text("keep this evidence")
    with pytest.raises(ResourceError):
        backup.prune(tmp_path, max_bytes=1)
    assert (path / "investigation.txt").exists()


def test_changed_restore_manifest_cannot_be_marked_verified(tmp_path, monkeypatch):
    path = make_set(tmp_path, 1, verified=False)
    plan = path / "plan.json"
    plan.write_text(json.dumps({"backup_identity": "not-the-backup"}))
    monkeypatch.setattr(backup, "validate_env", lambda *_: {})
    monkeypatch.setattr(backup, "docker", lambda *_: pytest.fail("must not start any container"))
    with pytest.raises(ResourceError):
        backup.verify_restored(tmp_path / "private.env", path, plan)
    assert json.loads((path / "backup.json").read_text())["diagnostic_hold"] is True


def test_unrecognized_partial_capture_counts_against_capacity(tmp_path):
    partial = tmp_path / "capture-incomplete"
    partial.mkdir()
    (partial / "FAILED").write_bytes(b"x" * 20)
    with pytest.raises(ResourceError):
        backup.prune(tmp_path, max_bytes=1)
    assert (partial / "FAILED").exists()
