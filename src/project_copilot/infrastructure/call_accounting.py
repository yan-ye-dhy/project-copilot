"""Count provider call initiations for the current durable job, without logging input."""
from contextlib import contextmanager
from contextvars import ContextVar

from project_copilot.services.quotas import QuotaService

_current = ContextVar("project_copilot_provider_accounting", default=None)
_settings = ContextVar("project_copilot_provider_release_settings", default=None)


def current_account_id():
    """Trusted execution scope for read-only caches; never a browser parameter."""
    current = _current.get()
    return current[1] if current is not None else None


@contextmanager
def account_job(database, user_id, reservation_id, *, max_attempts=16, prelocked_admin=False, settings=None):
    token = _current.set((database, user_id, reservation_id, max_attempts, prelocked_admin))
    setting_token = _settings.set(settings if settings is not None else _settings.get())
    try:
        yield
    finally:
        _current.reset(token)
        _settings.reset(setting_token)


def record_provider_attempt():
    current = _current.get()
    if current is None:
        return False
    database, user_id, reservation_id, max_attempts, prelocked_admin = current
    # Commit before invoking a provider. This is an initiation count; a crash
    # immediately afterward can overcount requests that reached the provider.
    with database.transaction() as session:
        from project_copilot.services.release_control import dependencies_ready, require_open
        from project_copilot.db.models import AiRequestReservation
        reservation = session.get(AiRequestReservation, reservation_id)
        # Public mode is checked again at each provider boundary; a live worker
        # cannot keep initiating requests after an operator closes admission.
        settings = _settings.get()
        if settings is not None:
            if reservation is None:
                from project_copilot.services.auth import AuthenticationRejected
                raise AuthenticationRejected()
            require_open(session, settings, user_id=user_id, worker=True, reservation=reservation)
            dependencies_ready(settings)
        from project_copilot.db.jobs import AiJob
        from project_copilot.services.auth import AuthService, AuthenticationRejected
        job = session.get(AiJob, reservation_id)
        if job is None:
            raise AuthenticationRejected()
        principal, _ = AuthService(session).authenticate_session_id(job.login_session_id, for_update=not prelocked_admin)
        if principal.id != user_id:
            raise AuthenticationRejected()
        if prelocked_admin:
            if principal.role != "admin" or reservation is None or reservation.kind != "upload":
                raise AuthenticationRejected()
        from project_copilot.infrastructure.execution_tracking import current_execution
        tracker = current_execution()
        if tracker is not None:
            from project_copilot.services.execution_slots import ExecutionFenced, guard_execution, provider_started
            if tracker.database is not database or tracker.job_id != reservation_id:
                raise ExecutionFenced("execution accounting binding mismatch")
            guard_execution(session, reservation_id, tracker.token)
        if settings is not None:
            from project_copilot.services.redis_admission import admit
            # Refusal occurs before either the paid attempt or provider-open
            # record is incremented. No provider request has been issued yet.
            admit(settings, user_id, scope="provider")
        QuotaService(session).mark_provider_attempt(user_id, reservation_id, max_attempts=max_attempts)
        if tracker is not None:
            provider_started(session, reservation_id, tracker.token)
    return True
