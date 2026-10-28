"""Real OTel SDK and controlled OTLP transport, distinct from Phoenix acceptance."""
import asyncio
from dataclasses import dataclass
import json
import logging
from threading import Lock

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from opentelemetry.trace import StatusCode
import pytest
from starlette.responses import Response

from project_copilot.config import Settings
from project_copilot.infrastructure import tracing
from project_copilot.main import create_app


def settings(**values):
    return Settings(_env_file=None, environment='test', database_url=None,
                    tracing_enabled=True, **values)


@pytest.fixture
def capture(monkeypatch):
    batches = []
    lock = Lock()
    def export(self, spans):
        with lock:
            batches.append(list(spans))
        return tracing.TraceExportResult(True, 'accepted', len(spans))
    monkeypatch.setattr(tracing.SingleAttemptOTLPExporter, 'export', export)
    return batches


def test_sdk_parentage_thread_propagation_and_metadata_only_encoding():
    runtime = tracing.TraceRuntime(settings(phoenix_project='controlled-test'))
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200)
    runtime.exporter.client.close()
    runtime.exporter.client = httpx.Client(transport=httpx.MockTransport(transport), trust_env=False)
    async def run():
        with runtime.activate('run-test') as parent:
            with tracing.trace_span('stage', {'count':3, 'api_key':'private-key', 'prompt':'private-prompt'}) as stage:
                await asyncio.to_thread(lambda: _worker())
            return f'{parent.get_span_context().trace_id:032x}'
    def _worker():
        with tracing.trace_span('thread-work', {'status':'ok'}):
            assert tracing.current_run_id() == 'run-test'
    try:
        trace_id = asyncio.run(run())
        result = runtime.export(trace_id)
        assert result.exported and result.spans == 3
        assert len(requests) == 1
        wire = ExportTraceServiceRequest.FromString(requests[0].content)
        encoded = str(wire)
        assert 'private-key' not in encoded and 'private-prompt' not in encoded
        assert 'openinference.project.name' in encoded and 'controlled-test' in encoded
        spans = wire.resource_spans[0].scope_spans[0].spans
        by_name = {s.name:s for s in spans}
        assert by_name['thread-work'].parent_span_id == by_name['stage'].span_id
        assert by_name['stage'].parent_span_id == by_name['http.request'].span_id
        assert all(s.trace_id.hex() == trace_id for s in spans)
        assert all(any(a.key=='copilot.run_id' and a.value.string_value=='run-test' for a in s.attributes) for s in spans)
        assert tracing.current_run_id() is None and tracing.current_trace_id() is None
    finally:
        runtime.close()


@pytest.mark.parametrize('status,body,expected_reason', [
    (503, b'private collector response', 'http_503'),
    (200, b'invalid protobuf', 'DecodeError'),
    (200, ExportTraceServiceResponse(partial_success={'rejected_spans':1,'error_message':'private'}).SerializeToString(), 'partial_rejection'),
])
def test_otlp_failure_is_not_acknowledged_or_retried(status, body, expected_reason):
    runtime=tracing.TraceRuntime(settings())
    calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(status,content=body)
    runtime.exporter.client.close()
    runtime.exporter.client=httpx.Client(transport=httpx.MockTransport(transport),trust_env=False)
    try:
        with runtime.activate('failure') as root:
            trace_id=f'{root.get_span_context().trace_id:032x}'
        result=runtime.export(trace_id)
        assert not result.exported and result.reason==expected_reason and len(calls)==1
        assert 'private' not in result.reason
    finally:runtime.close()


def test_network_failure_diagnostics_contain_only_error_type():
    runtime=tracing.TraceRuntime(settings())
    def transport(request):
        raise httpx.ReadTimeout('private token and endpoint')
    runtime.exporter.client.close()
    runtime.exporter.client=httpx.Client(transport=httpx.MockTransport(transport),trust_env=False)
    try:
        with runtime.activate('timeout') as root:
            trace_id=f'{root.get_span_context().trace_id:032x}'
        result=runtime.export(trace_id)
        assert not result.exported and result.reason=='ReadTimeout'
    finally:runtime.close()


def test_concurrent_requests_have_independent_acknowledgement_and_run_ids(capture):
    async def run():
        app=create_app(settings())
        @app.get('/traced')
        def endpoint():
            with tracing.trace_span('sync-handler', {'status':'ok'}):
                return {'status':'ok','trace_exported':False}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                return await asyncio.gather(*(client.get('/traced?secret=private',headers={'Authorization':'private'}) for _ in range(4)))
    responses=asyncio.run(run())
    assert len(capture)==4
    assert len({r.headers['x-trace-id'] for r in responses})==4
    for response in responses:
        assert response.status_code==200 and response.headers['x-trace-exported']=='true'
        assert response.json()['trace_exported'] is True
        assert int(response.headers['content-length'])==len(response.content)
        trace_id=response.headers['x-trace-id'];run_id=response.headers['x-run-id']
        batch=next(batch for batch in capture if f'{batch[0].context.trace_id:032x}'==trace_id)
        assert len(batch)==2 and all(span.attributes['copilot.run_id']==run_id for span in batch)
        assert 'private' not in str([dict(span.attributes) for span in batch])


def test_export_failure_preserves_business_response_and_has_diagnostic(monkeypatch,caplog):
    monkeypatch.setattr(tracing.SingleAttemptOTLPExporter,'export',lambda self,spans:tracing.TraceExportResult(False,'http_503',len(spans)))
    async def run():
        app=create_app(settings())
        @app.get('/receipt')
        def endpoint():return {'status':'completed','ticket_id':'real-id','trace_exported':False}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                return await client.get('/receipt')
    with caplog.at_level(logging.WARNING,logger='project_copilot.infrastructure.tracing'):
        response=asyncio.run(run())
    assert response.status_code==200 and response.json()['ticket_id']=='real-id'
    assert response.json()['trace_exported'] is False and response.headers['x-trace-exported']=='false'
    assert 'trace_export_failed' in caplog.text and 'http_503' in caplog.text


def test_real_sdk_error_span_is_exported_without_exception_message(capture,caplog):
    async def run():
        app=create_app(settings())
        @app.get('/broken')
        async def endpoint():
            with tracing.trace_span('failing-stage'):
                raise RuntimeError('private exception message')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
                return await client.get('/broken')
    response=asyncio.run(run())
    assert response.status_code==500 and response.headers['x-trace-exported']=='true'
    assert len(capture)==1
    assert all(span.status.status_code==StatusCode.ERROR for span in capture[0])
    assert all(span.attributes['error.type']=='RuntimeError' for span in capture[0])
    assert 'private exception message' not in response.text+caplog.text+str([dict(s.attributes) for s in capture[0]])
    assert all(not span.events for span in capture[0])


def test_disabled_tracing_constructs_no_exporter(monkeypatch):
    def forbidden(*args,**kwargs):pytest.fail('disabled tracing constructed exporter')
    monkeypatch.setattr(tracing.SingleAttemptOTLPExporter,'__init__',forbidden)
    async def run():
        app=create_app(Settings(_env_file=None,tracing_enabled=False,database_url=None))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                return await client.get('/health')
    response=asyncio.run(run())
    assert response.status_code==200 and 'x-trace-id' not in response.headers
    with tracing.trace_span('disabled') as span:
        assert not span.is_recording()


def test_bounded_response_buffer_preserves_large_response(capture,monkeypatch):
    monkeypatch.setattr(tracing,'MAX_BUFFERED_RESPONSE_BYTES',4)
    async def run():
        app=create_app(settings())
        @app.get('/large')
        def endpoint():return Response(b'abcdef',media_type='application/octet-stream')
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                return await client.get('/large')
    response=asyncio.run(run())
    assert response.content==b'abcdef' and response.status_code==200
    assert response.headers['x-trace-exported']=='false'  # not yet acknowledged when streamed
    assert len(capture)==1
