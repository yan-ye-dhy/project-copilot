"""Physical async boundaries under cancellation; no network/database/model usage."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event, Thread, get_ident
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage
import pytest

from project_copilot.agent import service as agent_service
from project_copilot.config import Settings
from project_copilot.infrastructure import call_accounting, execution_tracking
from project_copilot.infrastructure.agent_model import DeepSeekToolModel
from project_copilot.infrastructure.execution_tracking import ExecutionTracker, async_provider_request, track_execution
from project_copilot.infrastructure.job_progress import observe, progress_async
from project_copilot.services import execution_slots
from project_copilot.services.redis_admission import RateLimited
from test_agent_model import TOOLS, payload


def tracker():
    return ExecutionTracker(object(), uuid4(), uuid4())


async def wait_event(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


async def ticks_while_blocked(task, count=4):
    for _ in range(count):
        await asyncio.sleep(0.002)
        assert not task.done()


def model_for(create):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create))))
    return DeepSeekToolModel(client, model="synthetic-model", temperature=0, max_tokens=32)


async def invoke(model):
    return await model.ainvoke([SystemMessage(content="synthetic"), HumanMessage(content="synthetic")], TOOLS)


def test_slow_accounting_and_ack_leave_loop_responsive_and_http_waits_for_commit(monkeypatch):
    execution = tracker()
    enter_started, finish_enter, ack_started, finish_ack = (Event() for _ in range(4))
    order, receipt = [], []
    main_thread = get_ident()
    def account():
        assert get_ident() != main_thread
        enter_started.set()
        assert finish_enter.wait(2)
        order.append("committed")
        return True
    def acknowledge(*_args, completed):
        assert get_ident() != main_thread
        ack_started.set()
        assert finish_ack.wait(2)
        receipt.append(completed)
    async def http(**_kwargs):
        assert order == ["committed"]
        order.append("http")
        return SimpleNamespace(http_response=SimpleNamespace(json=payload))
    monkeypatch.setattr(call_accounting, "record_provider_attempt", account)
    monkeypatch.setattr(execution_slots, "provider_finished", acknowledge)
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(invoke(model_for(http)))
            try:
                await wait_event(enter_started)
                assert execution.pending == 1 and order == []
                await ticks_while_blocked(task)
                finish_enter.set()
                await wait_event(ack_started)
                assert execution.pending == 1 and order == ["committed", "http"]
                await ticks_while_blocked(task)
                finish_ack.set()
                await task
            finally:
                finish_enter.set()
                finish_ack.set()
    asyncio.run(run())
    execution.drain()
    assert receipt == [True] and execution.pending == 0


def test_cancel_during_accounting_never_sends_http_and_retains_attempt_count(monkeypatch):
    execution = tracker()
    started, finish = Event(), Event()
    state = {"attempts": 0, "open": 0, "receipts": [], "http": 0}
    def account():
        started.set()
        assert finish.wait(2)
        state["attempts"] += 1
        state["open"] += 1
        return True
    def acknowledge(*_args, completed):
        state["open"] -= 1
        state["receipts"].append(completed)
    async def http(**_kwargs):
        state["http"] += 1
        pytest.fail("cancelled enter must never send HTTP")
    monkeypatch.setattr(call_accounting, "record_provider_attempt", account)
    monkeypatch.setattr(execution_slots, "provider_finished", acknowledge)
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(invoke(model_for(http)))
            try:
                await wait_event(started)
                task.cancel()
                await ticks_while_blocked(task)
                task.cancel()  # A second cancellation must not cancel/retry accounting.
                await ticks_while_blocked(task)
                assert execution.pending == 1 and state["receipts"] == []
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                finish.set()
    asyncio.run(run())
    execution.drain()
    assert state == {"attempts": 1, "open": 0, "receipts": [True], "http": 0}


@pytest.mark.parametrize("cancel", [False, True])
def test_admission_failure_or_cancelled_failed_enter_has_zero_http_and_no_receipt(monkeypatch, cancel):
    execution = tracker()
    started, finish = Event(), Event()
    observed = []
    def denied():
        started.set()
        assert finish.wait(2)
        raise RateLimited(2)
    async def http(**_kwargs):
        observed.append("http")
    monkeypatch.setattr(call_accounting, "record_provider_attempt", denied)
    monkeypatch.setattr(execution_slots, "provider_finished", lambda *_args, **_kwargs: observed.append("receipt"))
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(invoke(model_for(http)))
            try:
                await wait_event(started)
                if cancel:
                    task.cancel()
                    await ticks_while_blocked(task)
                finish.set()
                with pytest.raises(asyncio.CancelledError if cancel else RateLimited):
                    await task
            finally:
                finish.set()
    asyncio.run(run())
    execution.drain()
    assert observed == []


def test_cancel_mid_http_stays_uncertain_and_ack_thread_must_physically_drain(monkeypatch):
    execution = tracker()
    http_started, ack_started, finish_ack, drained = (Event() for _ in range(4))
    state = {"attempts": 0, "receipts": [], "uncertain": False}
    def account():
        state["attempts"] += 1
        return True
    def acknowledge(*_args, completed):
        ack_started.set()
        assert finish_ack.wait(2)
        state["receipts"].append(completed)
        state["uncertain"] = not completed
    async def http(**_kwargs):
        http_started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(call_accounting, "record_provider_attempt", account)
    monkeypatch.setattr(execution_slots, "provider_finished", acknowledge)
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(invoke(model_for(http)))
            await wait_event(http_started)
            task.cancel()
            await wait_event(ack_started)
            drain_thread = Thread(target=lambda: (execution.drain(), drained.set()))
            drain_thread.start()
            try:
                await ticks_while_blocked(task)
                assert execution.pending == 1 and not drained.is_set()
                task.cancel()
                await ticks_while_blocked(task)
                assert not drained.is_set() and state["receipts"] == []
                finish_ack.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                finish_ack.set()
                drain_thread.join(2)
    asyncio.run(run())
    assert drained.is_set() and state == {"attempts": 1, "receipts": [False], "uncertain": True}


def test_ack_failure_is_once_and_redacted_without_replaying_http(monkeypatch, caplog):
    execution = tracker()
    calls = []
    monkeypatch.setattr(call_accounting, "record_provider_attempt", lambda: True)
    def acknowledge(*_args, **_kwargs):
        calls.append("ack")
        raise RuntimeError("private-dsn-and-provider-content-canary")
    monkeypatch.setattr(execution_slots, "provider_finished", acknowledge)
    async def run():
        with track_execution(execution):
            async with async_provider_request() as attempt:
                calls.append("http")
                attempt.completed()
    asyncio.run(run())
    execution.drain()
    assert calls == ["http", "ack"] and "canary" not in caplog.text


def test_cancel_during_complete_response_ack_preserves_known_completion(monkeypatch):
    execution = tracker()
    ack_started, finish_ack = Event(), Event()
    calls = []
    monkeypatch.setattr(call_accounting, "record_provider_attempt", lambda: True)
    def acknowledge(*_args, completed):
        ack_started.set()
        assert finish_ack.wait(2)
        calls.append(("ack", completed))
    async def http(**_kwargs):
        calls.append("http")
        return SimpleNamespace(http_response=SimpleNamespace(json=payload))
    monkeypatch.setattr(execution_slots, "provider_finished", acknowledge)
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(invoke(model_for(http)))
            try:
                await wait_event(ack_started)
                task.cancel()
                await ticks_while_blocked(task)
                assert execution.pending == 1 and calls == ["http"]
                finish_ack.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                finish_ack.set()
    asyncio.run(run())
    execution.drain()
    assert calls == ["http", ("ack", True)]


@pytest.mark.parametrize("cancel", [False, True])
def test_journal_await_is_responsive_and_cancel_keeps_physical_work_tracked(monkeypatch, cancel):
    execution = tracker()
    started, finish = Event(), Event()
    observed = []
    main_thread = get_ident()
    def writer(kind, data):
        assert get_ident() != main_thread
        started.set()
        assert finish.wait(2)
        observed.append((kind, data["stage"]))
    async def run():
        with track_execution(execution), observe(writer):
            task = asyncio.create_task(progress_async("model"))
            try:
                await wait_event(started)
                await ticks_while_blocked(task)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert execution.pending == 1 and observed == []
                finish.set()
                if not cancel:
                    await task
            finally:
                finish.set()
    asyncio.run(run())
    execution.drain()
    assert observed == [("progress", "model")] and execution.pending == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_context_authentication_is_offloaded_before_model_construction(monkeypatch, cancel):
    execution = tracker()
    started, finish = Event(), Event()
    observed = []
    main_thread = get_ident()
    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass
        def authenticate(self):
            assert get_ident() != main_thread
            started.set()
            assert finish.wait(2)
            observed.append("authenticated")
    @asynccontextmanager
    async def model(_settings):
        assert observed == ["authenticated"]
        observed.append("model")
        yield object()
    monkeypatch.setattr(agent_service, "ProposalBackend", Backend)
    monkeypatch.setattr(agent_service, "create_agent_model", model)
    monkeypatch.setattr(agent_service, "build_proposal_registry", lambda _: object())
    monkeypatch.setattr(agent_service, "AgentRunner", lambda *_: object())
    async def service():
        async with agent_service.create_agent_service(Settings(_env_file=None), None, context=(uuid4(), "synthetic")):
            observed.append("ready")
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(service())
            try:
                await wait_event(started)
                await ticks_while_blocked(task)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert execution.pending == 1 and observed == []
                finish.set()
                if not cancel:
                    await task
            finally:
                finish.set()
    asyncio.run(run())
    execution.drain()
    assert observed == (["authenticated"] if cancel else ["authenticated", "model", "ready"])
