"""Real adapter/compiled graph with controlled provider transport; no paid calls."""
import asyncio
import json
from uuid import UUID

import httpx
from openai import OpenAI
import pytest

from project_copilot.infrastructure.models import DeepSeekChat, ChatResponseError
from project_copilot.infrastructure.job_progress import observe
from project_copilot.rag.streaming import AnswerDraft
from project_copilot.rag.answering import RagAnswerer, AnswerResponseError
from project_copilot.schemas.documents import ChunkMetadata
from project_copilot.schemas.retrieval import RetrievedChunk

MESSAGES = [{"role": "system", "content": "JSON only"}, {"role": "user", "content": "question"}]


def chunk(content=None, *, finish=None, usage=None):
    result = {"model": "deepseek-flash", "choices": [] if usage else [{"index": 0,
        "delta": {"role": "assistant", "content": content}, "finish_reason": finish}]}
    if usage:
        result["usage"] = usage
    return b"data: " + json.dumps(result, ensure_ascii=False).encode() + b"\n\n"


class Wire(httpx.SyncByteStream):
    def __init__(self, chunks, before_next=None):
        self.chunks, self.before_next = chunks, before_next
        self.closed = False

    def __iter__(self):
        for i, value in enumerate(self.chunks):
            if self.before_next:
                self.before_next(i)
            yield value

    def close(self):
        self.closed = True


def chat_for(wire, calls):
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=wire)
    client = OpenAI(api_key="synthetic-test", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)))
    return DeepSeekChat(client, model="deepseek-flash", temperature=0, max_tokens=1024)


def test_adapter_delivers_real_delta_before_provider_stream_completes_and_closes_once():
    received, requests = [], []
    content = '{"status":"answered","answer":"真实增量","citation_numbers":[1]}'
    def before(i):
        if i == 1:
            assert received == [content[:30]]
    wire = Wire([chunk(content[:30]), chunk(content[30:]), chunk(finish="stop"),
        chunk(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}), b"data: [DONE]\n\n"], before)
    with chat_for(wire, requests) as chat:
        generation = chat.generate_json_stream(MESSAGES, received.append)
    assert generation.content == content and generation.usage["total_tokens"] == 5
    assert len(requests) == 1 and requests[0]["stream"] is True
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert wire.closed


@pytest.mark.parametrize("tail", [[], [chunk(finish="length"), b"data: [DONE]\n\n"],
    [b'data: {"model":"deepseek-flash","choices":[{"index":0,"delta":{"tool_calls":[{}]}}]}\n\n'],
    [b"data: invalid\n\n"], [chunk(finish="stop")]])
def test_broken_stream_is_never_retried_or_accepted(tail):
    requests = []
    wire = Wire([chunk('partial'), *tail])
    with chat_for(wire, requests) as chat, pytest.raises(ChatResponseError):
        chat.generate_json_stream(MESSAGES, lambda _: None)
    assert len(requests) == 1 and wire.closed


@pytest.mark.parametrize("answer", ['竹青 **回答**\n下一行', '引号"与反斜杠\\', '🌿完成', '<script>alert(1)</script>'])
def test_incremental_json_answer_handles_unicode_and_partial_escapes(answer):
    parts = []
    draft = AnswerDraft(parts.append)
    text = json.dumps({"status": "answered", "answer": answer, "citation_numbers": [1]})
    for char in text:
        draft.feed(char)
    assert "".join(parts) == answer
    assert draft.raw == text


def evidence():
    return RetrievedChunk(text="受控证据", score=1.0, metadata=ChunkMetadata(document_id=UUID(int=1),
        content_hash="a" * 64, source_name="source.md", page=None, chunk_id=UUID(int=2),
        chunk_index=0, index_version="split-v1-test"))


@pytest.mark.parametrize("citations,accepted", [([1], True), ([99], False)])
def test_streamed_draft_never_bypasses_final_citation_validation(citations, accepted):
    emitted, calls = [], []
    raw = json.dumps({"status": "answered", "answer": "**受控回答**", "citation_numbers": citations}, ensure_ascii=False)
    wire = Wire([chunk(raw[:30]), chunk(raw[30:]), chunk(finish="stop"), b"data: [DONE]\n\n"])
    class Retriever:
        def search(self, query):
            return [evidence()]
    with chat_for(wire, calls) as chat, observe(lambda kind, data: emitted.append((kind, data))):
        answerer = RagAnswerer(Retriever(), chat)
        if accepted:
            result = answerer.answer("问题")
            assert result.answer == "**受控回答**" and result.citations[0].text == "受控证据"
        else:
            with pytest.raises(AnswerResponseError, match="之外"):
                answerer.answer("问题")
    assert "".join(data["text"] for kind, data in emitted if kind == "delta") == "**受控回答**"
    assert [data["stage"] for kind, data in emitted if kind == "progress"] == ["retrieval", "model", "validate"]
    assert len(calls) == 1


def test_actual_compiled_agent_progress_has_tools_and_validated_final_no_draft():
    from langchain_core.messages import AIMessage
    from project_copilot.agent.graph import AgentRunner
    from project_copilot.agent.tools import build_readonly_registry
    from project_copilot.schemas.agent import AgentQuestion
    executed, events = [], []
    class Backend:
        async def search_tickets(self, **kwargs):
            executed.append("search_tickets")
            return {"data": {"items": []}, "sources": []}
        async def get_ticket(self, **kwargs):
            raise AssertionError("not called")
        async def search_knowledge(self, **kwargs):
            raise AssertionError("not called")
    class Model:
        async def ainvoke(self, messages, tools):
            if not executed:
                return AIMessage(content="", tool_calls=[{"name": "search_tickets", "args": {}, "id": "call_1"}])
            return AIMessage(content=json.dumps({"status": "insufficient_evidence", "answer": "证据不足", "source_ids": []}))
    with observe(lambda kind, data: events.append((kind, data))):
        result = asyncio.run(AgentRunner(Model(), build_readonly_registry(Backend())).run(AgentQuestion(question="查询"), "test-run"))
    assert result.status == "insufficient_evidence"
    assert [data["stage"] for kind, data in events] == ["model", "search_tickets", "tool_done", "model", "validate"]
    assert not any(kind == "delta" for kind, _ in events)
