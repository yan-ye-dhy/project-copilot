"""Operation receipts for replaying the bounded graph from its original entry."""
import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from project_copilot.infrastructure.execution_tracking import tracked_to_thread
from project_copilot.infrastructure.recovery_runtime import current_recovery
from project_copilot.infrastructure.job_progress import progress_async
from project_copilot.services.job_steps import (StepRejected, fingerprint, normalize_model_message,
                                               restore_model_message)
from project_copilot.services.job_recovery import RecoveryBudgetExceeded


class AgentCheckpointError(StepRejected):
    def __init__(self):
        super().__init__("checkpoint_unavailable")


async def boundary(function, *args, **kwargs):
    """Database/authority failures must escape the graph's generic tool handlers."""
    try:
        return await tracked_to_thread(function, *args, **kwargs)
    except RecoveryBudgetExceeded:
        raise
    except Exception:
        raise AgentCheckpointError() from None


def message_inputs(messages):
    """Stable input projection; generated graph message IDs are never identity."""
    result = []
    for message in messages:
        if isinstance(message, AIMessage):
            normalized = normalize_model_message(message)
            # Usage/model are receipts, not inputs sent to the next model.
            result.append({"role": "assistant", "content": normalized["content"],
                "tool_calls": normalized["tool_calls"], "invalid_tool_calls": normalized["invalid_tool_calls"]})
        elif isinstance(message, ToolMessage):
            result.append({"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id,
                           "name": message.name, "status": message.status})
        else:
            result.append({"role": message.type, "content": message.content})
    return result


async def prepare():
    runtime = current_recovery()
    if runtime is not None:
        await boundary(runtime.prepare)


async def model_call(model, messages, schemas, *, count, timeout):
    runtime = current_recovery()
    if runtime is None:
        return await asyncio.wait_for(model.ainvoke(messages, schemas), timeout=timeout)
    await boundary(runtime.validate_generation)
    key = f"agent:model:{count}"
    digest = fingerprint({"messages": message_inputs(messages), "schemas": schemas})
    decision = await boundary(runtime.journal.begin, key, kind="model", input_sha256=digest)
    if decision.decision == "replay":
        await progress_async("checkpoint")
        message = restore_model_message(decision.result)
        message._copilot_checkpoint_replayed = True
        return message
    await progress_async("model")
    try:
        message = await asyncio.wait_for(model.ainvoke(messages, schemas),
                                        timeout=min(timeout, runtime.remaining()))
    except Exception:
        await boundary(runtime.journal.mark_unknown, key)
        raise
    # Commit the normalized response before graph usage/schema/final validation.
    await boundary(runtime.validate_generation)
    await boundary(runtime.journal.complete, key, normalize_model_message(message))
    message._copilot_checkpoint_replayed = False
    return message


async def begin_tool(key, inputs, *, readonly=False):
    runtime = current_recovery()
    if runtime is None:
        return None, None
    await boundary(runtime.validate_generation)
    decision = await boundary(runtime.journal.begin, key, kind="tool", input_sha256=fingerprint(inputs),
                              retry_policy="readonly_tool" if readonly else None)
    return runtime, decision.result if decision.decision == "replay" else None


async def complete_tool(runtime, key, data, sources):
    if runtime is not None:
        await boundary(runtime.validate_generation)
        await boundary(runtime.journal.complete, key, {"data": data, "sources": sources})


def fresh_remaining(local):
    runtime = current_recovery()
    return local if runtime is None else min(local, runtime.remaining())
