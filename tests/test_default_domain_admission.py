"""Synthetic receipts only; never authorizes the real public database."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json

import pytest
from pydantic import HttpUrl

from project_copilot.services import entry_protection as entry
from project_copilot.services import release_control as release
from test_release_control import setup_release


def default_receipt(db, tmp_path, monkeypatch):
    settings, admin, alice, bob, evidence = setup_release(db, tmp_path, monkeypatch)
    settings = settings.model_copy(update={"auth_public_origin": HttpUrl("https://synthetic.edgeone.cool")})
    evidence.update(origin=str(settings.auth_public_origin).rstrip("/"),
                    configuration_fingerprint=release.config_fingerprint(settings))
    local = tmp_path / "local.json"
    cloud = tmp_path / "cloud.json"
    for path in (local, cloud):
        path.write_text('{"synthetic_test_only":true}')
    def artifact(path):
        return {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
    receipt = {"format": 1, "profile": entry.DEFAULT_DOMAIN_PROFILE, "origin": str(settings.auth_public_origin).rstrip("/"),
               "source_sha256": entry.source_fingerprint(), "cloud_runtime_artifact": artifact(cloud),
               "local_ingress_artifact": artifact(local), "risk_acknowledgement": entry.RISK_ACKNOWLEDGEMENT}
    path = tmp_path / "entry.json"
    path.write_text(json.dumps(receipt))
    evidence["checks"]["edge_protection"] = artifact(path)
    return settings, admin, alice, evidence, receipt, path


def test_default_domain_detection_does_not_match_suffix_attacks():
    assert entry.is_default_domain("https://demo.edgeone.cool")
    assert entry.is_default_domain("https://DEMO.EDGEONE.COOL")
    assert entry.is_default_domain("https://demo.edgeone.cool.")
    assert not entry.is_default_domain("https://edgeone.cool.evil.test")
    assert not entry.is_default_domain("https://notedgeone.cool")


def test_default_domain_needs_reviewed_entry_record(ticket_database, tmp_path, monkeypatch):
    settings, admin, alice, _, evidence = setup_release(ticket_database, tmp_path, monkeypatch)
    settings = settings.model_copy(update={"auth_public_origin": HttpUrl("https://synthetic.edgeone.cool")})
    evidence.update(origin=str(settings.auth_public_origin).rstrip("/"), configuration_fingerprint=release.config_fingerprint(settings))
    with pytest.raises(ValueError):
        release.validate_evidence(evidence, "STAGING", settings, datetime.now(timezone.utc))
    with ticket_database.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"


def test_reviewed_default_staging_stays_identity_bounded_and_expires(ticket_database, tmp_path, monkeypatch):
    settings, admin, alice, evidence, _, _ = default_receipt(ticket_database, tmp_path, monkeypatch)
    with ticket_database.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING",
                             reason="synthetic bounded admission only", evidence=evidence)
        row = release.require_open(session, settings, user_id=alice.id)
        assert row.evidence["entry_protection"]["profile"] == entry.DEFAULT_DOMAIN_PROFILE
        assert not row.worker_enabled
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=admin.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.flush()
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id)


@pytest.mark.parametrize("mutation", ["long_window", "risk", "source", "cloud_hash", "local_hash"])
def test_default_domain_invalid_evidence_stays_closed(ticket_database, tmp_path, monkeypatch, mutation):
    settings, admin, _, evidence, receipt, path = default_receipt(ticket_database, tmp_path, monkeypatch)
    if mutation == "long_window":
        evidence["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    elif mutation == "risk":
        receipt["risk_acknowledgement"] = "all costs protected"
    elif mutation == "source":
        receipt["source_sha256"]["cloud-functions/proxy.mjs"] = "0" * 64
    else:
        receipt["cloud_runtime_artifact" if mutation == "cloud_hash" else "local_ingress_artifact"]["sha256"] = "0" * 64
    path.write_text(json.dumps(receipt))
    evidence["checks"]["edge_protection"]["sha256"] = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        with ticket_database.transaction() as session:
            release.change_state(session, settings, actor_id=admin.id, operation="STAGING",
                                 reason="synthetic invalid proof rejection", evidence=evidence)
    with ticket_database.transaction() as session:
        assert release.release_status(session)["state"] == "CLOSED"


def test_public_is_rejected_even_with_all_checks_and_previous_staging(ticket_database, tmp_path, monkeypatch):
    settings, admin, alice, evidence, _, _ = default_receipt(ticket_database, tmp_path, monkeypatch)
    with ticket_database.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING",
                             reason="synthetic bounded stage only", evidence=evidence)
    public = deepcopy(evidence)
    public["state"] = "PUBLIC"
    public["checks"].update({key: evidence["checks"]["identity_isolation"] for key in release._PUBLIC - release._STAGING})
    with pytest.raises(ValueError, match="PUBLIC"):
        with ticket_database.transaction() as session:
            release.change_state(session, settings, actor_id=admin.id, operation="PUBLIC",
                                 reason="synthetic public should be rejected", evidence=public)
    with ticket_database.transaction() as session:
        row = release.require_open(session, settings, user_id=alice.id)
        # Defence in depth even if an externally modified row has all PUBLIC keys.
        row.state = "PUBLIC"
        row.evidence = {**row.evidence, "checks": {key: "a" * 64 for key in release._PUBLIC}}
        session.flush()
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id)


def test_proxy_or_ingress_drift_closes_authorized_default_domain(ticket_database, tmp_path, monkeypatch):
    settings, admin, alice, evidence, _, _ = default_receipt(ticket_database, tmp_path, monkeypatch)
    with ticket_database.transaction() as session:
        release.change_state(session, settings, actor_id=admin.id, operation="STAGING",
                             reason="synthetic entry source drift test", evidence=evidence)
    original = entry.source_fingerprint()
    monkeypatch.setattr(entry, "source_fingerprint", lambda: {**original, "cloud-functions/proxy.mjs": "0" * 64})
    with ticket_database.transaction() as session:
        with pytest.raises(release.ReleaseClosed):
            release.require_open(session, settings, user_id=alice.id)
