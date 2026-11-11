"""Only these diagnostic identifiers may cross the task API boundary."""

SAFE_FAILURE_TYPES = frozenset({
    "model_error", "model_timeout", "total_timeout", "invalid_final", "knowledge_retrieval_failed",
    "model_auth_error", "model_rate_limit", "model_provider_error", "model_request_rejected",
    "model_connection_error", "model_response_invalid", "model_output_limit", "model_input_invalid",
    "model_tool_protocol",
    "tool_timeout", "model_budget", "tool_budget", "context_budget", "tool_protocol_error",
    "execution_error", "invalid_schema", "invalid_json", "invalid_citation", "authentication_error",
    "repeated_tool_loop", "tool_no_progress", "provider_budget", "grant_ended",
    "admission_rate_limited", "admission_unavailable",
    "checkpoint_error", "step_uncertain",
})


def safe_failure(value):
    return value if isinstance(value, str) and value in SAFE_FAILURE_TYPES else "execution_error"
