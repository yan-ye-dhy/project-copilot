"""Trace middleware must release SSE frames before final export acknowledgement."""
import asyncio
from types import SimpleNamespace

from project_copilot.config import Settings
from project_copilot.infrastructure import tracing


def test_tracing_does_not_buffer_first_sse_frame(monkeypatch):
    emitted, exports = [], []
    settings = Settings(_env_file=None, tracing_enabled=True)
    runtime = tracing.TraceRuntime(settings)
    monkeypatch.setattr(runtime.exporter, "export", lambda spans:
        (exports.append(spans) or tracing.TraceExportResult(True, "accepted", len(spans))))
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
            "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        assert len(emitted) == 2 and not exports
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    async def run():
        scope = {"type": "http", "method": "GET", "path": "/jobs/id/events", "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(settings=settings, tracing=runtime))}
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            emitted.append(message)
        await tracing.RequestContextMiddleware(app)(scope, receive, send)
    try:
        asyncio.run(run())
        assert dict(emitted[0]["headers"])[b"x-trace-exported"] == b"false"
        assert len(exports) == 1
    finally:
        runtime.close()
