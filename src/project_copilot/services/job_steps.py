"""Server-only logical step receipts, independent of physical worker attempts.

An intent commits before an external call. A completed normalized response can
then survive a crash before its graph node commits. Reading it still requires
current execution, account, release and resource authority; a journal is never
an authorization cache. No prompts, HTTP metadata or capabilities belong here.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from uuid import UUID

from langchain_core.messages import AIMessage
from sqlalchemy import Text, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

from project_copilot.db.recovery import AiJobStep
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.execution_slots import guard_execution

MAX_RESULT_BYTES = 262144
MAX_JOB_STEPS = 256
MAX_JOB_RESULT_BYTES = 4 * 1024 * 1024
_KINDS = {"model", "tool", "node"}
_MODEL_FIELDS = {"content", "tool_calls", "invalid_tool_calls", "usage", "model"}
_USAGE = {"prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
          "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
_FORBIDDEN = {"authorization", "headers", "http_headers", "api_key", "apikey", "access_token",
              "refresh_token", "cookie", "cookies", "set_cookie", "context_token", "system_prompt",
              "raw_response", "raw_request", "additional_kwargs", "response_metadata", "messages"}


class StepRejected(RuntimeError):
    def __init__(self, reason="invalid_step"):
        self.reason = reason
        super().__init__("job step rejected: " + reason)


class StepUncertain(StepRejected):
    """An unacknowledged call cannot become an implicit second provider request."""
    def __init__(self):
        super().__init__("uncertain")


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise StepRejected("fingerprint")
    return value


def bounded_json(value):
    """Copy a bounded JSON object and reject accidental transport capabilities."""
    try:
        if type(value) is not dict:
            raise ValueError
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                             separators=(", ", ": ")).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError
        normalized = json.loads(encoded)
        def inspect(item, depth=0):
            if depth > 32:
                raise ValueError
            if isinstance(item, dict):
                for key, nested in item.items():
                    if key.lower().replace("-", "_") in _FORBIDDEN:
                        raise ValueError
                    inspect(nested, depth + 1)
            elif isinstance(item, list):
                for nested in item:
                    inspect(nested, depth + 1)
        inspect(normalized)
        return normalized
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StepRejected("result_contract") from None


def fingerprint(value) -> str:
    """Hash canonical JSON without retaining the inputs (including prompts)."""
    try:
        return sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StepRejected("fingerprint") from None


def normalize_model_message(message: AIMessage) -> dict:
    """Keep only the content/tool protocol and allowlisted model/usage receipt."""
    if not isinstance(message, AIMessage) or not isinstance(message.content, str):
        raise StepRejected("result_contract")
    model = message.response_metadata.get("model")
    usage = message.response_metadata.get("usage")
    calls = [{"id": call.get("id"), "name": call.get("name"), "args": call.get("args")}
             for call in message.tool_calls]
    invalid = [{"id": call.get("id"), "name": call.get("name"), "args": call.get("args")}
               for call in message.invalid_tool_calls]
    return _model_result({"content": message.content, "tool_calls": calls,
        "invalid_tool_calls": invalid, "model": model, "usage": usage})


def _model_result(result):
    result = bounded_json(result)
    if set(result) != _MODEL_FIELDS or type(result["content"]) is not str:
        raise StepRejected("result_contract")
    model, usage = result["model"], result["usage"]
    if model is not None and (type(model) is not str or not 1 <= len(model) <= 200):
        raise StepRejected("result_contract")
    if usage is not None and (type(usage) is not dict or not set(usage) <= _USAGE or
            any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in usage.values())):
        raise StepRejected("result_contract")
    for name in ("tool_calls", "invalid_tool_calls"):
        if type(result[name]) is not list or len(result[name]) > 12:
            raise StepRejected("result_contract")
        for call in result[name]:
            if type(call) is not dict or set(call) != {"id", "name", "args"}:
                raise StepRejected("result_contract")
            if any(value is not None and (type(value) is not str or len(value) > 128)
                   for value in (call["id"], call["name"])):
                raise StepRejected("result_contract")
            if name == "tool_calls" and type(call["args"]) is not dict:
                raise StepRejected("result_contract")
            if name == "invalid_tool_calls" and call["args"] is not None and type(call["args"]) is not str:
                raise StepRejected("result_contract")
    return result


def restore_model_message(result: dict) -> AIMessage:
    result = _model_result(result)
    calls = [{**call, "type": "tool_call"} for call in result["tool_calls"]]
    invalid = [{**call, "type": "invalid_tool_call", "error": None}
               for call in result["invalid_tool_calls"]]
    return AIMessage(content=result["content"], tool_calls=calls, invalid_tool_calls=invalid,
                     response_metadata={"model": result["model"], "usage": result["usage"]})


def normalize_tool_result(result) -> dict:
    from project_copilot.agent.tools import ToolResult
    try:
        checked = ToolResult.model_validate(result)
        return bounded_json(checked.model_dump(mode="json"))
    except (ValueError, TypeError):
        raise StepRejected("result_contract") from None


def _result(kind, result):
    if kind == "model":
        return _model_result(result)
    if kind == "tool":
        return normalize_tool_result(result)
    return bounded_json(result)


@dataclass(frozen=True)
class StepDecision:
    decision: str
    result: dict | None = None


class StepJournal:
    """Short transactional boundaries for model, tool and normalized node state.

    ``revalidate(session, kind, result)`` must return exactly True before cached
    data leaves this boundary. Integrations use it to recheck ticket ownership,
    proposal bindings and the current knowledge index, beyond login authority.
    """
    def __init__(self, database, settings, job_id: UUID, execution_token: UUID, *,
                 config_sha256: str, index_fingerprint: str | None = None, revalidate=None):
        if type(job_id) is not UUID or type(execution_token) is not UUID:
            raise StepRejected("identity")
        self.database, self.settings = database, settings
        self.job_id, self.token = job_id, execution_token
        self.config_sha256 = _hash(config_sha256)
        self.index_fingerprint = _hash(index_fingerprint) if index_fingerprint is not None else None
        self.revalidate = revalidate
        self.max_result_bytes = min(MAX_RESULT_BYTES, getattr(settings, "job_recovery_checkpoint_max_bytes", MAX_RESULT_BYTES))

    def _guard(self, session):
        from project_copilot.services.job_recovery import revalidate_job_authority, recovery_budget
        if not (getattr(self.settings, "job_recovery_enabled", False) and self.settings.message_queue_enabled):
            raise StepRejected("disabled")
        revalidate_job_authority(session, self.settings, self.job_id)
        guard_execution(session, self.job_id, self.token)
        state = recovery_budget(session, self.job_id, self.token, allow_expired=True)
        if state is None:
            raise StepRejected("legacy")
        return state

    @staticmethod
    def _identity(step_key, kind, input_sha256):
        if type(step_key) is not str or re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", step_key) is None or kind not in _KINDS:
            raise StepRejected("identity")
        _hash(input_sha256)

    def _row(self, session, step_key):
        return session.get(AiJobStep, (self.job_id, step_key), with_for_update=True)

    @staticmethod
    def _json_bytes(session, value):
        # JSONB text measures the uncompressed server representation, including
        # its spacing/number normalization, rather than a smaller Python dump.
        return session.scalar(select(func.octet_length(cast(literal(value, type_=JSONB), Text))))

    def _capacity(self, session, *, adding_row=False, extra_bytes=0):
        # _guard holds this job's recovery-state row lock through commit. Thus
        # independent step keys cannot race either the row or aggregate cap.
        count, size = session.execute(select(func.count(), func.coalesce(func.sum(
            func.coalesce(func.octet_length(cast(AiJobStep.result, Text)), 0)
            + func.coalesce(func.octet_length(cast(AiJobStep.prior_intent, Text)), 0)), 0))
            .where(AiJobStep.job_id == self.job_id)).one()
        if adding_row and count >= MAX_JOB_STEPS:
            raise StepRejected("row_limit")
        if size + extra_bytes > MAX_JOB_RESULT_BYTES or (adding_row and size >= MAX_JOB_RESULT_BYTES):
            raise StepRejected("job_size_limit")

    def _matching(self, row, kind, input_sha256):
        if (row.kind != kind or row.input_sha256 != input_sha256 or row.config_sha256 != self.config_sha256
                or row.index_fingerprint != self.index_fingerprint):
            raise StepRejected("metadata_mismatch")

    def _replay(self, session, row):
        result = _result(row.kind, row.result)
        if row.result_sha256 != fingerprint(result):
            raise StepRejected("receipt_mismatch")
        if self.revalidate is None or self.revalidate(session, row.kind, bounded_json(result)) is not True:
            raise StepRejected("revalidation")
        return result

    def _retry(self, session, row, state, retry_policy):
        from project_copilot.services.job_recovery import authorize_step_retry
        if (row.execution_token == self.token or not authorize_step_retry(session, state, row)
                or (row.kind == "tool" and retry_policy != "readonly_tool")):
            raise StepUncertain()
        prior = {"attempt_no": row.attempt_no, "execution_token": str(row.execution_token),
                 "status": row.status, "created_at": row.created_at.isoformat()}
        self._capacity(session, extra_bytes=self._json_bytes(session, prior))
        row.prior_intent = prior
        # Full physical proof remains immutable in AiJobExecutionAttempt; the
        # prior intent receipt stays attached when this logical writer moves.
        row.attempt_no, row.execution_token, row.status = state.current_attempt_no, self.token, "started"

    def begin(self, step_key: str, *, kind: str, input_sha256: str, retry_policy=None) -> StepDecision:
        self._identity(step_key, kind, input_sha256)
        if retry_policy not in {None, "readonly_tool"}:
            raise StepRejected("retry_policy")
        with self.database.transaction() as session:
            state = self._guard(session)
            row = self._row(session, step_key)
            if row is not None:
                self._matching(row, kind, input_sha256)
                if row.status == "completed":
                    return StepDecision("replay", self._replay(session, row))
                if session.get(AiRequestReservation, self.job_id).state != "running":
                    raise StepRejected("not_running")
                self._retry(session, row, state, retry_policy)
            else:
                if session.get(AiRequestReservation, self.job_id).state != "running":
                    raise StepRejected("not_running")
                self._capacity(session, adding_row=True)
                row = AiJobStep(job_id=self.job_id, step_key=step_key, kind=kind, input_sha256=input_sha256,
                    config_sha256=self.config_sha256, index_fingerprint=self.index_fingerprint,
                    status="started", attempt_no=state.current_attempt_no, execution_token=self.token)
                session.add(row)
            if kind in {"model", "tool"}:
                from project_copilot.services.job_recovery import charge_recovery_step
                charge_recovery_step(session, self.job_id, self.token, kind=kind)
            session.flush()
            return StepDecision("execute")

    def read(self, step_key: str, *, kind: str, input_sha256: str) -> dict | None:
        self._identity(step_key, kind, input_sha256)
        with self.database.transaction() as session:
            self._guard(session)
            row = self._row(session, step_key)
            if row is None:
                return None
            self._matching(row, kind, input_sha256)
            if row.status != "completed":
                raise StepUncertain()
            return self._replay(session, row)

    def complete(self, step_key: str, result: dict) -> dict:
        self._identity(step_key, "node", "0" * 64)
        with self.database.transaction() as session:
            state = self._guard(session)
            row = self._row(session, step_key)
            if row is None or row.execution_token != self.token or row.attempt_no != state.current_attempt_no:
                raise StepRejected("writer")
            self._matching(row, row.kind, row.input_sha256)
            result = _result(row.kind, result)
            digest = fingerprint(result)
            if row.status == "completed":
                if row.result_sha256 != digest:
                    raise StepRejected("immutable_result")
                return result
            if row.status != "started":
                raise StepUncertain()
            size = self._json_bytes(session, result)
            if size > self.max_result_bytes:
                raise StepRejected("result_size_limit")
            self._capacity(session, extra_bytes=size)
            row.status, row.result, row.result_sha256 = "completed", result, digest
            row.completed_at = session.scalar(select(func.clock_timestamp()))
            session.flush()
            return result

    def mark_unknown(self, step_key: str) -> bool:
        self._identity(step_key, "node", "0" * 64)
        with self.database.transaction() as session:
            state = self._guard(session)
            row = self._row(session, step_key)
            if row is None or row.execution_token != self.token or row.attempt_no != state.current_attempt_no:
                raise StepRejected("writer")
            if row.status == "completed":
                return False
            row.status = "unknown"
            return True

    def save_node_state(self, step_key: str, state: dict, *, input_sha256: str) -> dict:
        """Persist a normalized node projection; raw message histories are rejected."""
        state = bounded_json(state)
        decision = self.begin(step_key, kind="node", input_sha256=input_sha256)
        return decision.result if decision.decision == "replay" else self.complete(step_key, state)
