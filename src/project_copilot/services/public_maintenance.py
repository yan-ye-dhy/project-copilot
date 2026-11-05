"""Explicit administrator maintenance; uncertain jobs are never retried.

Retention deliberately keeps proposals, tickets, reservation identities, recovery
records and unknown/running payloads. WAL and backups have separate retention.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
from uuid import UUID, uuid4

from sqlalchemy import exists, func, select, text

from project_copilot.db.jobs import AiJob, AiJobUpload, AiRequestAttempt, AiJobEvent
from project_copilot.db.models import AgentContext, AiRequestReservation, LoginSession, PendingAction, Ticket, UserAccount
from project_copilot.db.operations import JobContentTombstone, JobRecoveryAudit, MaintenanceAudit

# Shared with services.release_control. A worker holds WORKER_EXECUTION_LOCK
# throughout execution; closing ingress alone is insufficient to prove quiescence.
RELEASE_ADMISSION_LOCK = 604116220001
WORKER_EXECUTION_LOCK = 604116220002


class MaintenanceRejected(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _require_admin(session, actor_user_id):
    actor = session.scalar(select(UserAccount).where(UserAccount.id == actor_user_id,
        UserAccount.role == "admin", UserAccount.active.is_(True)).with_for_update(read=True))
    if actor is None:
        raise MaintenanceRejected("active administrator required")


@contextmanager
def closed_quiescent(session):
    """No claim/provider admission or release transition can race this operation.

    The nonblocking transaction lock also proves every cooperating worker has
    released its execution lease. Operators must additionally verify containers
    and upstream completion; database locks cannot inspect a remote provider.
    """
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": RELEASE_ADMISSION_LOCK}):
        raise MaintenanceRejected("release admission is busy")
    from project_copilot.services.release_control import ReleaseClosed, require_closed
    try:
        require_closed(session)
    except ReleaseClosed:
        raise MaintenanceRejected("maintenance requires CLOSED with worker disabled") from None
    if not session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WORKER_EXECUTION_LOCK}):
        raise MaintenanceRejected("an execution lease is still active")
    yield


def cleanup(session, actor_user_id: UUID, *, batch_size: int = 100):
    """Bounded explicit cleanup; caller commits audit and changes atomically."""
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("invalid maintenance batch")
    _require_admin(session, actor_user_id)
    with closed_quiescent(session):
        now = session.scalar(select(func.clock_timestamp()))
        counts = {"expired_unclaimed": 0, "expired_uploads": 0, "terminal_content": 0, "request_audits": 0, "unreferenced_contexts": 0}
        # An unclaimed job is safe to expire; ever-claimed work is excluded.
        reservations = session.scalars(select(AiRequestReservation).where(
            AiRequestReservation.state == "reserved", AiRequestReservation.created_at < now - timedelta(hours=24))
            .order_by(AiRequestReservation.created_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for reservation in reservations:
            attachment = session.get(AiJobUpload, reservation.id)
            if attachment is not None:
                session.delete(attachment)
                counts["expired_uploads"] += 1
            job = session.get(AiJob, reservation.id)
            if job is not None:
                job.payload, job.result = {}, None
            reservation.state, reservation.finished_at = "failed", now
            reservation.slot_released_at = now
            counts["expired_unclaimed"] += 1

        rows = session.execute(select(AiJob, AiRequestReservation).join(AiRequestReservation).where(
            AiRequestReservation.state.in_(("succeeded", "failed")),
            AiRequestReservation.finished_at < now - timedelta(days=7),
            ~exists().where(JobRecoveryAudit.job_id == AiJob.id),
            ~exists().where(JobContentTombstone.job_id == AiJob.id),
        ).order_by(AiRequestReservation.finished_at).with_for_update(of=AiRequestReservation, skip_locked=True)
            .limit(batch_size)).all()
        for job, reservation in rows:
            job.payload, job.result = {}, None
            attachment = session.get(AiJobUpload, job.id)
            if attachment is not None:
                session.delete(attachment)
            session.add(JobContentTombstone(job_id=job.id, cleared_at=now))
            counts["terminal_content"] += 1

        # Expiring display drafts is distinct from deleting uncertain execution evidence.
        old_events = session.scalars(select(AiJobEvent).join(AiJob).join(AiRequestReservation).where(
            AiJobEvent.created_at < now - timedelta(days=7),
            AiRequestReservation.state.in_(("succeeded", "failed")),
            ~exists().where(JobRecoveryAudit.job_id == AiJobEvent.job_id),
        ).order_by(AiJobEvent.created_at).with_for_update(of=AiJobEvent, skip_locked=True)
            .limit(batch_size)).all()
        for event in old_events:
            session.delete(event)
        counts["event_content"] = len(old_events)

        old_audits = session.scalars(select(AiRequestAttempt).where(
            AiRequestAttempt.created_at < now - timedelta(days=7),
            # Keep attempt evidence for uncertain or manually resolved tasks.
            ~exists().where(AiRequestReservation.id == AiRequestAttempt.reservation_id,
                AiRequestReservation.state.in_(("running", "unknown"))),
            ~exists().where(JobRecoveryAudit.job_id == AiRequestAttempt.reservation_id),
        ).order_by(AiRequestAttempt.created_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for row in old_audits:
            session.delete(row)
            counts["request_audits"] += 1

        contexts = session.scalars(select(AgentContext).where(
            AgentContext.owner_user_id.is_not(None),
            AgentContext.expires_at < now - timedelta(days=7),
            ~exists().where(PendingAction.context_id == AgentContext.id),
            ~exists().where(AiJob.context_id == AgentContext.id),
        ).order_by(AgentContext.expires_at).with_for_update(skip_locked=True).limit(batch_size)).all()
        for context in contexts:
            session.delete(context)
            counts["unreferenced_contexts"] += 1
        session.add(MaintenanceAudit(actor_user_id=actor_user_id, counts=counts))
        session.flush()
        return counts


def inspect_job(session, actor_user_id: UUID, job_id: UUID) -> dict:
    """Read-only, sanitized investigation. No prompts, cookies or raw tool data."""
    actor = session.scalar(select(UserAccount.id).where(UserAccount.id == actor_user_id,
        UserAccount.role == "admin", UserAccount.active.is_(True)))
    if actor is None:
        raise MaintenanceRejected("active administrator required")
    row = session.execute(select(AiJob, AiRequestReservation).join(AiRequestReservation)
        .where(AiJob.id == job_id)).one_or_none()
    if row is None:
        raise MaintenanceRejected("job does not exist")
    job, reservation = row
    actions = session.scalars(select(PendingAction).where(PendingAction.context_id == job.context_id)
        .order_by(PendingAction.id)).all() if job.context_id is not None else []
    action_details = []
    for action in actions:
        ticket = session.get(Ticket, action.ticket_id) if action.ticket_id is not None else None
        action_details.append({"action_id": str(action.id), "status": action.status,
            "ticket_id": str(action.ticket_id) if action.ticket_id is not None else None,
            "binding_valid": bool(ticket and ticket.creation_key == action.id and ticket.owner_user_id == action.owner_user_id)
                if action.ticket_id is not None else True})
    result = {"job_id": str(job.id), "kind": reservation.kind, "state": reservation.state,
        "owner_user_id": str(reservation.user_id), "context_id": str(job.context_id) if job.context_id else None,
        "provider_attempts": reservation.provider_attempts, "usage": reservation.usage,
        "cost_status": reservation.cost_status, "created_at": reservation.created_at.isoformat(),
        "personal_demo_grant_id": str(reservation.personal_demo_grant_id) if reservation.personal_demo_grant_id else None,
        "utc_day": reservation.utc_day.isoformat(),
        "idempotency_sha256": sha256(reservation.idempotency_key.encode()).hexdigest(),
        "payload_fingerprint": reservation.payload_fingerprint, "result_present": job.result is not None,
        "finished_at": reservation.finished_at.isoformat() if reservation.finished_at else None,
        "request_attempt_outcomes": [[outcome, count] for outcome, count in session.execute(
            select(AiRequestAttempt.outcome, func.count()).where(AiRequestAttempt.reservation_id == job.id)
            .group_by(AiRequestAttempt.outcome).order_by(AiRequestAttempt.outcome)).all()],
        "actions": action_details, "slot_held": reservation.slot_released_at is None,
        "slot_released_at": reservation.slot_released_at.isoformat() if reservation.slot_released_at else None,
        "manually_resolved": session.scalar(select(JobRecoveryAudit.id).where(JobRecoveryAudit.job_id == job.id)) is not None}
    from project_copilot.db.executions import AiJobExecution
    execution = session.get(AiJobExecution, job_id)
    result["execution"] = None if execution is None else {
        "token": str(execution.token), "provider_open": execution.provider_open,
        "provider_uncertain": execution.provider_uncertain,
        "local_done_at": execution.local_done_at.isoformat() if execution.local_done_at else None,
        "fenced_at": execution.fenced_at.isoformat() if execution.fenced_at else None,
        "release_reason": execution.release_reason,
    }
    result["inspection_sha256"] = sha256(_canonical(result).encode()).hexdigest()
    return result


def resolve_no_retry(session, actor_user_id: UUID, job_id: UUID, evidence: dict, *, worker_stop_proof=None, settings=None) -> dict:
    """Retire a verified stopped execution, retaining all original evidence.

    This is an administrator decision after checking process/container inventory,
    business rows and the provider. It does not claim the upstream call failed or
    reproduce its result; unknown stays unknown and only its concurrency slot ends.
    """
    required = {"version", "job_id", "inspection_sha256", "original_execution_terminated",
        "business_verified", "provider_outcome", "evidence_reference_sha256", "decision"}
    if (not isinstance(evidence, dict) or set(evidence) != required or evidence["version"] != 1 or
        evidence["job_id"] != str(job_id) or evidence["original_execution_terminated"] is not True or
        evidence["business_verified"] is not True or evidence["decision"] != "terminal_no_retry" or
        evidence["provider_outcome"] not in {"not_called", "completed_and_reconciled"} or
        any(not isinstance(evidence[key], str) or not re.fullmatch("[0-9a-f]{64}", evidence[key])
            for key in ("inspection_sha256", "evidence_reference_sha256"))):
        raise MaintenanceRejected("complete verified recovery evidence is required")
    _require_admin(session, actor_user_id)
    with closed_quiescent(session):
        reservation = session.scalar(select(AiRequestReservation).where(AiRequestReservation.id == job_id)
            .with_for_update())
        from project_copilot.db.executions import AiJobExecution
        execution = session.get(AiJobExecution, job_id, with_for_update=True)
        eligible = reservation is not None and (reservation.state in ("running", "unknown") or
            (reservation.state in ("succeeded", "failed") and execution is not None))
        if (not eligible
                or reservation.slot_released_at is not None):
            raise MaintenanceRejected("only held executions requiring reconciliation can be retired")
        from project_copilot.services.worker_stop import verify_inventory
        try:
            verify_inventory(worker_stop_proof, secret=settings.public_proxy_secret.get_secret_value(),
                job_id=job_id, job_created_at=reservation.created_at, resource_id=settings.public_resource_id,
                now=session.scalar(select(func.clock_timestamp())), review_sha256=evidence["evidence_reference_sha256"])
        except (ValueError, TypeError, AttributeError, KeyError):
            raise MaintenanceRejected("trusted fresh Docker worker-stop proof required") from None
        if reservation.provider_attempts and evidence["provider_outcome"] == "not_called":
            raise MaintenanceRejected("provider initiation must be reconciled")
        inspection = inspect_job(session, actor_user_id, job_id)
        if inspection["inspection_sha256"] != evidence["inspection_sha256"]:
            raise MaintenanceRejected("inspection changed; inspect again")
        if not all(item["binding_valid"] for item in inspection["actions"]):
            raise MaintenanceRejected("business binding is inconsistent")
        session.add(JobRecoveryAudit(job_id=job_id, actor_user_id=actor_user_id,
            previous_state=reservation.state, resolution="terminal_no_retry",
            provider_attempts=reservation.provider_attempts, prior_usage=reservation.usage,
            evidence_sha256=sha256(_canonical({"review": evidence, "worker_stop": worker_stop_proof}).encode()).hexdigest(),
            inspection_sha256=inspection["inspection_sha256"]))
        now = session.scalar(select(func.clock_timestamp()))
        if reservation.state == "running":
            reservation.state, reservation.finished_at = "unknown", None
        reservation.slot_released_at = now
        if execution is not None:
            execution.fenced_at, execution.release_reason = now, "manual"
        session.flush()
        return {"job_id": str(job_id), "state": reservation.state, "resolution": "terminal_no_retry",
                "slot_released": True, "replay_permitted": False, "provider_result": "not_inferred"}


# One historical exception, not a second generic recovery policy. These facts
# come from the preserved read-only diagnosis, not a newly invented lease.
LEGACY_JOB_ID = UUID("3f72446c-6aed-470f-85da-774004b97555")
LEGACY_CREATED_AT = datetime.fromisoformat("2026-11-05T09:38:17.911814+00:00")
LEGACY_USAGE = {"prompt_tokens": 1389, "completion_tokens": 102, "total_tokens": 1491,
                "prompt_cache_hit_tokens": 896, "prompt_cache_miss_tokens": 493}
LEGACY_RISK_PHRASE = "ACCEPT UNVERIFIED PROVIDER RISK " + str(LEGACY_JOB_ID)
_LEGACY_RISK_SHA256 = sha256(LEGACY_RISK_PHRASE.encode()).hexdigest()
_LEGACY_OPERATION = "legacy_unknown_slot_retirement_v1"


def _digest(value):
    return sha256(_canonical(value).encode()).hexdigest()


def _legacy_quota_counts(session, reservation):
    count = lambda *where: session.scalar(select(func.count()).select_from(AiRequestReservation).where(*where))
    return {"user_total": count(AiRequestReservation.user_id == reservation.user_id),
        "user_original_utc_day": count(AiRequestReservation.user_id == reservation.user_id,
                                      AiRequestReservation.utc_day == reservation.utc_day),
        "global_original_utc_day": count(AiRequestReservation.utc_day == reservation.utc_day),
        "personal_grant": count(AiRequestReservation.personal_demo_grant_id == reservation.personal_demo_grant_id)}


def retire_legacy_unknown(session, actor_user_id: UUID, job_id: UUID, evidence: dict, *,
                          risk_acceptance, worker_stop_proof=None, settings=None) -> dict:
    """Release ONLY the pinned historical slot after explicit residual-risk review.

    No provider completion is inferred. The trusted host CLI collects the typed
    acknowledgement and fresh signed stop inventory; no HTTP/tool exposes this.
    All original job fields remain untouched. Both immutable audit rows commit
    with the single slot update in the caller's transaction.
    """
    required = {"version", "job_id", "expected_owner_user_id", "expected_context_id", "created_at",
        "provider_attempts", "inspection_sha256", "original_owner_verified", "original_execution_terminated",
        "business_verified", "provider_outcome", "evidence_reference_sha256", "decision"}
    if (job_id != LEGACY_JOB_ID or risk_acceptance != LEGACY_RISK_PHRASE or not isinstance(evidence, dict)
            or set(evidence) != required or type(evidence["version"]) is not int or evidence["version"] != 1
            or evidence["job_id"] != str(LEGACY_JOB_ID) or evidence["provider_outcome"] != "unverified"
            or evidence["decision"] != "retire_legacy_slot_no_retry_v1"
            or any(evidence[key] is not True for key in
                   ("original_owner_verified", "original_execution_terminated", "business_verified"))
            or type(evidence["provider_attempts"]) is not int or evidence["provider_attempts"] != 3
            or any(not isinstance(evidence[key], str) or not re.fullmatch("[0-9a-f]{64}", evidence[key])
                   for key in ("inspection_sha256", "evidence_reference_sha256"))):
        raise MaintenanceRejected("exact historical identity and explicit unverified-provider risk acceptance required")
    try:
        owner_id, context_id = UUID(evidence["expected_owner_user_id"]), UUID(evidence["expected_context_id"])
        created_at = datetime.fromisoformat(evidence["created_at"].replace("Z", "+00:00"))
        if created_at != LEGACY_CREATED_AT:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise MaintenanceRejected("historical identity receipt differs") from None
    _require_admin(session, actor_user_id)
    with closed_quiescent(session):
        reservation = session.scalar(select(AiRequestReservation).where(AiRequestReservation.id == job_id)
            .with_for_update().execution_options(populate_existing=True))
        job = session.get(AiJob, job_id, with_for_update=True)
        from project_copilot.db.executions import AiJobExecution
        execution = session.get(AiJobExecution, job_id, with_for_update=True)
        if (reservation is None or job is None or execution is not None or reservation.kind != "agent"
                or reservation.state != "unknown" or reservation.user_id != owner_id
                or reservation.created_at != LEGACY_CREATED_AT or reservation.provider_attempts != 3
                or reservation.usage != LEGACY_USAGE or reservation.finished_at is not None
                or reservation.cost_status != "unknown" or reservation.personal_demo_grant_id is None
                or job.result is not None or job.context_id != context_id):
            raise MaintenanceRejected("historical job no longer matches the pinned baseline")
        context = session.get(AgentContext, context_id, with_for_update=True)
        login = session.get(LoginSession, job.login_session_id, with_for_update=True)
        if (context is None or login is None or context.owner_user_id != owner_id or login.user_id != owner_id
                or session.scalar(select(exists().where(PendingAction.context_id == context_id)))
                or session.scalar(select(exists().where(Ticket.owner_user_id == owner_id,
                    Ticket.creation_key.is_not(None), ~exists().where(PendingAction.id == Ticket.creation_key))))):
            raise MaintenanceRejected("local business effects or ownership cannot be fully reconciled")
        from project_copilot.services.worker_stop import verify_inventory
        try:
            stop_digest = verify_inventory(worker_stop_proof, secret=settings.public_proxy_secret.get_secret_value(),
                job_id=job_id, job_created_at=reservation.created_at, resource_id=settings.public_resource_id,
                now=session.scalar(select(func.clock_timestamp())), review_sha256=evidence["evidence_reference_sha256"])
        except (ValueError, TypeError, AttributeError, KeyError):
            raise MaintenanceRejected("trusted fresh Docker worker-stop proof required") from None
        inspection = inspect_job(session, actor_user_id, job_id)
        original = dict(inspection)
        original.pop("inspection_sha256")
        # Only our own release effects may differ on an idempotent replay. No
        # usage, owner, attempts, actions or other investigation change is hidden.
        original.update(slot_held=True, slot_released_at=None, manually_resolved=False)
        if _digest(original) != evidence["inspection_sha256"]:
            raise MaintenanceRejected("inspection changed; review the latest historical evidence")
        audit = session.scalar(select(JobRecoveryAudit).where(JobRecoveryAudit.job_id == job_id))
        metadata_rows = session.scalars(select(MaintenanceAudit).where(
            MaintenanceAudit.counts["operation"].astext == _LEGACY_OPERATION,
            MaintenanceAudit.counts["job_id"].astext == str(job_id))).all()
        request_digest = _digest(evidence)
        quota_before = _legacy_quota_counts(session, reservation)
        already_retired = reservation.slot_released_at is not None
        if audit is not None or already_retired or metadata_rows:
            if (audit is None or not already_retired or len(metadata_rows) != 1
                    or audit.previous_state != "unknown" or audit.provider_attempts != 3 or audit.prior_usage != LEGACY_USAGE
                    or audit.inspection_sha256 != evidence["inspection_sha256"]
                    or metadata_rows[0].counts.get("recovery_audit_id") != str(audit.id)
                    or metadata_rows[0].counts.get("receipt_sha256") != request_digest
                    or metadata_rows[0].counts.get("provider_outcome") != "unverified"
                    or metadata_rows[0].actor_user_id != audit.actor_user_id
                    or metadata_rows[0].counts.get("risk_acceptance", {}).get("actor_user_id") != str(audit.actor_user_id)
                    or metadata_rows[0].counts.get("risk_acceptance", {}).get("version") != 1
                    or metadata_rows[0].counts.get("risk_acceptance", {}).get("phrase_sha256") != _LEGACY_RISK_SHA256
                    or _digest(metadata_rows[0].counts) != audit.evidence_sha256):
                raise MaintenanceRejected("existing retirement evidence is incomplete or differs")
        else:
            now = session.scalar(select(func.clock_timestamp()))
            details = {"operation": _LEGACY_OPERATION, "job_id": str(job_id), "recovery_audit_id": str(uuid4()),
                "provider_outcome": "unverified", "receipt_sha256": request_digest,
                "inspection_sha256": evidence["inspection_sha256"], "review_sha256": evidence["evidence_reference_sha256"],
                "stop_proof_sha256": stop_digest,
                "risk_acceptance": {"version": 1, "phrase_sha256": _LEGACY_RISK_SHA256,
                    "actor_user_id": str(actor_user_id), "accepted_at": now.isoformat()},
                "preserved": {"owner_user_id": str(owner_id), "context_id": str(context_id),
                    "created_at": reservation.created_at.isoformat(), "state": reservation.state,
                    "finished_at": None, "provider_attempts": reservation.provider_attempts,
                    "usage": dict(reservation.usage), "cost_status": reservation.cost_status,
                    "idempotency_sha256": sha256(reservation.idempotency_key.encode()).hexdigest(),
                    "payload_fingerprint": reservation.payload_fingerprint, "utc_day": reservation.utc_day.isoformat(),
                    "personal_demo_grant_id": str(reservation.personal_demo_grant_id), "quota_counts": quota_before}}
            audit = JobRecoveryAudit(id=UUID(details["recovery_audit_id"]), job_id=job_id,
                actor_user_id=actor_user_id, previous_state="unknown", resolution="terminal_no_retry",
                provider_attempts=3, prior_usage=dict(reservation.usage), evidence_sha256=_digest(details),
                inspection_sha256=evidence["inspection_sha256"])
            session.add(audit)
            session.add(MaintenanceAudit(actor_user_id=actor_user_id, counts=details))
            reservation.slot_released_at = now
            session.flush()
        return {"job_id": str(job_id), "state": reservation.state, "slot_released": True,
            "slot_released_at": reservation.slot_released_at.isoformat(), "already_retired": already_retired,
            "audit_id": str(audit.id), "provider_outcome": "unverified", "replay_permitted": False,
            "quota_before": quota_before, "quota_after": _legacy_quota_counts(session, reservation)}
