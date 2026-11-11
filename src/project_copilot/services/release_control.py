"""Explicit release admission, independent worker switch and execution leases.

Missing rows, stale code/evidence, expired authorization or unavailable services
all close admission. The operator CLI is the only supported state-writing path.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from project_copilot.config import PROJECT_ROOT
from project_copilot.db.release import ReleaseControl, ReleaseEvent
from project_copilot.db.models import AiRequestReservation, UserAccount
from project_copilot.services.personal_profile import (PERSONAL_JOB_LIMIT, PERSONAL_MAX_MINUTES,
    PERSONAL_PROFILE, PERSONAL_SETTINGS_FIELDS, personal_limits)

RELEASE_ADMISSION_LOCK = 604116220001
WORKER_EXECUTION_LOCK = 604116220002
PERSONAL_DEMO = "PERSONAL_DEMO"
_LOCAL = frozenset({"identity_isolation", "quotas_limits", "storage_restore", "retention_recovery", "proxy_limits", "resource_isolation"})
_STAGING = _LOCAL | {"edge_protection", "restricted_access"}
_PUBLIC = _STAGING | {"public_e2e", "cookies_csrf", "offline_shutdown"}
_SECURITY_FIELDS = PERSONAL_SETTINGS_FIELDS | frozenset({
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
    if getattr(settings, "queue_max_pending_jobs", 0) or getattr(settings, "worker_max_active_jobs", 0):
        payload["queue_capacity"] = {name: getattr(settings, name) for name in
                                     ("queue_max_pending_jobs", "worker_max_active_jobs")}
    if getattr(settings, "database_pool_size", 5) != 5 or getattr(settings, "database_max_overflow", 5) != 5:
        payload["database_pool"] = {"size": settings.database_pool_size, "overflow": settings.database_max_overflow}
    if getattr(settings, "redis_enabled", False):
        from urllib.parse import urlsplit
        # Bind an enabled backend's safety configuration; credentials remain
        # excluded. Disabled mode retains the previous fingerprint contract.
        fields = {name for name in type(settings).model_fields if name.startswith("redis_")
                  and name not in {"redis_url", "redis_cache_hmac_key"}}
        payload["redis"] = settings.model_dump(mode="json", include=fields)
        target = urlsplit(settings.redis_url.get_secret_value())
        payload["redis"]["target"] = sha256(
            f"{target.scheme}://{target.hostname}:{target.port}{target.path}".encode()).hexdigest()
    if getattr(settings, "message_queue_enabled", False):
        from urllib.parse import urlsplit
        fields = {name for name in type(settings).model_fields
                  if name.startswith(("rabbitmq_", "outbox_")) and name != "rabbitmq_url"}
        payload["messaging"] = settings.model_dump(mode="json", include=fields)
        target = urlsplit(settings.rabbitmq_url.get_secret_value())
        payload["messaging"]["target"] = sha256(
            f"{target.scheme}://{target.hostname}:{target.port}{target.path}".encode()).hexdigest()
    if getattr(settings, "job_recovery_enabled", False):
        fields = {name for name in type(settings).model_fields if name.startswith("job_recovery_")}
        payload["job_recovery"] = settings.model_dump(mode="json", include=fields)
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
    result = {"state": row.state if row else "CLOSED", "worker_enabled": bool(row and row.worker_enabled),
            "resource_id": row.resource_id if row else None, "expires_at": row.expires_at.isoformat() if row and row.expires_at else None}
    if row is not None and row.state == PERSONAL_DEMO:
        now = session.scalar(select(func.clock_timestamp()))
        result["expired"] = row.expires_at is None or row.expires_at <= now
        grant = (row.evidence or {}).get("grant_id")
        try:
            grant = UUID(grant)
        except (ValueError, TypeError, AttributeError):
            grant = None
        result["personal_demo"] = {"job_limit": PERSONAL_JOB_LIMIT,
            "jobs_used": session.scalar(select(func.count()).select_from(AiRequestReservation)
                .where(AiRequestReservation.personal_demo_grant_id == grant)) if grant else None,
            "allowed_user_ids": row.allowed_user_ids}
    return result


def require_closed(session):
    row = session.get(ReleaseControl, 1, populate_existing=True)
    if row is not None and (row.state != "CLOSED" or row.worker_enabled):
        raise ReleaseClosed("maintenance requires CLOSED and worker disabled")


def personal_settings(settings):
    """Only the dedicated public-demo configuration may use this opt-in mode."""
    if (not settings.public_demo or settings.environment != "production" or
            settings.public_resource_id != "public-demo-v2" or settings.auth_open_registration or
            settings.database_url is None):
        raise ReleaseClosed("personal demo requires isolated public settings")
    url = make_url(settings.database_url.get_secret_value())
    if (url.drivername != "postgresql+psycopg" or url.host != "postgres" or url.port != 5432 or
            url.username != "project_copilot_public" or url.database != "project_copilot_public" or url.query):
        raise ReleaseClosed("personal demo requires the dedicated database")


def _personal_admission(session, row, settings, now, user_id, reservation):
    personal_settings(settings)
    evidence = row.evidence
    from project_copilot.services.entry_protection import source_fingerprint
    try:
        if (not isinstance(evidence, dict) or set(evidence) != {"profile", "grant_id", "started_at", "configuration_fingerprint", "source_sha256", "limits"} or
                evidence["profile"] != PERSONAL_PROFILE or evidence["limits"] != personal_limits(settings)
                or evidence["source_sha256"] != source_fingerprint() or
                not isinstance(row.allowed_user_ids, list) or len(row.allowed_user_ids) != 1):
            raise ValueError()
        grant = UUID(evidence["grant_id"])
        allowed = UUID(row.allowed_user_ids[0])
        started = datetime.fromisoformat(evidence["started_at"])
        if started.tzinfo is None or not started <= now < row.expires_at <= started + timedelta(minutes=PERSONAL_MAX_MINUTES):
            raise ValueError()
        user = session.get(UserAccount, allowed, populate_existing=True)
        if user is None or not user.active or (user_id is not None and user_id != allowed):
            raise ValueError()
        if reservation is not None and (reservation.personal_demo_grant_id != grant or reservation.user_id != allowed):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OSError, AttributeError):
        raise ReleaseClosed("personal demo authorization unavailable") from None


def require_open(session, settings, *, user_id=None, worker=False, reservation=None):
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": RELEASE_ADMISSION_LOCK})
    row = session.get(ReleaseControl, 1, populate_existing=True)
    now = session.scalar(select(func.clock_timestamp()))
    if (row is None or row.state not in {"STAGING", "PUBLIC", PERSONAL_DEMO} or
            (worker and not row.worker_enabled) or row.expires_at is None or row.expires_at <= now or
            row.resource_id != settings.public_resource_id or not row.resource_id or
            row.origin != str(settings.auth_public_origin).rstrip("/") or
            row.code_fingerprint != code_fingerprint()):
        raise ReleaseClosed("release admission closed")
    if not isinstance(row.evidence, dict) or row.evidence.get("configuration_fingerprint") != config_fingerprint(settings):
        raise ReleaseClosed("approved safety configuration changed")
    if row.state == PERSONAL_DEMO:
        _personal_admission(session, row, settings, now, user_id, reservation)
        return row
    if reservation is not None and reservation.personal_demo_grant_id is not None:
        raise ReleaseClosed("personal task belongs to an ended personal grant")
    required = _STAGING if row.state == "STAGING" else _PUBLIC
    if set(row.evidence.get("checks", {})) != required:
        raise ReleaseClosed("release evidence incomplete")
    from project_copilot.services.entry_protection import check_default_domain_runtime
    try:
        check_default_domain_runtime(row.state, row.evidence, str(settings.auth_public_origin))
    except (ValueError, OSError):
        raise ReleaseClosed("default domain entry protection is unavailable") from None
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
    from project_copilot.services.entry_protection import validate_default_domain
    entry_protection = validate_default_domain(document, state, settings, now)
    return {"checks": normalized, "allowed_user_ids": allowed, "expires_at": expires,
            "entry_protection": entry_protection,
            "evidence_hash": sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()}


def change_state(session, settings, *, actor_id, operation, reason, evidence=None,
                 personal_user_id=None, duration_minutes=PERSONAL_MAX_MINUTES):
    """Operator-only service; no HTTP/model tool endpoint exposes this function."""
    if operation not in {"CLOSED", "STAGING", "PUBLIC", PERSONAL_DEMO, "worker-enable", "worker-disable"} or not 8 <= len(reason) <= 1000:
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
        if before in {PERSONAL_DEMO, "CLOSED"} and isinstance(row.evidence, dict) and row.evidence.get("profile") == PERSONAL_PROFILE:
            # A v2 grant cannot resume its queue in a later window. Only jobs
            # proven never claimed are ended by this explicit administrator
            # close; old history/running/unknown work is deliberately excluded.
            try:
                with session.begin_nested():
                    _end_unclaimed_personal_queue(session, row)
            except SQLAlchemyError as exc:
                # Emergency CLOSED must not depend on queue maintenance. The
                # savepoint preserves all held evidence when reconciliation fails.
                logging.getLogger(__name__).warning("personal queue close unavailable error_type=%s", type(exc).__name__)
        row.state, row.worker_enabled = "CLOSED", False
    elif operation == "worker-disable":
        row.worker_enabled = False
    elif operation == "worker-enable":
        require_open(session, settings)
        dependencies_ready(settings)
        row.worker_enabled = True
    elif operation == PERSONAL_DEMO:
        personal_settings(settings)
        if (before != "CLOSED" or row.worker_enabled or evidence is not None or
                not isinstance(personal_user_id, UUID) or type(duration_minutes) is not int or
                not 1 <= duration_minutes <= PERSONAL_MAX_MINUTES):
            raise ValueError("personal demo requires CLOSED, one existing user and 1-1440 minutes")
        user = session.scalar(select(UserAccount).where(UserAccount.id == personal_user_id)
                              .with_for_update(read=True).execution_options(populate_existing=True))
        if user is None or not user.active:
            raise ValueError("personal test user unavailable")
        dependencies_ready(settings)
        now = session.scalar(select(func.clock_timestamp()))
        from project_copilot.services.entry_protection import source_fingerprint
        row.state, row.worker_enabled = PERSONAL_DEMO, False
        row.resource_id, row.origin = settings.public_resource_id, str(settings.auth_public_origin).rstrip("/")
        row.code_fingerprint = code_fingerprint()
        row.expires_at = now + timedelta(minutes=duration_minutes)
        row.allowed_user_ids = [str(personal_user_id)]
        row.evidence = {"profile": PERSONAL_PROFILE, "grant_id": str(uuid4()), "started_at": now.isoformat(),
                        "configuration_fingerprint": config_fingerprint(settings), "source_sha256": source_fingerprint(),
                        "limits": personal_limits(settings)}
        event_hash = sha256(json.dumps({"personal": row.evidence, "user": str(personal_user_id),
                                      "expires_at": row.expires_at.isoformat()}, sort_keys=True).encode()).hexdigest()
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
        if checked["entry_protection"] is not None:
            row.evidence["entry_protection"] = checked["entry_protection"]
        event_hash = checked["evidence_hash"]
    row.updated_at = session.scalar(select(func.clock_timestamp()))
    session.add(ReleaseEvent(actor_user_id=actor_id, operation=operation, before_state=before,
                             after_state=row.state, reason=reason, evidence_hash=event_hash))
    session.flush()
    return release_status(session)


def _end_unclaimed_personal_queue(session, control):
    from project_copilot.db.executions import AiJobExecution
    from project_copilot.db.jobs import AiJob, AiJobUpload
    from project_copilot.services.job_events import append_event
    try:
        grant = UUID(control.evidence["grant_id"])
        if not isinstance(control.allowed_user_ids, list) or len(control.allowed_user_ids) != 1:
            return
        owner = UUID(control.allowed_user_ids[0])
    except (ValueError, TypeError, KeyError, AttributeError):
        return  # Close admission even when the old queue identity is malformed.
    rows = session.scalars(select(AiRequestReservation).where(
        AiRequestReservation.personal_demo_grant_id == grant,
        AiRequestReservation.user_id == owner,
        AiRequestReservation.state == "reserved", AiRequestReservation.provider_attempts == 0,
        AiRequestReservation.slot_released_at.is_(None),
        ~select(AiJobExecution.job_id).where(AiJobExecution.job_id == AiRequestReservation.id).exists(),
    ).order_by(AiRequestReservation.id).with_for_update()).all()
    now = session.scalar(select(func.clock_timestamp()))
    for request in rows:
        job = session.get(AiJob, request.id)
        if job is not None and job.result is not None:
            continue  # Inconsistent history needs investigation, not guessed cancellation.
        request.state, request.finished_at, request.slot_released_at = "failed", now, now
        if job is not None:
            job.failure_type = "grant_ended"
            attachment = session.get(AiJobUpload, request.id)
            if attachment is not None:
                session.delete(attachment)  # Only transient, never-claimed bytes.
            append_event(session, request.id, "diagnostic", {"failure_type": "grant_ended"})
            append_event(session, request.id, "terminal", {"status": "failed"})
