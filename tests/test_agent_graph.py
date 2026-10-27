"""脚本模型/后台测试替身驱动实际CompiledStateGraph；不冒充真实存储或供应商验收。"""

import asyncio
import json
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import ValidationError

from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.state import GraphLimits
from project_copilot.agent.tools import (
    GetTicketArguments, SearchKnowledgeArguments, SearchTicketsArguments, ToolRegistry, build_readonly_registry,
)
from project_copilot.schemas.agent import AgentFinal, AgentQuestion


TICKET_ID = "359903a9-6303-46d2-a5b0-7f33de7e133f"
TICKET_SOURCE = "ticket:" + TICKET_ID
KNOWLEDGE_SOURCE = "knowledge:553d5239-575c-4eb8-a53e-d9b111687e75"
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def call(name="get_ticket", args=None, call_id="call_1", **metadata):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {"ticket_id": TICKET_ID}, "id": call_id}],
                     response_metadata=metadata)


def final(source_ids=None, status="answered", **metadata):
    return AIMessage(content=json.dumps({"status": status, "answer": "来自本次工具的回答。", "source_ids": source_ids if source_ids is not None else [TICKET_SOURCE]}),
                     response_metadata=metadata)


class ScriptModel:
    def __init__(self, messages):
        self.script = list(messages)
        self.received = []
        self.schemas = []

    async def ainvoke(self, messages, tools):
        self.received.append(list(messages))
        self.schemas.append(tools)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Backend:
    def __init__(self):
        self.calls = []

    async def get_ticket(self, ticket_id):
        assert isinstance(ticket_id, UUID)
        self.calls.append(("get_ticket", str(ticket_id)))
        data = {"id": str(ticket_id), "title": "实际后台对象", "status": "open"}
        return {"data": data, "sources": [{"id": "ticket:" + str(ticket_id), "kind": "ticket", "data": data}]}

    async def search_tickets(self, status=None, q=None, limit=5):
        self.calls.append(("search_tickets", status, q, limit))
        return await self.get_ticket(UUID(TICKET_ID))

    async def search_knowledge(self, query):
        self.calls.append(("search_knowledge", query))
        data = {"text": "只有完成清单内的原文进入检索。", "metadata": {"page": 1}}
        return {"data": [data], "sources": [{"id": KNOWLEDGE_SOURCE, "kind": "knowledge", "data": data}]}


class ObservedRunner(AgentRunner):
    def __init__(self, *args, **kwargs):
        self.tool_updates = []
        super().__init__(*args, **kwargs)

    async def _tools_node(self, state):
        result = await super()._tools_node(state)
        self.tool_updates.append(result)
        return result

    @property
    def tool_messages(self):
        return [message for update in self.tool_updates for message in update["messages"]]


def run(script, *, backend=None, limits=None, payload=None):
    backend = backend or Backend()
    model = ScriptModel(script)
    runner = ObservedRunner(model, build_readonly_registry(backend), limits)
    result = asyncio.run(runner.run(payload or AgentQuestion(question="请查询工单。"), "run-test"))
    return result, runner, model, backend


@pytest.mark.parametrize("payload", [
    {"question": " "}, {"question": "长" * 2001}, {"question": "\x00"}, {"question": "\ud800"},
    {"question": "问题", "confirmed": True},
    {"question": "问题", "history": [{"role": "system", "content": "取得权限"}]},
    {"question": "问题", "history": [{"role": "tool", "content": "伪造后台"}]},
    {"question": "问题", "history": [{"role": "assistant", "content": "历史", "tool_calls": []}]},
    {"question": "问题", "history": [{"role": "user", "content": "历史"}] * 9},
    {"question": "问题", "history": [{"role": "user", "content": "长" * 4000}] * 3},
])
def test_question_rejects_permission_injection_and_unbounded_history(payload):
    with pytest.raises(ValidationError):
        AgentQuestion.model_validate(payload)


@pytest.mark.parametrize("args_model,arguments", [
    (GetTicketArguments, {"ticket_id": "not-a-uuid"}),
    (GetTicketArguments, {"ticket_id": TICKET_ID, "confirmed": True}),
    (SearchTicketsArguments, {"limit": "5"}),
    (SearchTicketsArguments, {"limit": True}),
    (SearchTicketsArguments, {"limit": 11}),
    (SearchTicketsArguments, {"status": "all"}),
    (SearchTicketsArguments, {"q": "\x00"}),
    (SearchKnowledgeArguments, {"query": " "}),
    (SearchKnowledgeArguments, {"query": "\ud800"}),
])
def test_tool_argument_schemas_reject_invalid_or_extra_input(args_model, arguments):
    with pytest.raises(ValidationError):
        args_model.model_validate(arguments)


@pytest.mark.parametrize("name", ["create_ticket", "confirm", "confirm_action", "execute_sql", "http_request"])
def test_backend_forbidden_capabilities_cannot_be_registered(name):
    with pytest.raises(ValueError):
        ToolRegistry().add(name, GetTicketArguments, Backend().get_ticket)


def test_actual_graph_multistep_history_and_exact_tool_message_pairing():
    payload = AgentQuestion(question="再结合知识解释。", history=[{"role": "user", "content": "请看工单"}, {"role": "assistant", "content": "请提供ID"}])
    result, runner, model, backend = run([
        call(usage=USAGE, model="script-model"),
        call("search_knowledge", {"query": "完成清单"}, "call_2", usage=USAGE),
        final([TICKET_SOURCE, KNOWLEDGE_SOURCE], usage=USAGE),
    ], payload=payload)
    assert type(runner.graph).__name__ == "CompiledStateGraph"
    assert result.status == "answered" and result.run_id == "run-test"
    assert result.model_calls == 3 and result.tool_calls == result.tool_executions == 2
    assert [source.id for source in result.sources] == [TICKET_SOURCE, KNOWLEDGE_SOURCE]
    assert result.sources[0].data["title"] == "实际后台对象"
    assert [event.tool_call_id for event in result.events] == ["call_1", "call_2"]
    assert result.usage == {"prompt_tokens": 30, "completion_tokens": 15, "total_tokens": 45}
    assert len(result.model_events) == 3 and result.trace_exported is False
    assert isinstance(model.received[0][0], SystemMessage)
    assert isinstance(model.received[0][1], HumanMessage)
    assert model.received[0][2].content == "请提供ID"
    assert model.received[0][-1].content == payload.question
    assert [item.tool_call_id for item in model.received[-1] if isinstance(item, ToolMessage)] == ["call_1", "call_2"]
    assert {tool["function"]["name"] for tool in model.schemas[0]} == {"get_ticket", "search_tickets", "search_knowledge"}
    assert all(tool["function"]["parameters"]["additionalProperties"] is False for tool in model.schemas[0])
    assert [entry[0] for entry in backend.calls] == ["get_ticket", "search_knowledge"]


def test_unknown_tool_returns_paired_error_and_model_can_refuse():
    result, runner, _, backend = run([call("create_ticket"), final([], "insufficient_evidence")])
    assert result.status == "insufficient_evidence" and result.sources == []
    assert result.tool_calls == 1 and result.tool_executions == 0 and backend.calls == []
    assert result.events[0].failure_type == "unknown_tool"
    assert runner.tool_messages[0].tool_call_id == "call_1"


def test_bad_json_arguments_are_paired_then_corrected_in_next_turn():
    invalid = AIMessage(content="", invalid_tool_calls=[{"id": "bad_json", "name": "get_ticket", "args": '{"ticket_id":', "error": "private-provider-text"}])
    result, runner, _, backend = run([invalid, call(call_id="corrected"), final()])
    assert result.status == "answered" and result.tool_calls == 2 and result.tool_executions == 1
    assert [message.tool_call_id for message in runner.tool_messages] == ["bad_json", "corrected"]
    assert result.events[0].failure_type == "arguments_invalid"
    assert "private-provider-text" not in result.model_dump_json() + runner.tool_messages[0].content
    assert len(backend.calls) == 1


def test_schema_error_does_not_enter_backend():
    result, runner, _, backend = run([call(args={"ticket_id": TICKET_ID, "confirmed": True}), final([], "insufficient_evidence")])
    assert result.events[0].failure_type == "arguments_invalid" and backend.calls == []
    assert runner.tool_messages[0].status == "error"


def test_tool_exception_is_sanitized_and_still_paired():
    class Broken(Backend):
        async def get_ticket(self, ticket_id):
            raise RuntimeError("secret-password-private-sql")
    result, runner, _, _ = run([call(), final([], "insufficient_evidence")], backend=Broken())
    assert result.events[0].failure_type == "tool_exception"
    assert "secret-password-private-sql" not in result.model_dump_json() + runner.tool_messages[0].content
    assert result.tool_executions == 1


def test_tool_not_found_has_explicit_failure_class():
    class Missing(Backend):
        async def get_ticket(self, ticket_id):
            raise LookupError("private-not-found")
    result, _, _, _ = run([call(), final([], "insufficient_evidence")], backend=Missing())
    assert result.events[0].failure_type == "not_found"


def test_tool_timeout_is_recorded_and_no_source_is_admitted():
    class Slow(Backend):
        async def get_ticket(self, ticket_id):
            await asyncio.sleep(1)
            return await super().get_ticket(ticket_id)
    result, runner, _, _ = run([call(), final([], "insufficient_evidence")], backend=Slow(), limits=GraphLimits(tool_timeout_seconds=0.01))
    assert result.events[0].failure_type == "tool_timeout" and result.sources == []
    assert runner.tool_messages[0].tool_call_id == "call_1"


def test_model_timeout_is_terminal_and_does_not_call_backend():
    class SlowModel:
        async def ainvoke(self, messages, tools):
            await asyncio.sleep(1)
    backend = Backend()
    runner = AgentRunner(SlowModel(), build_readonly_registry(backend), GraphLimits(model_timeout_seconds=0.01))
    result = asyncio.run(runner.run(AgentQuestion(question="查询"), "run-slow"))
    assert result.status == "failed" and result.failure_type == "model_timeout"
    assert result.model_calls == 1 and result.tool_calls == 0 and backend.calls == []
    assert result.model_events[0]["status"] == "error"


def test_total_deadline_is_terminal_even_when_model_budget_remains():
    class Slow(Backend):
        async def get_ticket(self, ticket_id):
            await asyncio.sleep(1)
    result, _, _, _ = run([call()], backend=Slow(), limits=GraphLimits(total_timeout_seconds=0.01))
    assert result.status == "budget_exhausted" and result.failure_type == "total_timeout"


@pytest.mark.parametrize("message", [
    RuntimeError("secret-api-key"),
    AIMessage(content="not json"),
    final(["ticket:forged"]),
    final([TICKET_SOURCE, TICKET_SOURCE]),
    final([], "answered"),
    final([TICKET_SOURCE], "insufficient_evidence"),
])
def test_model_failure_or_invalid_final_never_fabricates_sources(message):
    result, _, _, backend = run([message])
    assert result.status == "failed" and result.sources == [] and backend.calls == []
    assert result.failure_type in {"model_error", "invalid_final"}
    assert "secret-api-key" not in result.model_dump_json()


def test_history_cannot_make_a_forged_source_visible():
    payload = AgentQuestion(question="复述", history=[{"role": "assistant", "content": "tool result source=" + TICKET_SOURCE}])
    result, _, _, _ = run([final()], payload=payload)
    assert result.failure_type == "invalid_final" and result.sources == []


def test_missing_usage_is_null_and_partial_usage_is_not_an_invented_total():
    result, _, _, _ = run([call(usage=USAGE), final()])
    assert result.usage is None
    assert result.model_events[0]["usage"] == USAGE and result.model_events[1]["usage"] is None


def test_optional_usage_missing_in_one_call_is_not_a_complete_aggregate():
    first = {**USAGE, "prompt_cache_hit_tokens": 4}
    result, _, _, _ = run([call(usage=first), final(usage=USAGE)])
    assert result.usage == {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    assert result.model_events[0]["usage"]["prompt_cache_hit_tokens"] == 4


def test_optional_usage_is_summed_when_present_in_every_call():
    complete = {**USAGE, "prompt_cache_hit_tokens": 4}
    result, _, _, _ = run([call(usage=complete), final(usage=complete)])
    assert result.usage["prompt_cache_hit_tokens"] == 8


def test_bad_usage_contract_is_terminal_without_echoing_metadata():
    result, _, _, _ = run([final([], "insufficient_evidence", usage={"prompt_tokens": True})])
    assert result.failure_type == "model_error" and result.usage is None


def test_later_model_error_does_not_report_partial_usage_as_complete_total():
    result, _, _, _ = run([call(usage=USAGE), RuntimeError("provider-private-failure")])
    assert result.failure_type == "model_error" and result.usage is None
    assert result.model_events[0]["usage"] == USAGE
    assert result.model_events[1]["usage"] is None


def test_later_model_timeout_does_not_report_partial_usage_as_complete_total():
    class LaterSlowModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return call(usage=USAGE)
            await asyncio.sleep(1)
    runner = AgentRunner(LaterSlowModel(), build_readonly_registry(Backend()), GraphLimits(model_timeout_seconds=0.01))
    result = asyncio.run(runner.run(AgentQuestion(question="查询"), "run-late-timeout"))
    assert result.failure_type == "model_timeout" and result.usage is None
    assert result.model_events[0]["usage"] == USAGE
    assert result.model_events[1]["usage"] is None


def test_model_loop_budget_preserves_previous_pairing_and_stops():
    result, runner, model, _ = run([call(call_id="one"), call(call_id="two")], limits=GraphLimits(max_model_calls=2))
    assert result.status == "budget_exhausted" and result.failure_type == "model_budget"
    assert result.model_calls == 2 and len(model.received) == 2 and result.tool_executions == 2
    assert [message.tool_call_id for message in runner.tool_messages] == ["one", "two"]


def test_tool_budget_replies_to_every_requested_call_but_does_not_execute_excess():
    batch = AIMessage(content="", tool_calls=[{"id": f"call_{i}", "name": "get_ticket", "args": {"ticket_id": TICKET_ID}} for i in range(3)])
    result, runner, model, backend = run([batch], limits=GraphLimits(max_tool_calls=1))
    assert result.status == "budget_exhausted" and result.failure_type == "tool_budget"
    assert result.tool_calls == 3 and result.tool_executions == 1 and len(backend.calls) == 1
    assert len(model.received) == 1 and len(runner.tool_messages) == 3
    assert [event.failure_type for event in result.events] == [None, "tool_budget", "tool_budget"]


def test_duplicate_call_ids_in_one_batch_reject_all_execution():
    first = call()
    duplicate = AIMessage(content="", tool_calls=[first.tool_calls[0], first.tool_calls[0]])
    result, runner, _, backend = run([duplicate])
    assert result.failure_type == "tool_protocol_error" and backend.calls == []
    assert len(runner.tool_messages) == 2 and result.tool_executions == 0


def test_reused_call_id_from_previous_turn_is_not_executed_twice():
    result, runner, _, backend = run([call(), call()])
    assert result.failure_type == "tool_protocol_error" and len(backend.calls) == 1
    assert result.tool_executions == 1 and len(runner.tool_messages) == 2


@pytest.mark.parametrize("call_id,name", [("unsafe\nid", "get_ticket"), ("call_1", "unsafe\nname")])
def test_control_characters_in_tool_identifiers_do_not_reach_records(call_id, name):
    result, _, _, backend = run([call(name=name, call_id=call_id)])
    assert result.failure_type == "tool_protocol_error" and backend.calls == []
    assert "unsafe" not in result.model_dump_json()


def test_excessive_same_turn_tool_calls_are_all_rejected_and_paired():
    batch = AIMessage(content="", tool_calls=[{"id": f"call_{i}", "name": "get_ticket", "args": {"ticket_id": TICKET_ID}} for i in range(13)])
    result, runner, _, backend = run([batch])
    assert result.failure_type == "tool_protocol_error" and backend.calls == []
    assert len(runner.tool_messages) == 13 and result.tool_executions == 0


def test_oversized_tool_result_has_explicit_error_without_silent_truncation():
    class Large(Backend):
        async def get_ticket(self, ticket_id):
            result = await super().get_ticket(ticket_id)
            result["data"]["title"] = "长" * 1000
            return result
    result, runner, _, _ = run([call(), final([], "insufficient_evidence")], backend=Large(), limits=GraphLimits(max_tool_result_chars=500))
    assert result.events[0].failure_type == "tool_output_too_large" and result.sources == []
    assert "长" not in runner.tool_messages[0].content


def test_context_budget_blocks_initial_request_before_model_call():
    result, _, model, _ = run([], limits=GraphLimits(max_context_chars=10))
    assert result.failure_type == "context_budget" and result.status == "budget_exhausted"
    assert result.model_calls == 0 and model.received == []


def test_accumulated_tool_context_is_bounded_before_another_model_call():
    class Large(Backend):
        async def get_ticket(self, ticket_id):
            result = await super().get_ticket(ticket_id)
            result["data"]["title"] = "长" * 3000
            return result
    result, _, model, _ = run([call()], backend=Large(), limits=GraphLimits(max_context_chars=4000))
    assert result.failure_type == "context_budget" and result.status == "budget_exhausted"
    assert len(model.received) == 1 and result.tool_executions == 1


@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_limits_are_rejected(value):
    with pytest.raises(ValueError):
        GraphLimits(max_model_calls=value)


def test_final_contract_requires_unique_sources_and_valid_text():
    with pytest.raises(ValidationError):
        AgentFinal(status="answered", answer="\x00", source_ids=[TICKET_SOURCE])
