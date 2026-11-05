"""Validated, isolated personal limits; never selects a release mode itself."""

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
