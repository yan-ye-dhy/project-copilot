"""Track physical local work and one-shot provider boundaries for a personal job.

A cancelled asyncio Future is not proof that its underlying thread ended. The
worker drains actual concurrent Futures before dropping its physical lease.
"""
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import asyncio
import functools
import logging
from threading import Condition

from openai import APIStatusError

logger = logging.getLogger(__name__)
_current: ContextVar['ExecutionTracker | None'] = ContextVar('copilot_physical_execution', default=None)


def current_execution():
    return _current.get()


@contextmanager
def track_execution(tracker):
    token = _current.set(tracker)
    try:
        yield tracker
    finally:
        _current.reset(token)


class ExecutionTracker:
    """One process-owned execution; no timeout turns unfinished work into proof."""
    def __init__(self, database, job_id, token):
        self.database, self.job_id, self.token = database, job_id, token
        self._condition = Condition()
        self._pending = 0
        self._closed = False
        self._executor = None

    @property
    def pending(self):
        with self._condition:
            return self._pending

    def submit(self, executor: Executor | None, function: Callable, *args, **kwargs) -> Future:
        context = copy_context()
        call = functools.partial(function, *args, **kwargs)
        with self._condition:
            if self._closed:
                raise RuntimeError('execution already drained')
            if executor is None:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='personal-job')
                executor = self._executor
            # Reserve before scheduling so a fast thread cannot escape drain().
            self._pending += 1
            try:
                future = executor.submit(context.run, call)
            except BaseException:
                self._pending -= 1
                self._condition.notify_all()
                raise
            future.add_done_callback(self._finished)
            return future

    def _finished(self, future):
        # This callback belongs to the real concurrent Future. Cancelling only
        # its asyncio wrapper cannot decrement the count of a running thread.
        with self._condition:
            self._pending -= 1
            self._condition.notify_all()

    def drain(self):
        with self._condition:
            self._condition.wait_for(lambda: self._pending == 0)
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)


def tracked_submit(executor, function, *args, **kwargs):
    tracker = current_execution()
    if tracker is None:
        return executor.submit(function, *args, **kwargs)
    return tracker.submit(executor, function, *args, **kwargs)


async def tracked_to_thread(function, *args, **kwargs):
    tracker = current_execution()
    if tracker is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    return await asyncio.wrap_future(tracker.submit(None, function, *args, **kwargs))


@dataclass
class ProviderAttempt:
    terminal_observed: bool = False

    def completed(self):
        """Call only after the complete synchronous response or stream DONE."""
        self.terminal_observed = True


@contextmanager
def provider_request():
    """One accounted attempt; losing a response never becomes remote completion.

    Local/thread completion and remote provider completion are different. A full
    HTTP rejection is terminal; timeout, cancellation or lost stream is not.
    """
    from project_copilot.infrastructure.call_accounting import record_provider_attempt
    from project_copilot.services.execution_slots import ExecutionFenced, provider_finished

    tracker = current_execution()
    started = record_provider_attempt()
    if tracker is not None and not started:
        raise ExecutionFenced('provider attempt has no job accounting scope')
    attempt = ProviderAttempt()
    try:
        yield attempt
    except APIStatusError as exc:
        # The SDK reads error responses before raising APIStatusError. Do not
        # infer a terminal response from a headers-only/incomplete custom error.
        if getattr(exc.response, 'is_stream_consumed', False):
            attempt.completed()
        raise
    finally:
        if tracker is not None:
            try:
                provider_finished(tracker.database, tracker.job_id, tracker.token,
                                  completed=attempt.terminal_observed)
            except Exception as exc:
                # Keep the durable open count when acknowledgement fails; no
                # retry, raw provider exception, payload or credential is logged.
                logger.warning('execution provider acknowledgement unavailable error_type=%s', type(exc).__name__)
