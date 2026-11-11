"""Conservative logical-job usage across recovery attempts.

An invocation's usage does not prove a complete bill for a retried job. Never
sum a replayed result twice or replace missing predecessor usage with zero.
"""
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.recovery import AiJobExecutionAttempt, AiJobRecoveryState


def logical_usage(session, job_id, current_usage):
    state = session.get(AiJobRecoveryState, job_id)
    if state is None or state.current_attempt_no == 1:
        return current_usage
    previous = session.get(AiJobExecutionAttempt, (job_id, state.current_attempt_no - 1))
    reservation = session.get(AiRequestReservation, job_id)
    if previous is None or reservation is None:
        return None
    if previous.provider_attempts == previous.provider_baseline:
        return current_usage
    if previous.usage is None:
        return None
    if reservation.provider_attempts == state.attempt_provider_baseline:
        # This attempt only replayed/validated local receipts. Its returned
        # cached usage belongs to the predecessor and must not be added again.
        return dict(previous.usage)
    # The current result may contain cached AND fresh model usage. Until an
    # explicit complete logical receipt proves their scope, keep the aggregate
    # unknown; per-attempt receipts/results preserve the known measurements.
    return None
