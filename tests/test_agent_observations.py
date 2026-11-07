"""Lossless projection and actual graph contract tests, no provider requests."""
import json

import pytest

from project_copilot.agent.observations import model_observation
from project_copilot.agent.tools import ToolResult
from project_copilot.agent.state import GraphLimits
from test_agent_graph import Backend, TICKET_ID, TICKET_SOURCE, call, final, run


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def source(data, identity=TICKET_SOURCE):
    return {"id": identity, "kind": "ticket", "data": data}


def test_duplicate_full_ticket_is_sent_once_and_original_evidence_is_untouched():
    data = {"id": TICKET_ID, "description": "完整工单原文，保持全部细节。" * 500}
    result = ToolResult(data=data, sources=[source(data)])
    before = result.model_dump(mode="json")
    observation = model_observation(result)
    assert observation["data"] == data
    assert observation["sources"] == [{"id": TICKET_SOURCE, "kind": "ticket", "data_ref": "/data"}]
    assert result.model_dump(mode="json") == before
    assert size(observation) < size({"status": "ok", **before}) * .55


def test_list_references_preserve_order_distinct_ids_and_exact_text():
    first, second = {"text": "原文一"}, {"text": "原文二"}
    result = ToolResult(data=[first, second, first], sources=[source(second, "ticket:s2"), source(first, "ticket:s1"), source(first, "ticket:s3")])
    output = model_observation(result)
    assert output["data"] == [first, second, first]
    assert [s["data_ref"] for s in output["sources"]] == ["/data/1", "/data/0", "/data/0"]
    assert [s["id"] for s in output["sources"]] == ["ticket:s2", "ticket:s1", "ticket:s3"]


@pytest.mark.parametrize("data,evidence,matched", [
    ({"a": 1, "b": "字"}, {"b": "字", "a": 1}, True),
    ({"value": True}, {"value": 1}, False),
    ({"text": "旧版本"}, {"text": "新版本"}, False),
    ({"text": "全文"}, {"text": "全文", "page": 1}, False),
    ([], {"text": "独立来源"}, False),
])
def test_only_canonically_identical_data_is_referenced(data, evidence, matched):
    output = model_observation(ToolResult(data=data, sources=[source(evidence)]))
    assert ("data_ref" in output["sources"][0]) == matched
    if not matched:
        assert output["sources"][0]["data"] == evidence


def test_projection_preserves_the_existing_pydantic_json_normalization():
    parsed = ToolResult(data={"n": float("nan")}, sources=[])
    assert model_observation(parsed)["data"] == parsed.model_dump(mode="json")["data"] == {"n": None}


def test_actual_graph_keeps_full_citations_and_pairs_tool_messages_after_projection():
    result, runner, model, backend = run([call(), final()])
    assert result.status == "answered" and len(backend.calls) == 1
    observation = json.loads(runner.tool_messages[0].content)
    assert runner.tool_messages[0].tool_call_id == "call_1"
    assert observation["sources"][0]["data_ref"] == "/data"
    assert result.sources[0].data == observation["data"]
    assert "data_ref" not in result.sources[0].model_dump()
    assert len(model.received) == 2


def test_graph_checks_actual_projection_budget_without_truncating_long_evidence():
    class LongBackend(Backend):
        async def get_ticket(self, ticket_id):
            data = {"id": str(ticket_id), "description": "字" * 13000}
            return {"data": data, "sources": [source(data)]}

    result, runner, _, _ = run([call(), final()], backend=LongBackend())
    assert result.status == "answered" and len(result.sources[0].data["description"]) == 13000
    assert len(runner.tool_messages[0].content) < 24000
    rejected, _, _, _ = run([call(), final([])], backend=LongBackend(),
                            limits=GraphLimits(max_tool_result_chars=1000))
    assert rejected.events[0].failure_type == "tool_output_too_large"
    assert rejected.sources == []


def test_forged_final_source_still_fails_closed_without_erasing_observed_evidence():
    result, _, _, _ = run([call(), final(["ticket:unseen"])])
    assert result.failure_type == "invalid_final" and result.status == "failed"
    assert {item.id for item in result.sources} == {TICKET_SOURCE}
