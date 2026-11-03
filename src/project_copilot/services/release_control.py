"""Explicit release admission, independent worker switch and execution leases.

Missing rows, stale code/evidence, expired authorization or unavailable services
all close admission. The operator CLI is the only supported state-writing path.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID

import httpx
from sqlalchemy import func, select, text

from project_copilot.config import PROJECT_ROOT
from project_copilot.db.release import ReleaseControl, ReleaseEvent
from project_copilot.db.models import UserAccount

RELEASE_ADMISSION_LOCK = 604116220001
WORKER_EXECUTION_LOCK = 604116220002
_LOCAL = frozenset({"identity_isolation", "quotas_limits", "storage_restore", "retention_recovery", "proxy_limits", "resource_isolation"})
_STAGING = _LOCAL | {"edge_protection", "restricted_access"}
_PUBLIC = _STAGING | {"public_e2e", "cookies_csrf", "offline_shutdown"}
_SECURITY_FIELDS = frozenset({
    "environment", "public_demo", "public_resource_id", "auth_public_origin",
    "auth_open_registration", "auth_session_ttl_seconds",
    "max_daily_requests_per_user", "max_daily_requests_global", "max_concurrent_requests_per_user",
    "max_provider_attempts_per_job", "public_job_queue_ttl_seconds",
    "public_upload_max_chunks", "public_upload_max_chars", "upload_max_bytes",
    "embedding_model", "embedding_expected_dimensions", "embedding_batch_size", "zhipu_base_url", "zhipu_timeout_seconds",
    "deepseek_base_url", "deepseek_model", "deepseek_timeout_seconds", "deepseek_temperature", "deepseek_max_tokens",
    "qdrant_url", "qdrant_collection_name", "qdrant_timeout_seconds", "manifest_dir", "rag_top_k",
    "tracing_enabled", "phoenix_endpoint", "phoenix_project", "trace_export_timeout_seconds",
    "database_connect_timeout_seconds", "database_pool_timeout_seconds",
    "agent_max_model_calls", "agent_max_tool_calls", "agent_tool_timeout_seconds",
    "agent_model_timeout_seconds", "agent_total_timeout_seconds",
    "final_rerank_device", "final_rerank_dtype", "final_rerank_timeout_seconds", "final_model_cache_dir",
})


class ReleaseClosed(PermissionError):
    pass


def config_fingerprint(settings) -> str:
    """Bind approved safety budgets/modes without persisting any secret value.

    Credentials are deliberately excluded so safe key rotation does not store
    password/key material in the release ledger. Resource identity and Docker
    preflight separately constrain the actual database and volume destination.
    """
    payload = settings.model_dump(mode="json", include=_SECURITY_FIELDS)
    if set(payload) != _SECURITY_FIELDS:
        raise ReleaseClosed("security configuration cannot be fingerprinted")
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def code_fingerprint(root: Path = PROJECT_ROOT) -> str:
    digest = sha256()
    paths = list((root / "src/project_copilot").rglob("*.py")) + list((root / "migrations").rglob("*.py"))
    # These exact backend artifacts are present at the same relative paths in
    # the built image and local checkout. Proxy/deployment evidence is bound
    # independently by reviewed artifact digests in the authorization document.
    paths += [root / "uv.lock"]
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def admission_exclusive(session):
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": RELEASE_ADMISSION_LOCK})


def release_status(session) -> dict:
    row = session.get(ReleaseControl, 1, populate_existing=True)
    return {"state": row.state if row else "CLOSED", "worker_enabled": bool(row and row.worker_enabled),
            "resource_id": row.resource_id if row else None, "expires_at": row.expires_at.isoformat() if row and row.expires_at else None}


def require_closed(session):
    row = session.get(ReleaseControl, 1, populate_existing=True)
    if row is not None and (row.state != "CLOSED" or row.worker_enabled):
        raise ReleaseClosed("maintenance requires CLOSED and worker disabled")


def require_open(session, settings, *, user_id=None, worker=False):
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": RELEASE_ADMISSION_LOCK})
    row = session.get(ReleaseControl, 1, populate_existing=True)
    now = session.scalar(select(func.clock_timestamp()))
    if (row is None or row.state not in {"STAGING", "PUBLIC"} or
            (worker and not row.worker_enabled) or row.expires_at is None or row.expires_at <= now or
            row.resource_id != settings.public_resource_id or not row.resource_id or
            row.origin != str(settings.auth_public_origin).rstrip("/") or
            row.code_fingerprint != code_fingerprint()):
        raise ReleaseClosed("release admission closed")
    required = _STAGING if row.state == "STAGING" else _PUBLIC
    if not isinstance(row.evidence, dict) or set(row.evidence.get("checks", {})) != required:
        raise ReleaseClosed("release evidence incomplete")
    if row.evidence.get("configuration_fingerprint") != config_fingerprint(settings):
        raise ReleaseClosed("approved safety configuration changed")
    if row.state == "STAGING" and user_id is not None and str(user_id) not in row.allowed_user_ids:
        raise ReleaseClosed("identity is outside staging allowlist")
    return row


def dependencies_ready(settings):
    """Read-only short probes; never generate model/API usage or warm BGE."""
    if not settings.database_url or not settings.public_resource_id or settings.auth_open_registration:
        raise ReleaseClosed("public dependencies not configured")
    for value in (settings.deepseek_api_key, settings.zhipu_api_key):
        if value is None or not value.get_secret_value().strip():
            raise ReleaseClosed("provider configuration unavailable")
    from project_copilot.rag.rerank import MODEL_REVISION
    snapshot = settings.final_model_cache_dir / "models--BAAI--bge-reranker-v2-m3" / "snapshots" / MODEL_REVISION
    if not all((snapshot / name).is_file() for name in ("config.json", "tokenizer.json", "model.safetensors")):
        raise ReleaseClosed("frozen model cache unavailable")
    if not settings.manifest_dir.is_dir():
        raise ReleaseClosed("manifest storage unavailable")
    with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client:
        response = client.get(str(settings.qdrant_url).rstrip("/") + "/readyz")
        if response.status_code != 200:
            raise ReleaseClosed("vector storage unavailable")
        if settings.tracing_enabled:
            from urllib.parse import urlsplit
            endpoint = urlsplit(str(settings.phoenix_endpoint))
            if client.get(f"{endpoint.scheme}://{endpoint.netloc}/healthz").status_code != 200:
                raise ReleaseClosed("trace storage unavailable")


@contextmanager
def execution_lease(database):
    """Session lock survives commits and is held until all runner work returns."""
    with database.engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock_shared(:key)"), {"key": WORKER_EXECUTION_LOCK})
        connection.commit()
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock_shared(:key)"), {"key": WORKER_EXECUTION_LOCK})
            connection.commit()


def validate_evidence(document: dict, state: str, settings, now: datetime) -> dict:
    """Verify immutable local artifacts, code/resource binding and expiry.

    Artifacts are administrator-reviewed test records, not automatically proven
    claims. External platform evidence must be collected in authorized staging.
    """
    required = _STAGING if state == "STAGING" else _PUBLIC
    if (not isinstance(document, dict) or document.get("format") != 1 or document.get("state") != state or
            document.get("resource_id") != settings.public_resource_id or
            document.get("code_fingerprint") != code_fingerprint() or
            document.get("configuration_fingerprint") != config_fingerprint(settings) or
            document.get("origin") != str(settings.auth_public_origin).rstrip("/")):
        raise ValueError("evidence binding invalid")
    verified = datetime.fromisoformat(document["verified_at"])
    expires = datetime.fromisoformat(document["expires_at"])
    if verified.tzinfo is None or expires.tzinfo is None or not now - timedelta(days=7) <= verified <= now or not now < expires <= now + timedelta(hours=24):
        raise ValueError("evidence expired or future-dated")
    checks = document.get("checks")
    if not isinstance(checks, dict) or set(checks) != required:
        raise ValueError("required evidence missing")
    normalized = {}
    for name, item in checks.items():
        if not isinstance(item, dict) or set(item) != {"path", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("invalid evidence artifact")
        path = Path(item["path"])
        if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("invalid evidence artifact file")
        if sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("evidence artifact changed")
        normalized[name] = item["sha256"]
    allowed = document.get("allowed_user_ids", [])
    if state == "STAGING" and (not isinstance(allowed, list) or not 1 <= len(allowed) <= 20 or len(set(allowed)) != len(allowed)):
        raise ValueError("staging identities required")
    allowed = [str(UUID(value)) for value in allowed]
    return {"checks": normalized, "allowed_user_ids": allowed, "expires_at": expires,
            "evidence_hash": sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()}


def change_state(session, settings, *, actor_id, operation, reason, evidence=None):
    """Operator-only service; no HTTP/model tool endpoint exposes this function."""
    if operation not in {"CLOSED", "STAGING", "PUBLIC", "worker-enable", "worker-disable"} or not 8 <= len(reason) <= 1000:
        raise ValueError("invalid release operation")
    admission_exclusive(session)
    # FOR SHARE prevents demotion/deactivation through commit but remains
    # compatible with an in-flight upload's same role-stability lock. Taking
    # FOR UPDATE here after admission_exclusive would deadlock that upload's
    # next provider-boundary admission check.
    admin = session.scalar(select(UserAccount).where(UserAccount.id == actor_id).with_for_update(read=True)
                           .execution_options(populate_existing=True))
    if admin is None or not admin.active or admin.role != "admin":
        raise ReleaseClosed("active administrator required")
    row = session.get(ReleaseControl, 1, with_for_update=True)
    if row is None:
        row = ReleaseControl(id=1, state="CLOSED", worker_enabled=False)
        session.add(row)
        session.flush()
    before = row.state
    event_hash = None
    if operation == "CLOSED":
        row.state, row.worker_enabled = "CLOSED", False
    elif operation == "worker-disable":
        row.worker_enabled = False
    elif operation == "worker-enable":
        require_open(session, settings)
        dependencies_ready(settings)
        row.worker_enabled = True
    else:
        if operation == "PUBLIC" and before != "STAGING":
            raise ValueError("PUBLIC requires an independently verified STAGING release")
        now = session.scalar(select(func.clock_timestamp()))
        checked = validate_evidence(evidence, operation, settings, now)
        for user_id in checked["allowed_user_ids"]:
            user = session.get(UserAccount, UUID(user_id))
            if user is None or not user.active:
                raise ValueError("staging user is unavailable")
        dependencies_ready(settings)
        row.state, row.worker_enabled = operation, False
        row.resource_id, row.origin = settings.public_resource_id, str(settings.auth_public_origin).rstrip("/")
        row.code_fingerprint, row.expires_at = code_fingerprint(), checked["expires_at"]
        row.allowed_user_ids = checked["allowed_user_ids"]
        row.evidence = {"checks": checked["checks"], "configuration_fingerprint": config_fingerprint(settings)}
        event_hash = checked["evidence_hash"]
    row.updated_at = session.scalar(select(func.clock_timestamp()))
    session.add(ReleaseEvent(actor_user_id=actor_id, operation=operation, before_state=before,
                             after_state=row.state, reason=reason, evidence_hash=event_hash))
    session.flush()
    return release_status(session)
