"""Safe diagnostics cross detached API/journal contracts without a database or provider."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from project_copilot.api.jobs import _response
from project_copilot.db.jobs import AiJob
from project_copilot.services.job_events import append_event, latest_failure
from project_copilot.services.job_failures import safe_failure


MODEL_FAILURES = (
    "model_auth_error", "model_rate_limit", "model_provider_error", "model_request_rejected",
    "model_connection_error", "model_response_invalid", "model_output_limit", "model_input_invalid",
    "model_tool_protocol", "model_error", "model_timeout",
)
IDENTITY = UUID("11111111-1111-4111-8111-111111111111")
PRIVATE = "private-provider-body-and-credential"


class JournalSession:
    """Capture ORM operations only; never execute SQL or connect to storage."""
    def __init__(self):
        self.job = SimpleNamespace(event_sequence=0)
        self.events = []

    def scalar(self, statement):
        return self.job

    def add(self, event):
        self.events.append(event)

    def flush(self):
        pass

    def execute(self, statement):
        pass


@pytest.mark.parametrize("failure", MODEL_FAILURES)
def test_model_diagnostic_survives_api_and_journal_without_private_details(failure):
    assert safe_failure(failure) == failure
    journal = JournalSession()
    event = append_event(journal, IDENTITY, "diagnostic", {"failure_type": failure})
    assert event.data == {"failure_type": failure}
    assert latest_failure(SimpleNamespace(scalar=lambda _: event.data), IDENTITY) == failure

    # Detached ORM records exercise the response boundary without a session.
    job = AiJob(id=IDENTITY, event_sequence=1, failure_type=failure, result={"private": PRIVATE})
    for state in ("failed", "unknown"):
        reservation = SimpleNamespace(kind="agent", state=state, provider_attempts=1, usage=None)
        before = vars(reservation).copy()
        result = _response(job, reservation)
        assert result["failure_type"] == failure
        assert result["status"] == state and result["result"] is None
        assert result["job_id"] == str(IDENTITY)
        assert PRIVATE not in str(result)
        assert vars(reservation) == before


@pytest.mark.parametrize("untrusted", [None, 429, {}, [], PRIVATE, "model_rate_limit " + PRIVATE])
def test_unapproved_diagnostics_never_cross_the_failure_boundary(untrusted):
    assert safe_failure(untrusted) == "execution_error"


def test_api_does_not_reflect_an_unapproved_provider_error():
    job = AiJob(id=IDENTITY, event_sequence=0, failure_type=PRIVATE, result=None)
    result = _response(job, SimpleNamespace(kind="agent", state="failed"))
    assert result["failure_type"] == "execution_error"
    assert PRIVATE not in str(result)


@pytest.mark.parametrize("data", [
    {"failure_type": PRIVATE},
    {"failure_type": "model_rate_limit", "message": PRIVATE},
])
def test_diagnostic_journal_rejects_raw_errors_before_storage(data):
    journal = JournalSession()
    with pytest.raises(ValueError, match="invalid diagnostic"):
        append_event(journal, IDENTITY, "diagnostic", data)
    assert journal.events == [] and journal.job.event_sequence == 0
