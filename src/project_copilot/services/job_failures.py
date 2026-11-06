"""Only these diagnostic identifiers may cross the task API boundary."""

SAFE_FAILURE_TYPES = frozenset({
    "model_error", "model_timeout", "total_timeout", "invalid_final", "knowledge_retrieval_failed",
    "tool_timeout", "model_budget", "tool_budget", "context_budget", "tool_protocol_error",
    "execution_error", "invalid_schema", "invalid_json", "invalid_citation", "authentication_error",
    "repeated_tool_loop", "tool_no_progress", "provider_budget", "grant_ended",
})


def safe_failure(value):
    return value if isinstance(value, str) and value in SAFE_FAILURE_TYPES else "execution_error"
