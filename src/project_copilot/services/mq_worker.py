"""Bridge a broker delivery into the existing durable execution protocol."""
from project_copilot.services.ai_worker import execute, process_one
from project_copilot.services.messaging import begin_delivery, complete_delivery, defer_delivery


def process_message(database, settings, message, *, runner=execute):
    """Return only after a committed outcome or a durable deferred dispatch.

    The RabbitMQ adapter may ACK this delivery after this function returns.
    No broker connection/visibility timeout is proof that an old execution died.
    """
    if _recover(database, settings, message.job_id):
        return "deferred"
    claim = begin_delivery(database, message)
    if claim.decision != "ready":
        return claim.decision
    try:
        worked = process_one(database, settings, runner=runner, job_id=message.job_id)
        if _recover(database, settings, message.job_id):
            # A new generation is committed in Outbox; the old delivery can
            # now be ACKed without pretending that the logical job completed.
            return "deferred"
        if complete_delivery(database, claim):
            return "completed"
        if defer_delivery(database, claim, reason="capacity" if not worked else "active"):
            return "deferred"
        raise RuntimeError("delivery ownership changed")
    except Exception:
        # Execution/commit failure can leave a live or uncertain attempt. Save
        # only a later *inspection*, never turn the task back into reserved.
        defer_delivery(database, claim, reason="active")
        raise


def _recover(database, settings, job_id):
    if not getattr(settings, "job_recovery_enabled", False):
        return False
    from project_copilot.services.job_recovery import recover_job
    decision = recover_job(database, settings, job_id,
                           allow_model_repeat=settings.job_recovery_allow_model_repeat)
    return decision.decision == "requeued"
