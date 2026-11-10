"""Validated, isolated personal limits; never selects a release mode itself."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import logging


_worker_resources = ContextVar("copilot_worker_resources", default=None)

PERSONAL_JOB_LIMIT = 500
PERSONAL_MAX_MINUTES = 24 * 60
PERSONAL_PROFILE = "personal-demo-v2"

PERSONAL_SETTINGS_FIELDS = frozenset({
    "personal_max_daily_requests_per_user", "personal_max_daily_requests_global",
    "personal_max_concurrent_requests_per_user", "personal_max_provider_attempts_per_job",
    "personal_agent_max_model_calls", "personal_agent_max_tool_calls",
    "personal_agent_tool_timeout_seconds", "personal_agent_model_timeout_seconds",
    "personal_agent_total_timeout_seconds", "personal_deepseek_max_tokens",
    "personal_context_hourly_limit", "personal_context_active_limit",
    "personal_proposal_daily_limit", "personal_proposal_pending_limit",
})


def personal_limits(settings):
    return {"job_limit": PERSONAL_JOB_LIMIT,
        "user_daily_limit": settings.personal_max_daily_requests_per_user,
        "global_daily_limit": settings.personal_max_daily_requests_global,
        "concurrent_limit": settings.personal_max_concurrent_requests_per_user,
        "provider_attempts": settings.personal_max_provider_attempts_per_job,
        "agent_model_calls": settings.personal_agent_max_model_calls,
        "agent_tool_calls": settings.personal_agent_max_tool_calls,
        "tool_timeout_seconds": settings.personal_agent_tool_timeout_seconds,
        "model_timeout_seconds": settings.personal_agent_model_timeout_seconds,
        "total_timeout_seconds": settings.personal_agent_total_timeout_seconds,
        "max_output_tokens": settings.personal_deepseek_max_tokens,
        "context_hourly": settings.personal_context_hourly_limit,
        "context_active": settings.personal_context_active_limit,
        "proposal_daily": settings.personal_proposal_daily_limit,
        "proposal_pending": settings.personal_proposal_pending_limit}


def personal_runtime_settings(settings):
    """Only a Worker-claimed personal execution may use this derived copy.

    Values come from independently validated personal fields. Keep the original
    Settings for release fingerprints/accounting; never mutate app settings or
    widen the ordinary mode's validation limits.
    """
    scope = _worker_resources.get()
    if scope is not None:
        return scope.personal(settings)
    return _derive_settings(settings)


def _derive_settings(settings):
    return settings.model_copy(update={
        "agent_max_model_calls": settings.personal_agent_max_model_calls,
        "agent_max_tool_calls": settings.personal_agent_max_tool_calls,
        "agent_tool_timeout_seconds": settings.personal_agent_tool_timeout_seconds,
        "agent_model_timeout_seconds": settings.personal_agent_model_timeout_seconds,
        "agent_total_timeout_seconds": settings.personal_agent_total_timeout_seconds,
        "max_provider_attempts_per_job": settings.personal_max_provider_attempts_per_job,
        "deepseek_max_tokens": settings.personal_deepseek_max_tokens,
        "deepseek_timeout_seconds": settings.personal_agent_model_timeout_seconds,
        "zhipu_timeout_seconds": settings.personal_agent_model_timeout_seconds})


def _configuration(settings):
    # Python values preserve real SecretStr equality. A JSON dump would mask
    # different credentials with the same stars and permit unsafe reuse.
    return deepcopy(settings.model_dump(mode="python") if hasattr(settings, "model_dump") else vars(settings))


class WorkerResources:
    """Stable settings and explicit cleanup for one serial Worker lifetime."""
    def __init__(self, settings):
        self.settings = settings
        self._snapshot = _configuration(settings)
        self._personal = None
        self._personal_snapshot = None

    def validate(self, settings=None):
        if (settings is not None and settings is not self.settings) or _configuration(self.settings) != self._snapshot:
            raise RuntimeError("worker configuration changed; restart required")
        if self._personal is not None and _configuration(self._personal) != self._personal_snapshot:
            raise RuntimeError("personal worker configuration changed; restart required")

    def personal(self, settings):
        self.validate(settings)
        if self._personal is None:
            self._personal = _derive_settings(settings)
            self._personal_snapshot = _configuration(self._personal)
        return self._personal

    def close(self):
        from project_copilot.infrastructure.redis_runtime import close_redis_runtime
        from project_copilot.rag.final_retrieval import close_final_retriever
        for settings in (self._personal, self.settings):
            if settings is None:
                continue
            for kind, close in (("retriever", close_final_retriever), ("redis", close_redis_runtime)):
                try:
                    close(settings)
                except Exception as exc:
                    logging.getLogger(__name__).warning("worker resource cleanup unavailable kind=%s error_type=%s",
                                                        kind, type(exc).__name__)


@contextmanager
def worker_resource_scope(settings):
    """No global cache of derived settings; scope exit drains all owned clients."""
    if _worker_resources.get() is not None:
        raise RuntimeError("worker resource scope is already active")
    resources = WorkerResources(settings)
    token = _worker_resources.set(resources)
    try:
        yield resources
    finally:
        try:
            resources.close()
        finally:
            _worker_resources.reset(token)
