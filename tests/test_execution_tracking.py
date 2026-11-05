"""Physical Future tracking and provider boundaries; no database/provider calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import ContextVar
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from openai import APIStatusError
import pytest

from project_copilot.infrastructure.execution_tracking import (
    ExecutionTracker, current_execution, provider_request, track_execution,
    tracked_submit, tracked_to_thread,
)


def tracker():
    return ExecutionTracker(SimpleNamespace(), UUID(int=1), UUID(int=2))


def test_cancelled_async_wait_does_not_remove_running_thread_from_drain():
    execution = tracker()
    started, finish, drained = Event(), Event(), Event()
    def work():
        started.set()
        assert finish.wait(5)
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(tracked_to_thread(work))
            while not started.is_set():
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(run())
    assert execution.pending == 1
    thread = Thread(target=lambda: (execution.drain(), drained.set()))
    thread.start()
    try:
        assert not drained.wait(.03)
        finish.set()
        thread.join(5)
        assert drained.is_set() and execution.pending == 0
        with pytest.raises(RuntimeError, match='drained'):
            execution.submit(None, lambda: None)
    finally:
        finish.set()
        thread.join(5)


def test_custom_reranker_timeout_is_not_physical_future_completion():
    execution = tracker()
    started, finish = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def score():
            started.set()
            assert finish.wait(5)
            return current_execution()
        with track_execution(execution):
            future = tracked_submit(pool, score)
        try:
            assert started.wait(2)
            with pytest.raises(FutureTimeout):
                future.result(timeout=.01)
            assert execution.pending == 1
            finish.set()
            assert future.result(timeout=2) is execution
        finally:
            finish.set()
    execution.drain()
    assert execution.pending == 0


def test_truly_cancelled_queued_future_does_not_leave_a_fake_active_thread():
    execution = tracker()
    started, finish = Event(), Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = execution.submit(pool, lambda: (started.set(), finish.wait(5)))
        assert started.wait(2)
        second = execution.submit(pool, lambda: pytest.fail('cancelled queue must not execute'))
        assert execution.pending == 2
        assert second.cancel()
        assert execution.pending == 1
        finish.set()
        first.result(timeout=2)
    execution.drain()
    assert execution.pending == 0


def test_context_propagation_and_restoration_do_not_affect_ordinary_local_threads():
    execution = tracker()
    marker = ContextVar('synthetic-marker', default=None)
    async def run():
        marker.set('copied')
        assert current_execution() is None
        assert await tracked_to_thread(lambda: current_execution()) is None
        with track_execution(execution):
            observed = await tracked_to_thread(lambda: (current_execution(), marker.get()))
        assert current_execution() is None
        return observed
    assert asyncio.run(run()) == (execution, 'copied')
    execution.drain()


def provider_mocks(execution):
    return (
        patch('project_copilot.infrastructure.call_accounting.record_provider_attempt', return_value=True),
        patch('project_copilot.services.execution_slots.provider_finished'),
    )


@pytest.mark.parametrize('complete', [False, True])
def test_provider_requires_explicit_complete_response_even_on_normal_context_exit(complete):
    execution = tracker()
    counted, finished = provider_mocks(execution)
    with counted as initiation, finished as acknowledgement, track_execution(execution):
        with provider_request() as attempt:
            if complete:
                attempt.completed()
    initiation.assert_called_once_with()
    acknowledgement.assert_called_once_with(execution.database, execution.job_id, execution.token, completed=complete)


def test_schema_failure_after_full_response_preserves_provider_completion():
    execution = tracker()
    counted, finished = provider_mocks(execution)
    with counted, finished as acknowledgement, track_execution(execution), pytest.raises(ValueError):
        with provider_request() as attempt:
            attempt.completed()
            raise ValueError('synthetic invalid JSON')
    assert acknowledgement.call_args.kwargs['completed'] is True


def test_async_model_cancellation_retains_uncertain_remote_completion():
    execution = tracker()
    entered = asyncio.Event()
    async def provider():
        with provider_request():
            entered.set()
            await asyncio.Future()
    async def run():
        with track_execution(execution):
            task = asyncio.create_task(provider())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    counted, finished = provider_mocks(execution)
    with counted, finished as acknowledgement:
        asyncio.run(run())
    assert acknowledgement.call_args.kwargs['completed'] is False


def test_complete_http_rejection_is_terminal_but_headers_only_error_is_not():
    execution = tracker()
    request = httpx.Request('POST', 'https://synthetic.invalid')
    for full in (True, False):
        response = httpx.Response(429, request=request, content=b'{}') if full else httpx.Response(
            429, request=request, stream=httpx.ByteStream(b'{}'))
        error = APIStatusError('synthetic provider rejection', response=response, body=None)
        counted, finished = provider_mocks(execution)
        with counted, finished as acknowledgement, track_execution(execution), pytest.raises(APIStatusError):
            with provider_request():
                raise error
        assert acknowledgement.call_args.kwargs['completed'] is full
        response.close()


def test_missing_accounting_cannot_start_an_unfenced_provider_request():
    from project_copilot.services.execution_slots import ExecutionFenced
    execution = tracker()
    with patch('project_copilot.infrastructure.call_accounting.record_provider_attempt', return_value=False), \
         patch('project_copilot.services.execution_slots.provider_finished') as acknowledge, track_execution(execution):
        with pytest.raises(ExecutionFenced):
            with provider_request():
                pytest.fail('provider must not execute')
    acknowledge.assert_not_called()


def test_acknowledgement_failure_never_logs_provider_payload_or_swallows_business_error(caplog):
    execution = tracker()
    with patch('project_copilot.infrastructure.call_accounting.record_provider_attempt', return_value=True), \
         patch('project_copilot.services.execution_slots.provider_finished', side_effect=RuntimeError('private-dsn')), \
         track_execution(execution), pytest.raises(ValueError, match='business error'):
        with provider_request() as attempt:
            attempt.completed()
            raise ValueError('business error')
    assert 'RuntimeError' in caplog.text and 'private-dsn' not in caplog.text


def test_drain_includes_nested_work_started_by_a_cancelled_outer_tool():
    execution = tracker()
    outer_started, spawn_nested, inner_started, finish_inner, drained = (Event() for _ in range(5))
    with ThreadPoolExecutor(max_workers=1) as reranker_pool:
        def inner():
            inner_started.set()
            assert finish_inner.wait(5)
        def outer():
            outer_started.set()
            assert spawn_nested.wait(5)
            tracked_submit(reranker_pool, inner)
        async def run():
            with track_execution(execution):
                future = asyncio.create_task(tracked_to_thread(outer))
                while not outer_started.is_set():
                    await asyncio.sleep(0)
                future.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await future
        asyncio.run(run())
        drain_thread = Thread(target=lambda: (execution.drain(), drained.set()))
        drain_thread.start()
        try:
            assert not drained.wait(.02)
            spawn_nested.set()
            assert inner_started.wait(2)
            assert not drained.wait(.02)
            finish_inner.set()
            drain_thread.join(5)
            assert drained.is_set() and execution.pending == 0
        finally:
            spawn_nested.set()
            finish_inner.set()
            drain_thread.join(5)


@pytest.mark.parametrize('done', [False, True])
def test_clipped_sdk_stream_needs_actual_done_for_remote_completion(done):
    import json
    from openai import OpenAI
    from project_copilot.infrastructure.models import ChatResponseError, DeepSeekChat
    execution = tracker()
    wire = 'data: ' + json.dumps({'model': 'deepseek-flash', 'choices': [
        {'index': 0, 'delta': {'content': 'clipped'}, 'finish_reason': 'length'}]}) + '\n\n'
    if done:
        wire += 'data: [DONE]\n\n'
    attempts = []
    def transport(request):
        attempts.append(request)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=wire)
    client = OpenAI(api_key='synthetic', max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    counted, finished = provider_mocks(execution)
    with counted, finished as acknowledgement, track_execution(execution), \
         DeepSeekChat(client, model='deepseek-flash', temperature=0, max_tokens=32) as chat:
        with pytest.raises(ChatResponseError):
            chat.generate_json_stream([{'role': 'system', 'content': 'JSON'}, {'role': 'user', 'content': 'q'}], lambda _: None)
    assert len(attempts) == 1
    assert acknowledgement.call_args.kwargs['completed'] is done


def test_clipped_stream_drain_is_bounded_and_does_not_invent_done():
    import json
    from openai import OpenAI
    from project_copilot.infrastructure.models import ChatResponseError, DeepSeekChat
    execution = tracker()
    first = 'data: ' + json.dumps({'model': 'deepseek-flash', 'choices': [
        {'index': 0, 'delta': {}, 'finish_reason': 'length'}]}) + '\n\n'
    wire = first + ':' + ('x' * (512 * 1024)) + '\n\ndata: [DONE]\n\n'
    client = OpenAI(api_key='synthetic', max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            headers={'Content-Type': 'text/event-stream'}, content=wire))))
    counted, finished = provider_mocks(execution)
    with counted, finished as acknowledgement, track_execution(execution), \
         DeepSeekChat(client, model='deepseek-flash', temperature=0, max_tokens=32) as chat:
        with pytest.raises(ChatResponseError, match='预算'):
            chat.generate_json_stream([{'role': 'system', 'content': 'JSON'}, {'role': 'user', 'content': 'q'}], lambda _: None)
    assert acknowledgement.call_args.kwargs['completed'] is False
