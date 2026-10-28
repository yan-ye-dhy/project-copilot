"""Request-scoped OpenTelemetry export with explicit Phoenix acknowledgement.

Only developer-selected metadata enters spans. Request/response bodies, headers,
provider diagnostics and exception text are never exported. All ended spans of one
HTTP request are exported once, after its root ends; another request cannot supply
its acknowledgement. Export failure preserves the business response.
"""

import asyncio
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import math
from threading import Lock
import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from starlette.datastructures import MutableHeaders
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from project_copilot.config import Settings

logger = logging.getLogger(__name__)
MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_BUFFERED_RESPONSE_BYTES = 16 * 1024 * 1024
_runtime: ContextVar['TraceRuntime | None'] = ContextVar('copilot_trace_runtime', default=None)
_run_id: ContextVar[str | None] = ContextVar('copilot_trace_run_id', default=None)


@dataclass(frozen=True)
class TraceExportResult:
    exported: bool
    reason: str
    spans: int


class SingleAttemptOTLPExporter:
    """Standard OTLP/protobuf payload, one HTTP attempt and a bounded timeout."""

    def __init__(self, endpoint: str, timeout: float):
        self.endpoint = endpoint
        self.client = httpx.Client(timeout=timeout, trust_env=False)

    def export(self, spans: Sequence[ReadableSpan]) -> TraceExportResult:
        try:
            payload = encode_spans(spans).SerializeToString()
            response = self.client.post(self.endpoint, content=payload,
                                        headers={'Content-Type': 'application/x-protobuf'})
            if response.status_code != 200:
                return TraceExportResult(False, 'http_' + str(response.status_code), len(spans))
            reply = ExportTraceServiceResponse()
            if response.content:
                reply.ParseFromString(response.content)
            if reply.partial_success.rejected_spans:
                return TraceExportResult(False, 'partial_rejection', len(spans))
            return TraceExportResult(True, 'accepted', len(spans))
        except Exception as exc:
            # A URL/credential or collector body may occur in exception text.
            return TraceExportResult(False, type(exc).__name__, len(spans))

    def shutdown(self) -> None:
        self.client.close()


class RequestSpanProcessor(SpanProcessor):
    """Keep only explicitly active traces, with bounded spans per request."""

    MAX_SPANS_PER_TRACE = 256

    def __init__(self):
        self._lock = Lock()
        self._spans: dict[int, list[ReadableSpan]] = {}
        self._overflow: set[int] = set()

    def begin(self, trace_id: int) -> None:
        with self._lock:
            self._spans[trace_id] = []

    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span: ReadableSpan) -> None:
        trace_id = span.context.trace_id
        with self._lock:
            active = self._spans.get(trace_id)
            # A tool thread that ends after the request budget was exhausted is
            # deliberately not attached to a later request or retained forever.
            if active is not None:
                if len(active) < self.MAX_SPANS_PER_TRACE:
                    active.append(span)
                else:
                    self._overflow.add(trace_id)

    def take(self, trace_id: int) -> tuple[list[ReadableSpan], bool]:
        with self._lock:
            spans = self._spans.pop(trace_id, [])
            overflow = trace_id in self._overflow
            self._overflow.discard(trace_id)
        return spans, overflow

    def shutdown(self) -> None:
        with self._lock:
            self._spans.clear()
            self._overflow.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        # Export is explicitly performed by the request owner, not a background
        # queue whose global flush could acknowledge the wrong request.
        return True


def _attributes(values: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {}
    for key, value in (values or {}).items():
        if not isinstance(key, str) or len(key) > 100:
            continue
        if any(private in key.casefold() for private in ('password', 'api_key', 'authorization', 'context_token', 'headers', 'prompt', 'payload', 'arguments')):
            continue
        if isinstance(value, str):
            result[key] = value[:256]
        elif isinstance(value, (bool, int)):
            result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
    if _run_id.get() is not None:
        result['copilot.run_id'] = _run_id.get()
    return result


@contextmanager
def trace_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
    """Metadata-only child span, or a harmless non-recording span when disabled."""
    runtime = _runtime.get()
    if runtime is None:
        yield trace.INVALID_SPAN
        return
    with runtime.tracer.start_as_current_span(name, attributes=_attributes(attributes),
                                              record_exception=False, set_status_on_exception=False) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_attribute('error.type', type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return f'{context.trace_id:032x}' if context.is_valid else None


@contextmanager
def detached_trace_span(name: str, attributes: Mapping[str, Any] | None = None) -> Iterator[Span]:
    """A duration span that can cross a sync dependency's enter/exit threads.

    FastAPI may resume a yield dependency in a different copied context. Keeping
    an attached context token across that yield cannot be safely detached there.
    This span retains the HTTP parent without changing the current context.
    """
    runtime = _runtime.get()
    if runtime is None:
        yield trace.INVALID_SPAN
        return
    span = runtime.tracer.start_span(name, attributes=_attributes(attributes))
    try:
        yield span
    except BaseException as exc:
        span.set_attribute('error.type', type(exc).__name__)
        span.set_status(Status(StatusCode.ERROR))
        raise
    finally:
        span.end()


def current_run_id() -> str | None:
    return _run_id.get()


class TraceRuntime:
    def __init__(self, settings: 'Settings'):
        self.processor = RequestSpanProcessor()
        self.exporter = SingleAttemptOTLPExporter(str(settings.phoenix_endpoint), settings.trace_export_timeout_seconds)
        self.provider = TracerProvider(resource=Resource.create({
            'service.name': 'project-copilot',
            'openinference.project.name': settings.phoenix_project,
        }), shutdown_on_exit=False)
        self.provider.add_span_processor(self.processor)
        self.tracer = self.provider.get_tracer('project_copilot', '0.1.0')

    @contextmanager
    def activate(self, run_id: str, name: str = 'http.request', attributes: Mapping[str, Any] | None = None):
        token_runtime = _runtime.set(self)
        token_id = _run_id.set(run_id)
        try:
            with self.tracer.start_as_current_span(name, kind=SpanKind.SERVER,
                                                   attributes=_attributes(attributes),
                                                   record_exception=False, set_status_on_exception=False) as span:
                self.processor.begin(span.get_span_context().trace_id)
                try:
                    yield span
                except BaseException as exc:
                    span.set_attribute('error.type', type(exc).__name__)
                    span.set_status(Status(StatusCode.ERROR))
                    raise
        finally:
            _run_id.reset(token_id)
            _runtime.reset(token_runtime)

    def export(self, trace_id: str) -> TraceExportResult:
        spans, overflow = self.processor.take(int(trace_id, 16))
        if not spans:
            return TraceExportResult(False, 'no_completed_spans', 0)
        result = self.exporter.export(spans)
        if overflow:
            return TraceExportResult(False, 'span_limit_exceeded', len(spans))
        return result

    def close(self) -> None:
        self.provider.shutdown()
        self.exporter.shutdown()


def create_trace_runtime(settings: 'Settings') -> TraceRuntime | None:
    return TraceRuntime(settings) if settings.tracing_enabled else None


def _body_status(messages: list[Message]) -> str | None:
    try:
        value = json.loads(b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
        return value.get('status') if isinstance(value, dict) and isinstance(value.get('status'), str) else None
    except (ValueError, UnicodeError):
        return None


def _acknowledged_messages(messages: list[Message], run_id: str, trace_id: str, exported: bool) -> list[Message]:
    start = next((m for m in messages if m['type'] == 'http.response.start'), None)
    if start is None:
        return messages
    headers = MutableHeaders(scope=start)
    headers['X-Run-ID'] = run_id
    headers['X-Trace-ID'] = trace_id
    headers['X-Trace-Exported'] = str(exported).lower()
    if headers.get('content-type', '').startswith('application/json') and not headers.get('content-encoding'):
        try:
            body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
            value = json.loads(body)
            if isinstance(value, dict) and 'trace_exported' in value:
                value['trace_exported'] = exported
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
                headers['Content-Length'] = str(len(encoded))
                messages = [m for m in messages if m['type'] != 'http.response.body']
                # Current endpoints return finite JSON; preserve start plus any
                # non-body messages without serializing sensitive data to logs.
                index = messages.index(start) + 1
                messages.insert(index, {'type': 'http.response.body', 'body': encoded, 'more_body': False})
        except (ValueError, UnicodeError):
            pass
    return messages


class RequestContextMiddleware:
    """Server run_id, body limits and request-scoped actual export receipt."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        settings = scope['app'].state.settings
        runtime = getattr(scope['app'].state, 'tracing', None)
        run_id = str(uuid4())
        state = scope.setdefault('state', {})
        state.update(run_id=run_id, trace_exported=False)
        started = time.monotonic()
        status = 500
        trace_id = None
        result = TraceExportResult(False, 'disabled', 0)
        messages: list[Message] = []
        buffered_bytes = 0
        passthrough = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, buffered_bytes, passthrough
            if message['type'] == 'http.response.start':
                status = message['status']
                MutableHeaders(scope=message)['X-Run-ID'] = run_id
            if runtime is None or passthrough:
                await send(message)
                return
            messages.append(message)
            buffered_bytes += len(message.get('body', b''))
            if buffered_bytes > MAX_BUFFERED_RESPONSE_BYTES:
                # Tracing must not exhaust memory or break a streaming response.
                # No export has been acknowledged when this response starts.
                passthrough = True
                for item in _acknowledged_messages(messages, run_id, trace_id, False):
                    await send(item)
                messages.clear()

        limited = RequestBodyLimitMiddleware(self.app, max_body_size=settings.upload_max_bytes + MULTIPART_OVERHEAD_BYTES)
        try:
            if runtime is None:
                await limited(scope, receive, send_with_id)
            else:
                try:
                    with runtime.activate(run_id, attributes={'http.request.method': scope['method'],
                                                               'openinference.span.kind': 'CHAIN'}) as root:
                        trace_id = f'{root.get_span_context().trace_id:032x}'
                        state['trace_id'] = trace_id
                        try:
                            await limited(scope, receive, send_with_id)
                        finally:
                            route = getattr(scope.get('route'), 'path', '<unmatched>')
                            root.update_name(scope['method'] + ' ' + route)
                            root.set_attribute('http.route', route)
                            root.set_attribute('http.response.status_code', status)
                            app_status = _body_status(messages) if not passthrough else None
                            if app_status:
                                root.set_attribute('app.status', app_status)
                            if status >= 400 or app_status in {'failed', 'budget_exhausted', 'error'}:
                                root.set_status(Status(StatusCode.ERROR))
                finally:
                    if trace_id:
                        result = await asyncio.to_thread(runtime.export, trace_id)
                        state['trace_exported'] = result.exported
                        state['trace_export_reason'] = result.reason
                        if not result.exported:
                            logger.warning('trace_export_failed run_id=%s trace_id=%s reason=%s spans=%d',
                                           run_id, trace_id, result.reason, result.spans)
                if not passthrough:
                    for message in _acknowledged_messages(messages, run_id, trace_id, result.exported):
                        await send(message)
        finally:
            # No query string, headers, body, exception text or collector URL.
            route = getattr(scope.get('route'), 'path', '<unmatched>')
            logger.info('run_id=%s method=%s path=%s status=%s elapsed_ms=%.2f trace_id=%s trace_exported=%s',
                        run_id, scope['method'], route, status, (time.monotonic()-started)*1000,
                        trace_id or 'none', str(result.exported).lower())
