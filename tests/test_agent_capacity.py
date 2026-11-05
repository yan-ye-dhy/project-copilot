"""High personal budgets and request-local loop control on the compiled graph.

Scripted models and backend substitutes only: no provider or database access.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage

from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.state import GraphLimits
from project_copilot.agent.tools import build_proposal_registry, build_readonly_registry
from project_copilot.schemas.agent import AgentQuestion, ToolEvent
from project_copilot.services.quotas import QuotaExceeded
from test_agent_graph import Backend, ObservedRunner, ScriptModel, TICKET_ID, TICKET_SOURCE, call, final, run


def ticket_call(index, *, prefix="call"):
    return {"id": f"{prefix}_{index}", "name": "get_ticket", "args": {"ticket_id": str(UUID(int=index + 1))}}


def test_twenty_four_model_steps_are_available_and_twenty_fifth_is_not_started():
    model = ScriptModel([AIMessage(content="", tool_calls=[ticket_call(index)]) for index in range(24)])
    runner = ObservedRunner(model, build_readonly_registry(Backend()),
        GraphLimits(max_model_calls=24, max_tool_calls=80, max_context_chars=200000, total_timeout_seconds=900))
    result = asyncio.run(runner.run(AgentQuestion(question="逐项查询合成工单"), "budget24"))
    assert result.status == "budget_exhausted" and result.failure_type == "model_budget"
    assert result.model_calls == len(model.received) == 24
    assert result.tool_calls == result.tool_executions == 24 and result.tool_failures == 0
    assert len(runner.tool_messages) == 24


def test_twenty_fourth_model_step_can_finish_with_validated_sources():
    script = [AIMessage(content="", tool_calls=[ticket_call(index)]) for index in range(23)]
    source = "ticket:" + str(UUID(int=1))
    model = ScriptModel([*script, final([source])])
    runner = AgentRunner(model, build_readonly_registry(Backend()),
        GraphLimits(max_model_calls=24, max_tool_calls=80, max_context_chars=200000, total_timeout_seconds=900))
    result = asyncio.run(runner.run(AgentQuestion(question="合成高预算"), "finish24"))
    assert result.status == "answered" and result.model_calls == 24 and result.sources[0].id == source


def test_eighty_tool_attempts_execute_but_eighty_first_is_paired_rejected():
    batches = [AIMessage(content="", tool_calls=[ticket_call(index) for index in range(start, start + 10)])
               for start in range(0, 80, 10)]
    script = [*batches, AIMessage(content="", tool_calls=[ticket_call(80)])]
    result, runner, model, backend = run(script, limits=GraphLimits(
        max_model_calls=24, max_tool_calls=80, max_context_chars=200000, total_timeout_seconds=900))
    assert result.status == "budget_exhausted" and result.failure_type == "tool_budget"
    assert result.tool_calls == 81 and result.tool_executions == len(backend.calls) == 80
    assert result.tool_failures == 1 and len(runner.tool_messages) == 81
    assert len(model.received) == 9
    assert all(event.executed for event in result.events[:80])
    assert not result.events[80].executed and result.events[80].failure_type == "tool_budget"


def test_duplicate_successful_read_is_rejected_without_cached_result_or_backend_execution():
    result, runner, _, backend = run([call(call_id="first"), call(call_id="again"), final()])
    assert result.status == "answered" and result.tool_calls == 2 and result.tool_executions == 1
    assert result.tool_failures == 1 and len(backend.calls) == 1
    duplicate = json.loads(runner.tool_messages[-1].content)
    assert duplicate["failure_type"] == "duplicate_read" and duplicate["previous_tool_call_id"] == "first"
    assert "data" not in duplicate and "sources" not in duplicate
    assert [event.executed for event in result.events] == [True, False]
    assert [message.tool_call_id for message in runner.tool_messages] == ["first", "again"]


def test_same_batch_duplicate_is_still_an_attempt_and_has_matching_tool_message():
    first = call(call_id="first").tool_calls[0]
    second = call(call_id="second").tool_calls[0]
    result, runner, _, backend = run([AIMessage(content="", tool_calls=[first, second]), final()])
    assert result.tool_calls == 2 and result.tool_executions == len(backend.calls) == 1
    assert result.tool_failures == 1 and [m.tool_call_id for m in runner.tool_messages] == ["first", "second"]


def test_validated_default_arguments_share_one_read_identity():
    result, _, _, backend = run([
        call("search_tickets", {"q": "故障"}, "first"),
        call("search_tickets", {"status": None, "q": "故障", "limit": 5}, "defaulted"),
        final(),
    ])
    assert result.status == "answered" and result.tool_executions == 1 and result.tool_failures == 1
    assert len([entry for entry in backend.calls if entry[0] == "search_tickets"]) == 1


def test_obvious_repeated_read_loop_stops_with_budget_remaining():
    result, runner, model, backend = run(
        [call(call_id=f"loop_{index}") for index in range(10)],
        limits=GraphLimits(max_model_calls=24, max_tool_calls=80, total_timeout_seconds=900))
    assert result.failure_type == "repeated_tool_loop" and result.status == "budget_exhausted"
    assert result.model_calls == len(model.received) == 4 and result.tool_calls == 4
    assert result.tool_executions == len(backend.calls) == 1 and result.tool_failures == 3
    assert len(runner.tool_messages) == 4


def test_unproductive_invalid_calls_stop_without_spending_the_whole_budget():
    result, _, model, backend = run([call("unregistered", call_id=f"bad_{index}") for index in range(10)],
        limits=GraphLimits(max_model_calls=24, max_tool_calls=80, total_timeout_seconds=900))
    assert result.failure_type == "tool_no_progress" and result.status == "budget_exhausted"
    assert result.model_calls == len(model.received) == 3
    assert result.tool_calls == result.tool_failures == 3 and result.tool_executions == 0 and backend.calls == []


def test_new_successful_observation_resets_no_progress_streak():
    script = [call(call_id="first"), call(call_id="repeat_one"),
              call(args={"ticket_id": str(UUID(int=2))}, call_id="fresh"),
              call(call_id="repeat_two"), final()]
    result, _, _, backend = run(script, limits=GraphLimits(max_model_calls=24, max_tool_calls=80))
    assert result.status == "answered" and result.tool_executions == 2 and result.tool_failures == 2
    assert len(backend.calls) == 2


def test_a_reused_runner_does_not_share_read_suppression_between_requests():
    backend = Backend()
    model = ScriptModel([call(call_id="same"), final(), call(call_id="same"), final()])
    runner = AgentRunner(model, build_readonly_registry(backend))
    first = asyncio.run(runner.run(AgentQuestion(question="第一请求"), "one"))
    second = asyncio.run(runner.run(AgentQuestion(question="第二请求"), "two"))
    assert first.status == second.status == "answered" and first.tool_failures == second.tool_failures == 0
    assert first.tool_executions == second.tool_executions == 1 and len(backend.calls) == 2


def test_write_tool_is_not_cached_or_suppressed_as_a_read():
    class WriteBackend(Backend):
        def __init__(self):
            super().__init__()
            self.proposals = 0

        async def propose_ticket(self, title, description):
            self.proposals += 1
            return {"data": {"title": title}, "sources": []}

    backend = WriteBackend()
    calls = [{"id": f"proposal_{i}", "name": "propose_ticket", "args": {"title": "演示", "description": "合成数据"}}
             for i in range(2)]
    runner = AgentRunner(ScriptModel([AIMessage(content="", tool_calls=calls)]), build_proposal_registry(backend))
    result = asyncio.run(runner.run(AgentQuestion(question="只预览"), "write"))
    assert result.status == "pending_confirmation" and result.tool_calls == result.tool_executions == 2
    assert result.tool_failures == 0 and backend.proposals == 2


def test_900_second_deadline_is_supported_without_allowing_unbounded_operation_timeout():
    configured = GraphLimits(total_timeout_seconds=900, model_timeout_seconds=60, tool_timeout_seconds=300)
    assert configured.total_timeout_seconds == 900
    for name, value in [("total_timeout_seconds", 901), ("total_timeout_seconds", float("inf")),
                        ("total_timeout_seconds", float("nan")), ("tool_timeout_seconds", 301),
                        ("model_timeout_seconds", 301), ("total_timeout_seconds", True)]:
        with pytest.raises(ValueError):
            GraphLimits(**{name: value})


def test_actual_deadline_stops_at_900_without_waiting_in_real_time(monkeypatch):
    class Clock:
        current = 0

        @classmethod
        def monotonic(cls):
            return cls.current

    class AdvancesClock:
        async def ainvoke(self, messages, tools):
            Clock.current = 901
            return call()

    monkeypatch.setattr("project_copilot.agent.graph.time", SimpleNamespace(monotonic=Clock.monotonic))
    backend = Backend()
    runner = AgentRunner(AdvancesClock(), build_readonly_registry(backend), GraphLimits(total_timeout_seconds=900))
    result = asyncio.run(runner.run(AgentQuestion(question="合成时限"), "clock"))
    assert result.status == "budget_exhausted" and result.failure_type == "total_timeout"
    assert result.model_calls == 1 and result.tool_calls == 1 and result.tool_executions == 0
    assert result.elapsed_ms == 901000 and backend.calls == []


def test_failure_count_includes_rejected_protocol_attempts_without_unsafe_identifiers():
    result, _, _, backend = run([call(call_id="unsafe\nid")])
    assert result.failure_type == "tool_protocol_error" and result.tool_calls == result.tool_failures == 1
    assert result.tool_executions == 0 and backend.calls == [] and "unsafe" not in result.model_dump_json()


def test_old_tool_event_remains_parseable_without_execution_flag():
    event = ToolEvent(tool_call_id="old", name="get_ticket", status="ok", elapsed_ms=1)
    assert event.executed is False


def test_provider_budget_at_model_boundary_is_not_hidden_as_model_error_or_retried():
    result, _, model, backend = run([QuotaExceeded("provider_attempts"), final()],
        limits=GraphLimits(max_model_calls=24, max_tool_calls=80))
    assert result.status == "budget_exhausted" and result.failure_type == "provider_budget"
    assert result.model_calls == len(model.received) == 1 and backend.calls == []
    assert result.model_events[0]["failure_type"] == "provider_budget" and result.usage is None


def test_provider_budget_after_completed_model_preserves_only_acknowledged_usage():
    result, _, model, _ = run([call(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
                                QuotaExceeded("provider_attempts")],
        limits=GraphLimits(max_model_calls=24, max_tool_calls=80))
    assert result.failure_type == "provider_budget" and len(model.received) == 2
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_provider_budget_in_tool_stops_remaining_batch_but_preserves_every_pair():
    class LimitedBackend(Backend):
        async def search_knowledge(self, query):
            self.calls.append(("search_knowledge", query))
            raise QuotaExceeded("provider_attempts")

    script = [AIMessage(content="", tool_calls=[
        {"id": "limited", "name": "search_knowledge", "args": {"query": "预算"}},
        {"id": "not_run", "name": "get_ticket", "args": {"ticket_id": TICKET_ID}},
        {"id": "not_run_two", "name": "search_tickets", "args": {"q": "预算"}},
    ]), final()]
    result, runner, model, backend = run(script, backend=LimitedBackend(),
        limits=GraphLimits(max_model_calls=24, max_tool_calls=80))
    assert result.status == "budget_exhausted" and result.failure_type == "provider_budget"
    assert len(model.received) == 1 and result.tool_calls == result.tool_failures == 3
    assert result.tool_executions == 1 and backend.calls == [("search_knowledge", "预算")]
    assert [event.executed for event in result.events] == [True, False, False]
    assert [message.tool_call_id for message in runner.tool_messages] == ["limited", "not_run", "not_run_two"]
    assert all(json.loads(message.content)["failure_type"] == "provider_budget" for message in runner.tool_messages)
