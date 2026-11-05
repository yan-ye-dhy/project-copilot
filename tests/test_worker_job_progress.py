"""Real PG worker/journal and compiled graph with controlled release/providers.

Synthetic admission uses conftest's explicit fixture. OTel exports use a captured
SDK exporter and are not represented as Phoenix acceptance or paid model calls.
"""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import httpx
from langchain_core.messages import AIMessage
from openai import OpenAI
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.agent.graph import AgentRunner
from project_copilot.agent.tools import build_readonly_registry
from project_copilot.config import Settings
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.infrastructure import tracing
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.infrastructure.job_progress import emit, progress
from project_copilot.infrastructure.models import DeepSeekChat
from project_copilot.rag.answering import RagAnswerer
from project_copilot.schemas.agent import AgentQuestion
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.schemas.documents import ChunkMetadata
from project_copilot.schemas.retrieval import RetrievedChunk
from project_copilot.services import ai_worker
from project_copilot.services.auth import AuthService
from project_copilot.services.job_events import recent_events
from project_copilot.services.quotas import QuotaExceeded, QuotaService


@pytest.fixture
def worker_job(ticket_database):
    database = ticket_database
    settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="synthetic-worker-secret-" * 3,
                        auth_public_origin="https://demo.example", tracing_enabled=False)
    with database.transaction() as session:
        auth = AuthService(session)
        user = auth.register(RegisterInput(username="workeruser", password="worker-synthetic-password-123"),
                             open_registration=True)
        issued = auth.login(LoginInput(username="workeruser", password="worker-synthetic-password-123"))
        _, login = auth.authenticate(issued.token)
        user_id, login_id = user.id, login.id

    def create(kind="rag"):
        with database.transaction() as session:
            row, _ = QuotaService(session).reserve(user_id, "worker-progress-idempotency", "f" * 64, kind)
            session.add(AiJob(id=row.id, login_session_id=login_id, payload={"question": "受控问题"}))
            return row.id

    return SimpleNamespace(database=database, settings=settings, user_id=user_id, create=create)


def test_worker_persists_real_progress_and_atomically_commits_terminal_result(worker_job):
    setup = worker_job
    job_id = setup.create()
    calls = []
    def runner(settings, database, job):
        calls.append(job.id)
        record_provider_attempt()  # controlled initiation, no external provider
        progress("retrieval")
        with database.transaction() as session:
            assert [event.data["stage"] for event in recent_events(session, job.id)] == ["running", "retrieval"]
            assert session.get(AiRequestReservation, job.id).state == "running"
        emit("delta", {"text": "provisional"})
        progress("validate")
        return SimpleNamespace(model_dump=lambda **_: {"answer": "verified final"}, generation=None)
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert calls == [job_id]
    with setup.database.transaction() as session:
        row = session.get(AiRequestReservation, job_id)
        events = recent_events(session, job_id)
        assert row.state == "succeeded" and row.finished_at is not None and row.provider_attempts == 1
        assert session.get(AiJob, job_id).result == {"answer": "verified final"}
        assert events[-1].kind == "terminal" and events[-1].data == {"status": "succeeded"}
        assert "".join(event.data["text"] for event in events if event.kind == "delta") == "provisional"


def test_terminal_event_failure_rolls_back_status_and_result_without_replaying(worker_job, monkeypatch):
    setup = worker_job
    job_id = setup.create()
    original = ai_worker.append_event
    def failed_terminal(session, identity, kind, data):
        if kind == "terminal":
            assert session.get(AiRequestReservation, identity).state == "succeeded"
            raise SQLAlchemyError("synthetic transaction failure")
        return original(session, identity, kind, data)
    monkeypatch.setattr(ai_worker, "append_event", failed_terminal)
    calls = []
    def runner(*_):
        calls.append("executed")
        return SimpleNamespace(model_dump=lambda **_: {"answer": "not committed"}, generation=None)
    with pytest.raises(SQLAlchemyError):
        ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert calls == ["executed"]
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "running"
        assert session.get(AiJob, job_id).result is None
        assert not any(event.kind == "terminal" for event in recent_events(session, job_id))


def test_unknown_preserves_diagnostic_slot_and_attempts_without_replay_or_exception_leak(worker_job, caplog):
    setup = worker_job
    job_id = setup.create()
    calls = []
    def runner(*_):
        calls.append("once")
        record_provider_attempt()
        raise RuntimeError("private-provider-credential-and-body")
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert calls == ["once"]
    with setup.database.transaction() as session:
        row = session.get(AiRequestReservation, job_id)
        assert row.state == "unknown" and row.finished_at is None and row.provider_attempts == 1
        assert session.get(AiJob, job_id).result is None
        events = recent_events(session, job_id)
        assert [(event.kind, event.data) for event in events][-2:] == [
            ("diagnostic", {"failure_type": "execution_error"}), ("terminal", {"status": "unknown"})]
        assert "private-provider" not in str([event.data for event in events]) + caplog.text
        assert QuotaService(session).reserve(setup.user_id, "worker-progress-idempotency", "f" * 64, "rag")[0].id == job_id
    with pytest.raises(QuotaExceeded), setup.database.transaction() as session:
        QuotaService(session).reserve(setup.user_id, "new-unsafe-retry-key", "a" * 64, "rag")


@pytest.mark.parametrize("citation,expected", [(1, "succeeded"), (99, "unknown")])
def test_real_rag_stream_adapter_preserves_draft_but_final_validation_controls_success(worker_job, citation, expected):
    setup = worker_job
    job_id = setup.create()
    requests = []
    answer = "真实分段**草稿**"
    raw = json.dumps({"status": "answered", "answer": answer, "citation_numbers": [citation]}, ensure_ascii=False)
    def provider_event(content=None, finish=None):
        return "data: " + json.dumps({"model": "deepseek-flash", "choices": [{"index": 0,
            "delta": {"content": content}, "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"
    class Wire(httpx.SyncByteStream):
        def __iter__(self):
            for event in [provider_event(raw[:28]), provider_event(raw[28:]),
                          provider_event(finish="stop"), "data: [DONE]\n\n"]:
                yield event.encode()
    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Wire())
    evidence = RetrievedChunk(text="真实证据边界替身", score=1.0, metadata=ChunkMetadata(
        document_id=UUID(int=1), content_hash="a" * 64, source_name="controlled.md", page=None,
        chunk_id=UUID(int=2), chunk_index=0, index_version="split-v1-controlled"))
    class Retriever:
        def search(self, _query):
            return [evidence]
    def runner(*_):
        client = OpenAI(api_key="synthetic-only", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        with DeepSeekChat(client, model="deepseek-flash", temperature=0, max_tokens=1024) as chat:
            return RagAnswerer(Retriever(), chat).answer("受控问题")
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert len(requests) == 1 and requests[0]["stream"] is True
    with setup.database.transaction() as session:
        reservation = session.get(AiRequestReservation, job_id)
        events = recent_events(session, job_id)
        assert reservation.state == expected and reservation.provider_attempts == 1
        assert "".join(event.data["text"] for event in events if event.kind == "delta") == answer
        assert events[-1].data["status"] == expected
        result = session.get(AiJob, job_id).result
        if expected == "unknown":
            assert result is None
            assert any(event.kind == "diagnostic" and event.data["failure_type"] == "invalid_citation" for event in events)
        else:
            assert result["answer"] == answer and result["citations"][0]["text"] == evidence.text


@pytest.mark.parametrize("bad_source,expected", [(False, "succeeded"), (True, "unknown")])
def test_real_compiled_agent_progress_and_source_validation_survive_worker_journal(worker_job, bad_source, expected):
    setup = worker_job
    job_id = setup.create(kind="agent")
    executed, model_calls = [], []
    class Backend:
        async def search_tickets(self, **_kwargs):
            executed.append("search_tickets")
            return {"data": {"items": []}, "sources": []}
        async def get_ticket(self, **_kwargs):
            pytest.fail("unexpected tool")
        async def search_knowledge(self, **_kwargs):
            pytest.fail("unexpected tool")
    class Model:
        async def ainvoke(self, messages, tools):
            model_calls.append("once")
            record_provider_attempt()
            if not executed:
                return AIMessage(content="", tool_calls=[{"name": "search_tickets", "args": {}, "id": "call_test"}])
            return AIMessage(content=json.dumps({"status": "answered" if bad_source else "insufficient_evidence",
                "answer": "受控回答", "source_ids": ["knowledge:unauthorized"] if bad_source else []}))
    def runner(*_):
        return asyncio.run(AgentRunner(Model(), build_readonly_registry(Backend())).run(
            AgentQuestion(question="查询"), str(job_id)))
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert executed == ["search_tickets"] and len(model_calls) == 2
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == expected
        assert session.get(AiRequestReservation, job_id).provider_attempts == 2
        events = recent_events(session, job_id)
        stages = [event.data["stage"] for event in events if event.kind == "progress"]
        assert stages[:5] == ["running", "model", "search_tickets", "tool_done", "model"]
        assert not any(event.kind == "delta" for event in events)
        if bad_source:
            assert session.get(AiJob, job_id).result is None
            assert any(event.kind == "diagnostic" and event.data == {"failure_type": "invalid_final"} for event in events)


def test_worker_creates_independent_trace_and_exports_once_using_controlled_otlp(worker_job, monkeypatch):
    setup = worker_job
    job_id = setup.create()
    captures = []
    def export(_self, spans):
        captures.append(list(spans))
        return tracing.TraceExportResult(True, "accepted", len(spans))
    monkeypatch.setattr(tracing.SingleAttemptOTLPExporter, "export", export)
    settings = setup.settings.model_copy(update={"tracing_enabled": True})
    def runner(*_):
        with tracing.trace_span("controlled.stage"):
            return SimpleNamespace(model_dump=lambda **_: {"answer": "verified"}, generation=None)
    assert ai_worker.process_one(setup.database, settings, runner=runner)
    assert len(captures) == 1
    spans = captures[0]
    roots = [span for span in spans if span.name == "worker.job"]
    assert len(roots) == 1
    assert all(span.attributes["copilot.run_id"] == str(job_id) for span in spans)
    stage = next(span for span in spans if span.name == "controlled.stage")
    assert stage.parent.span_id == roots[0].context.span_id
    assert tracing.current_run_id() is None and tracing.current_trace_id() is None


def test_worker_journal_is_visible_to_an_independent_reader_process_without_execution(worker_job):
    setup = worker_job
    job_id = setup.create()
    def runner(*_):
        progress("retrieval")
        return SimpleNamespace(model_dump=lambda **_: {"answer": "verified"}, generation=None)
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    env = dict(os.environ)
    # Only an isolated synthetic test DSN; never a public database credential.
    env["PC_EVENT_TEST_DSN"] = setup.database.engine.url.render_as_string(hide_password=False)
    env["PC_EVENT_TEST_JOB"] = str(job_id)
    result = subprocess.run([sys.executable, "-c", """
import json, os
from uuid import UUID
from project_copilot.db.session import Database
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.job_events import recent_events
database = Database(os.environ['PC_EVENT_TEST_DSN'])
try:
    with database.transaction() as session:
        job_id = UUID(os.environ['PC_EVENT_TEST_JOB'])
        events = recent_events(session, job_id)
        row = session.get(AiRequestReservation, job_id)
        print(json.dumps({'status': row.state, 'attempts': row.provider_attempts,
                          'sequence': [event.sequence for event in events],
                          'terminal': events[-1].data}))
finally:
    database.close()
"""], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "isolated event reader failed"
    actual = json.loads(result.stdout)
    assert actual == {"status": "succeeded", "attempts": 0,
                      "sequence": [1, 2, 3], "terminal": {"status": "succeeded"}}


def test_trace_export_exception_cannot_undo_or_reexecute_committed_job(worker_job, monkeypatch, caplog):
    setup = worker_job
    job_id = setup.create()
    setup.settings.tracing_enabled = True
    def broken_export(*_):
        raise RuntimeError("private-export-endpoint-and-secret")
    monkeypatch.setattr(tracing.TraceRuntime, "export", broken_export)
    calls = []
    def runner(*_):
        calls.append("once")
        return SimpleNamespace(model_dump=lambda **_: {"answer": "committed"}, generation=None)
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert not ai_worker.process_one(setup.database, setup.settings, runner=runner)
    assert calls == ["once"]
    with setup.database.transaction() as session:
        assert session.get(AiRequestReservation, job_id).state == "succeeded"
    assert "worker_trace_export_failed" in caplog.text
    assert "private-export" not in caplog.text


def test_late_timed_out_thread_observer_cannot_append_after_terminal(worker_job):
    from project_copilot.infrastructure.job_progress import _observer
    setup = worker_job
    job_id = setup.create()
    copied = []
    def runner(*_):
        copied.append(_observer.get())  # same object a copied to_thread context retains
        return SimpleNamespace(model_dump=lambda **_: {"answer": "final"}, generation=None)
    assert ai_worker.process_one(setup.database, setup.settings, runner=runner)
    with setup.database.transaction() as session:
        before = session.get(AiJob, job_id).event_sequence
    copied[0]("progress", {"stage": "rerank", "label": "正在重排知识片段"})
    copied[0]("delta", {"text": "late provisional text"})
    with setup.database.transaction() as session:
        assert session.get(AiJob, job_id).event_sequence == before
        assert recent_events(session, job_id)[-1].kind == "terminal"
