"""Bridge a broker delivery into the existing durable execution protocol."""
from project_copilot.services.ai_worker import execute, process_one
from project_copilot.services.messaging import begin_delivery, complete_delivery, defer_delivery


def process_message(database, settings, message, *, runner=execute):
    """Return only after a committed outcome or a durable deferred dispatch.

    The RabbitMQ adapter may ACK this delivery after this function returns.
    No broker connection/visibility timeout is proof that an old execution died.
    """
    claim = begin_delivery(database, message)
    if claim.decision != "ready":
        return claim.decision
    try:
        worked = process_one(database, settings, runner=runner, job_id=message.job_id)
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
