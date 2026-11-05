"""Count provider call initiations for the current durable job, without logging input."""
from contextlib import contextmanager
from contextvars import ContextVar

from project_copilot.services.quotas import QuotaService

_current = ContextVar("project_copilot_provider_accounting", default=None)
_settings = ContextVar("project_copilot_provider_release_settings", default=None)


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
        return
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
        QuotaService(session).mark_provider_attempt(user_id, reservation_id, max_attempts=max_attempts)
