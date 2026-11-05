"""Default-domain admission: bounded staging is not a public release permit.

Artifacts prove reviewed byte identity, not the truth of a platform claim. An
administrator still has to review actual Node runtime and local ingress tests.
No function-side code can undo the invocation that has already happened.
"""
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlsplit

from project_copilot.config import PROJECT_ROOT

DEFAULT_DOMAIN_PROFILE = "default-domain-bounded-v1"
RISK_ACKNOWLEDGEMENT = "Cloud Function invocations remain unprotected; accept only time-bounded restricted STAGING"
ENTRY_SOURCES = (
    "cloud-functions/proxy.mjs", "cloud-functions/api/[[default]].js", "edgeone.json",
    "deploy/nginx/public-demo.conf", "deploy/nginx/Dockerfile", "docker-compose.public-demo.yml",
)


def is_default_domain(origin: str) -> bool:
    # DNS FQDN spelling with a trailing dot must not bypass this profile.
    host = (urlsplit(origin).hostname or "").lower().rstrip(".")
    return host == "edgeone.cool" or host.endswith(".edgeone.cool")


def source_fingerprint(root: Path = PROJECT_ROOT) -> dict[str, str]:
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in ENTRY_SOURCES}


def checked_artifact(item: dict) -> str:
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        raise ValueError("invalid entry protection artifact")
    path = Path(item["path"])
    if (not path.is_absolute() or path.is_symlink() or not path.is_file() or
            not 0 < path.stat().st_size <= 8 * 1024 * 1024):
        raise ValueError("invalid entry protection file")
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != item["sha256"]:
        raise ValueError("entry protection artifact changed")
    return digest


def validate_default_domain(document: dict, state: str, settings, now) -> dict | None:
    if not is_default_domain(str(settings.auth_public_origin)):
        return None
    if state != "STAGING":
        raise ValueError("default domain PUBLIC is not supported by this protection profile")
    # Deliberately read the actual edge_protection artifact. A top-level boolean
    # or ordinary env flag must not substitute for a reviewed test record.
    item = document.get("checks", {}).get("edge_protection")
    checked_artifact(item)
    receipt = json.loads(Path(item["path"]).read_text(encoding="utf-8"))
    expected = {"format", "profile", "origin", "source_sha256", "cloud_runtime_artifact",
                "local_ingress_artifact", "risk_acknowledgement"}
    if (not isinstance(receipt, dict) or set(receipt) != expected or receipt["format"] != 1 or
            receipt["profile"] != DEFAULT_DOMAIN_PROFILE or
            receipt["origin"] != str(settings.auth_public_origin).rstrip("/") or
            receipt["source_sha256"] != source_fingerprint() or
            receipt["risk_acknowledgement"] != RISK_ACKNOWLEDGEMENT):
        raise ValueError("reviewed default-domain protection receipt required")
    from datetime import datetime, timedelta
    expiry = datetime.fromisoformat(document["expires_at"])
    if expiry > now + timedelta(hours=2):
        raise ValueError("default domain STAGING authorization must not exceed two hours")
    return {"profile": DEFAULT_DOMAIN_PROFILE, "source_sha256": receipt["source_sha256"],
            "cloud_runtime_sha256": checked_artifact(receipt["cloud_runtime_artifact"]),
            "local_ingress_sha256": checked_artifact(receipt["local_ingress_artifact"]),
            "risk_acknowledgement": RISK_ACKNOWLEDGEMENT}


def check_default_domain_runtime(state: str, evidence: dict, origin: str):
    if not is_default_domain(origin):
        return
    protection = evidence.get("entry_protection")
    if (state != "STAGING" or not isinstance(protection, dict) or
            protection.get("profile") != DEFAULT_DOMAIN_PROFILE or
            protection.get("source_sha256") != source_fingerprint() or
            protection.get("risk_acknowledgement") != RISK_ACKNOWLEDGEMENT):
        raise ValueError("default-domain admission is not approved or has changed")
