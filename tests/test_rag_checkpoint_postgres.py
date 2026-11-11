"""Actual RAG/journal recovery with owned PG, real leases and counted substitutes."""
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation
from project_copilot.db.messaging import AiJobOutbox
from project_copilot.db.recovery import AiJobRecoveryState, AiJobStep
from project_copilot.infrastructure.call_accounting import account_job
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, provider_request, track_execution
from project_copilot.infrastructure.job_progress import observe
from project_copilot.infrastructure.models import ChatGeneration
from project_copilot.infrastructure.recovery_runtime import recovery_scope
from project_copilot.rag import answering, final_retrieval
from project_copilot.rag.answering import AnswerResponseError, RagAnswerer
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.documents import ChunkMetadata, DocumentChunk
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, RetrievedChunk
from project_copilot.services.job_recovery import RecoveryBudgetExceeded, recover_job
from project_copilot.services.job_steps import StepRejected, fingerprint
from project_copilot.services.messaging import JobMessage
from project_copilot.services.personal_profile import personal_runtime_settings
from test_job_recovery_postgres import recovery_setup

QUESTION = "controlled recovery"
USAGE = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
ANSWER = {"status": "answered", "answer": "保存的两个事实。", "citation_numbers": [2, 1]}


class CrashAfterReceipt(BaseException):
    """Controlled interruption after PG commit, before validation/result commit."""


class CrashJournal:
    def __init__(self, actual, key):
        self.actual, self.key = actual, key
    def __getattr__(self, name):
        return getattr(self.actual, name)
    def complete(self, key, result):
        saved = self.actual.complete(key, result)
        if key == self.key:
            raise CrashAfterReceipt()
        return saved


class CountedRetriever:
    def __init__(self, evidence):
        self.evidence, self.calls, self.fail = evidence, 0, False
        self.auth_token = "private-retriever-canary"
    def search(self, question):
        assert not self.fail, "a completed retrieval was executed again"
        assert question == QUESTION
        self.calls += 1
        with provider_request() as attempt:
            # Substitute for one embedding request; no HTTP endpoint is used.
            attempt.completed()
        return self.evidence


class CountedChat:
    model, temperature, max_tokens = "synthetic-model", 0.0, 128
    def __init__(self):
        self.calls, self.stream_calls, self.fail = 0, 0, False
        self.api_key = "private-chat-key-canary"
        self.content = json.dumps(ANSWER, ensure_ascii=False)
    def _generate(self, messages, on_delta=None):
        assert not self.fail, "a saved model response was generated again"
        assert len(messages) == 2 and messages[0]["role"] == "system"
        self.calls += 1
        with provider_request() as attempt:
            if on_delta is not None:
                midpoint = len(self.content) // 2
                on_delta(self.content[:midpoint])
                on_delta(self.content[midpoint:])
            attempt.completed()
        return ChatGeneration(self.content, self.model, USAGE)
    def generate_json(self, messages):
        return self._generate(messages)
    def generate_json_stream(self, messages, on_delta):
        self.stream_calls += 1
        return self._generate(messages, on_delta)


@pytest.fixture
def rag_setup(recovery_setup, monkeypatch):
    setup = recovery_setup
    identity = setup.reserve()
    first = setup.claim(identity)
    spec = DenseIndexSpec(collection_name=setup.settings.qdrant_collection_name,
        embedding_model=setup.settings.embedding_model, dimensions=setup.settings.embedding_expected_dimensions,
        splitter_version=SplitConfig().index_version)
    manifest = ManifestStore(setup.settings.manifest_dir, spec)
    document_id, identities = uuid4(), (uuid4(), uuid4())
    chunks = [RetrievedChunk(text=f"受控证据{index + 1}，保留原始内容。", score=0.9 - index / 10,
        metadata=ChunkMetadata(document_id=document_id, content_hash="a" * 64,
            source_name="synthetic-checkpoint.txt", page=None, chunk_id=identity,
            chunk_index=index, index_version=spec.splitter_version)) for index, identity in enumerate(identities)]
    manifest.record(manifest.load(), CompletedDocument(document_id=document_id, content_hash="a" * 64,
        source_name="synthetic-checkpoint.txt", chunk_ids=identities))
    retriever, chat = CountedRetriever(chunks), CountedChat()
    value = SimpleNamespace(setup=setup, database=setup.database, settings=setup.settings,
        identity=identity, job=first, first=first, manifest=manifest, chunks=chunks,
        retriever=retriever, chat=chat, view_calls=0, view_mode="valid", run_events=[])

    def checkpoint_view():
        value.view_calls += 1
        if value.view_mode == "unavailable":
            raise final_retrieval.FinalRetrievalError("synthetic unavailable reranker/Qdrant")
        generation = fingerprint(manifest.load().model_dump(mode="json"))
        if value.view_mode == "generation_mismatch":
            generation = "f" * 64 if generation != "f" * 64 else "e" * 64
        actual = {item.metadata.chunk_id: DocumentChunk(text=item.text, metadata=item.metadata) for item in chunks}
        if value.view_mode == "payload_mismatch":
            actual[chunks[0].metadata.chunk_id] = DocumentChunk(text="其他存储内容", metadata=chunks[0].metadata)
        return generation, actual

    monkeypatch.setattr(final_retrieval, "get_final_retriever", lambda _settings: SimpleNamespace(checkpoint_view=checkpoint_view))

    @contextmanager
    def scope():
        tracker = ExecutionTracker(setup.database, identity, value.job.execution_lease.token)
        try:
            with track_execution(tracker), account_job(setup.database, setup.user_id, identity,
                    max_attempts=setup.settings.personal_max_provider_attempts_per_job, settings=setup.settings), \
                    recovery_scope(setup.database, setup.settings, personal_runtime_settings(setup.settings), value.job) as runtime:
                yield runtime
        finally:
            tracker.drain()

    def run(*, crash_after=None):
        events = []
        value.run_events.append(events)
        with scope() as runtime, observe(lambda kind, data: events.append((kind, data))):
            if crash_after:
                runtime.journal = CrashJournal(runtime.journal, crash_after)
            return RagAnswerer(retriever, chat, index_version=spec.index_version).answer(QUESTION)

    def rotate():
        token = value.job.execution_lease.token
        value.job.execution_lease.close()
        decision = recover_job(setup.database, setup.settings, identity)
        assert decision.decision == "requeued" and decision.reason == "checkpoint"
        value.job = setup.claim(identity)
        assert value.job.execution_lease.token != token and value.job.attempt_no == 2
        return decision

    value.scope, value.run, value.rotate = scope, run, rotate
    return value


def test_rag_receipts_survive_model_to_validation_gap_and_restore_exact_evidence(rag_setup):
    value = rag_setup
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after="rag:model:1")
    with value.database.transaction() as session:
        rows = session.scalars(select(AiJobStep).where(AiJobStep.job_id == value.identity)).all()
        assert len(rows) == 2 and all(row.status == "completed" for row in rows)
        assert session.get(AiJob, value.identity).result is None
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 2
        proof = session.get(AiJobExecution, value.identity)
        assert proof.provider_open == 0 and not proof.provider_uncertain
        serialized = json.dumps([row.result for row in rows], ensure_ascii=False)
        for secret in (value.setup.token, value.retriever.auth_token, value.chat.api_key, "你是Project Copilot"):
            assert secret not in serialized
    assert (value.retriever.calls, value.chat.calls, value.chat.stream_calls) == (1, 1, 1)
    assert "".join(data["text"] for kind, data in value.run_events[0] if kind == "delta") == ANSWER["answer"]
    value.rotate()
    value.retriever.fail = value.chat.fail = True
    result = value.run()
    assert result.status == "answered" and result.answer == ANSWER["answer"]
    assert [citation.number for citation in result.citations] == [2, 1]
    assert [citation.text for citation in result.citations] == [value.chunks[1].text, value.chunks[0].text]
    assert result.generation.usage == USAGE
    assert (value.retriever.calls, value.chat.calls, value.chat.stream_calls) == (1, 1, 1)
    assert value.view_calls == 1 and not any(kind == "delta" for kind, _ in value.run_events[1])
    with value.database.transaction() as session:
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 2
        assert session.get(AiJobRecoveryState, value.identity).model_calls == 1
        assert session.get(AiJobRecoveryState, value.identity).tool_calls == 1


@pytest.mark.parametrize("content,reason", [
    ('{"status":"answered","answer":"bad citation","citation_numbers":[3]}', "invalid_citation"),
    ('{"status":"answered","answer":"missing citation list"}', "invalid_schema"),
    ('not-json-private-model-canary', "invalid_json"),
])
def test_recovered_rag_response_reenters_original_schema_and_citation_validation(rag_setup, content, reason):
    value = rag_setup
    value.chat.content = content
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after="rag:model:1")
    value.rotate()
    value.retriever.fail = value.chat.fail = True
    with pytest.raises(AnswerResponseError) as error:
        value.run()
    assert error.value.reason == reason
    assert "private-model-canary" not in str(error.value)
    assert value.chat.calls == value.retriever.calls == 1


@pytest.mark.parametrize("mode", ["unavailable", "generation_mismatch", "payload_mismatch"])
def test_current_resource_validation_refuses_replay_before_model_or_retrieval(rag_setup, mode):
    value = rag_setup
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after="rag:model:1")
    value.rotate()
    value.view_mode = mode
    value.retriever.fail = value.chat.fail = True
    with pytest.raises((StepRejected, final_retrieval.FinalRetrievalError)):
        value.run()
    assert value.chat.calls == value.retriever.calls == 1
    with value.database.transaction() as session:
        assert session.get(AiRequestReservation, value.identity).provider_attempts == 2


def test_changed_manifest_blocks_automatic_rag_resume_without_repeating_provider(rag_setup):
    value = rag_setup
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after="rag:model:1")
    value.manifest.record(value.manifest.load(), CompletedDocument(document_id=uuid4(), content_hash="b" * 64,
        source_name="new-index-generation.txt", chunk_ids=(uuid4(),)))
    value.job.execution_lease.close()
    decision = recover_job(value.database, value.settings, value.identity)
    assert decision.decision == "blocked" and decision.reason == "model_repeat_required"
    assert value.chat.calls == value.retriever.calls == 1


@pytest.mark.parametrize("saved_stage", ["rag:model:1", "rag:retrieval"])
def test_expired_deadline_allows_cached_finalization_and_denies_missing_generation(rag_setup, saved_stage):
    value = rag_setup
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after=saved_stage)
    with value.database.transaction() as session:
        state = session.get(AiJobRecoveryState, value.identity)
        state.budget_started_at -= timedelta(hours=1)
        state.deadline_at -= timedelta(hours=1)
        deadline = state.deadline_at
    value.rotate()
    value.retriever.fail = value.chat.fail = True
    if saved_stage == "rag:model:1":
        assert value.run().status == "answered"
        assert value.chat.calls == 1
    else:
        with pytest.raises(RecoveryBudgetExceeded) as error:
            value.run()
        assert error.value.reason == "deadline" and value.chat.calls == 0
    with value.database.transaction() as session:
        assert session.get(AiJobRecoveryState, value.identity).deadline_at == deadline
        expected = 2 if saved_stage == "rag:model:1" else 1
        assert session.get(AiRequestReservation, value.identity).provider_attempts == expected


def test_mq_worker_commits_restored_rag_result_with_no_second_provider_request(rag_setup, monkeypatch):
    from project_copilot.services import ai_worker
    from project_copilot.services.mq_worker import process_message
    value = rag_setup
    with pytest.raises(CrashAfterReceipt):
        value.run(crash_after="rag:model:1")
    original_token = value.job.execution_lease.token
    value.job.execution_lease.close()
    assert recover_job(value.database, value.settings, value.identity).reason == "checkpoint"
    with value.database.transaction() as session:
        event = session.scalar(select(AiJobOutbox).where(AiJobOutbox.job_id == value.identity,
                                                       AiJobOutbox.dispatch_generation == 2))
        message = JobMessage(event.event_id, value.identity, 2)
    value.retriever.fail = value.chat.fail = True

    @contextmanager
    def answer_service(_settings):
        yield RagAnswerer(value.retriever, value.chat, index_version=value.manifest.spec.index_version)

    monkeypatch.setattr(ai_worker, "create_answer_service", answer_service)
    assert process_message(value.database, value.settings, message) == "completed"
    assert process_message(value.database, value.settings, message) == "completed"
    assert value.retriever.calls == value.chat.calls == 1
    with value.database.transaction() as session:
        reservation = session.get(AiRequestReservation, value.identity)
        proof = session.get(AiJobExecution, value.identity)
        result = session.get(AiJob, value.identity).result
        assert reservation.state == "succeeded" and reservation.provider_attempts == 2
        assert reservation.slot_released_at is not None and proof.token != original_token
        assert proof.provider_open == 0 and not proof.provider_uncertain
        assert result["answer"] == ANSWER["answer"]
        assert [item["number"] for item in result["citations"]] == [2, 1]
