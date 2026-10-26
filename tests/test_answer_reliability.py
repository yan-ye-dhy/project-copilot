"""D19可靠性边界：受控响应验证后端，不将假模型行为当作Prompt效果。"""

import asyncio
from contextlib import contextmanager
import json

import httpx
import pytest

from evals.prompt_reliability import FEW_SHOT, build_plan, case_chunks
from project_copilot.api import rag
from project_copilot.config import Settings
from project_copilot.infrastructure.models import ChatGeneration, ChatRequestError
from project_copilot.main import create_app
from project_copilot.rag.answering import (
    AnswerResponseError, RagAnswerer, build_messages, prompt_info, validate_model_answer,
)


class Retriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, _query):
        return self.chunks


class Chat:
    model = "controlled-test"
    temperature = 0.0
    max_tokens = 1024

    def __init__(self, content):
        self.content = content
        self.calls = []

    def generate_json(self, messages):
        self.calls.append(messages)
        if isinstance(self.content, Exception):
            raise self.content
        return ChatGeneration(content=self.content, model="controlled-test-response", usage=None)


@pytest.fixture
def cases():
    return build_plan()["dataset"]["cases"]


def test_default_prompt_schema_and_context_contract_remain_exactly_frozen():
    plan = build_plan()
    assert plan["prompt_variants"]["baseline"]["info"] == prompt_info().model_dump(mode="json")
    assert prompt_info().fingerprint == "83971da601780ea5a5f5e6e4b45f11d952cb261e50d7275a1efb38494a46a9ea"
    assert prompt_info().answer_schema_hash == "ded2c45bf4b0dcd4888884ad1ff1bcbc729a1846ee0977ec90e7dfdd903c5941"
    assert prompt_info(template=FEW_SHOT).fingerprint != prompt_info().fingerprint
    assert prompt_info(template=FEW_SHOT).answer_schema_hash == prompt_info().answer_schema_hash


def test_fewshot_only_changes_system_template_and_keeps_injection_as_data(cases):
    case = cases[3]
    evidence = case_chunks(case)
    baseline = build_messages(case["question"], evidence)
    candidate = build_messages(case["question"], evidence, template=FEW_SHOT)
    assert baseline[1] == candidate[1]
    assert baseline[0]["content"] != candidate[0]["content"]
    assert [m["role"] for m in candidate] == ["system", "user"]
    assert case["text"] not in candidate[0]["content"]
    assert json.loads(candidate[1]["content"].split("\n", 1)[1])["evidence"][0]["text"] == case["text"]


@pytest.mark.parametrize("template", [None, FEW_SHOT], ids=["baseline", "fewshot"])
def test_empty_evidence_is_deterministic_without_model_calls(cases, template):
    chat = Chat(AssertionError("不得调用模型"))
    result = RagAnswerer(Retriever([]), chat, prompt_template=template).answer(cases[-1]["question"])
    assert result.status == "insufficient_evidence" and result.generation is None
    assert result.citations == () and result.evidence_count == 0 and chat.calls == []


@pytest.mark.parametrize("content,reason", [
    ('{broken private-output', "invalid_json"),
    ('{"status":"answered","answer":"private-output","citation_numbers":[Infinity]}', "invalid_json"),
    ('{"status":"answered","status":"answered","answer":"private-output","citation_numbers":[1]}', "invalid_json"),
    ('[]', "invalid_schema"),
    ('{"status":"answered","answer":"private-output","citation_numbers":[true]}', "invalid_schema"),
    ('{"status":"answered","answer":"private-output","citation_numbers":[1.0]}', "invalid_schema"),
    ('{"status":"answered","answer":"private-output","citation_numbers":[]}', "invalid_schema"),
    ('{"status":"insufficient_evidence","answer":"private-output","citation_numbers":[1]}', "invalid_schema"),
    ('{"status":"answered","answer":"private-output","citation_numbers":[2]}', "invalid_citation"),
])
def test_json_schema_and_out_of_context_errors_are_separate_sanitized_and_never_retried(cases, content, reason):
    chat = Chat(content)
    with pytest.raises(AnswerResponseError) as exc:
        RagAnswerer(Retriever(case_chunks(cases[0])), chat).answer(cases[0]["question"])
    assert exc.value.reason == reason
    assert "private-output" not in str(exc.value)
    assert len(chat.calls) == 1


@pytest.mark.parametrize("template", [None, FEW_SHOT])
def test_weak_evidence_refusal_preserves_real_generation_information(cases, template):
    chat = Chat('{"status":"insufficient_evidence","answer":"资料没有恢复时限。","citation_numbers":[]}')
    result = RagAnswerer(Retriever(case_chunks(cases[1])), chat, prompt_template=template).answer(cases[1]["question"])
    assert result.status == "insufficient_evidence" and result.evidence_count == 1
    assert result.citations == () and result.generation.actual_model == "controlled-test-response"
    assert result.generation.usage is None and len(chat.calls) == 1


def test_valid_number_is_not_a_factual_grounding_judge():
    # 明确保留此局限：单凭Pydantic/编号无法阻止语义幻觉或判断注入是否成功。
    parsed = validate_model_answer(
        '{"status":"answered","answer":"无证据的24小时恢复承诺。","citation_numbers":[1]}',
        evidence_count=1,
    )
    assert parsed.status == "answered"


@pytest.mark.parametrize("name,content,status", [
    ("valid_chinese_null_page", '{"status":"answered","answer":"支持TXT、Markdown和PDF，超限返回413。","citation_numbers":[1]}', 200),
    ("bad_json", "private-output-not-json", 502),
    ("bad_schema", '{"status":"answered","answer":"private-output","citation_numbers":[]}', 502),
    ("forged_citation", '{"status":"answered","answer":"private-output","citation_numbers":[999]}', 502),
    ("timeout", ChatRequestError("private-output-upstream-timeout"), 502),
])
def test_real_answerer_asgi_error_mapping_closure_and_correlation(cases, monkeypatch, caplog, name, content, status):
    chat = Chat(content)
    events = []

    @contextmanager
    def factory(_settings):
        events.append("opened")
        try:
            yield RagAnswerer(Retriever(case_chunks(cases[0])), chat)
        finally:
            events.append("closed")

    monkeypatch.setattr(rag, "create_answer_service", factory)

    async def run():
        app = create_app(Settings(_env_file=None, environment="test"))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.post("/rag/ask", json={"question": cases[0]["question"]})

    response = asyncio.run(run())
    assert response.status_code == status
    assert events == ["opened", "closed"] and len(chat.calls) == 1
    assert response.headers["x-run-id"]
    assert "private-output" not in response.text + caplog.text
    if status == 200:
        citation = response.json()["citations"][0]
        assert citation["text"] == cases[0]["text"] and citation["metadata"]["page"] is None
        assert response.json()["prompt"] == prompt_info().model_dump(mode="json")
