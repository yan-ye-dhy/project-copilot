"""D6请求关联与实际body大小上限；纯ASGI，不访问模型或存储。"""

import asyncio
import logging
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from project_copilot.api import rag
from project_copilot.config import Settings
from project_copilot.infrastructure.tracing import MULTIPART_OVERHEAD_BYTES
from project_copilot.main import create_app


def test_server_run_id_is_unique_and_logs_exclude_payload_headers_and_query(caplog):
    settings = Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None)

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return [await client.get('/health?private=secret-value', headers={
                    "X-Run-ID": "client-provided-id", "Authorization": "Bearer private-value",
                }) for _ in range(2)]

    with caplog.at_level(logging.INFO, logger="project_copilot.infrastructure.tracing"):
        responses = asyncio.run(run())
    ids = [response.headers['X-Run-ID'] for response in responses]
    assert all(response.status_code == 200 for response in responses)
    assert len(set(ids)) == 2
    assert all(str(UUID(value)) == value for value in ids)
    logs = [record.getMessage() for record in caplog.records
            if record.name == 'project_copilot.infrastructure.tracing']
    assert len(logs) == 2
    assert all('trace_exported=false' in item and 'status=200' in item for item in logs)
    assert all(any(value in item for item in logs) for value in ids)
    assert all('secret-value' not in item and 'private-value' not in item
               and 'client-provided-id' not in item for item in logs)


@pytest.mark.parametrize('length_mode', ['actual', 'missing', 'underreported'])
def test_body_limit_counts_actual_asgi_bytes_and_rejects_before_model_factory(monkeypatch, length_mode):
    def forbidden(*args, **kwargs):
        pytest.fail('超限请求不得进入模型工厂')
    monkeypatch.setattr(rag, 'create_answer_service', forbidden)
    settings = Settings(_env_file=None, upload_max_bytes=16, zhipu_api_key=None, deepseek_api_key=None)
    oversized = b'x' * (16 + MULTIPART_OVERHEAD_BYTES + 1)

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                headers = {'Content-Type': 'application/json'}
                if length_mode == 'underreported':
                    headers['Content-Length'] = '1'
                if length_mode == 'missing':
                    async def stream():
                        yield oversized[:30000]
                        yield oversized[30000:]
                    content = stream()
                else:
                    content = oversized
                return await client.post('/rag/ask', content=content, headers=headers)

    response = asyncio.run(run())
    assert response.status_code == 413
    assert UUID(response.headers['X-Run-ID'])


def test_handled_422_and_404_responses_also_have_run_ids(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('非法问题不得创建模型客户端')
    monkeypatch.setattr(rag, 'create_answer_service', forbidden)

    async def run():
        app = create_app(Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                return [await client.post('/rag/ask', json={'question': '   '}),
                        await client.get('/does-not-exist')]

    responses = asyncio.run(run())
    assert [item.status_code for item in responses] == [422, 404]
    assert all(UUID(item.headers['X-Run-ID']) for item in responses)


def test_unexpected_error_response_is_safe_and_has_same_run_id(monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError('private-upstream-body')
    monkeypatch.setattr(rag, 'create_answer_service', broken)

    async def run():
        app = create_app(Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test',
            ) as client:
                return await client.post('/rag/ask', json={'question': '问题'})

    response = asyncio.run(run())
    assert response.status_code == 500
    run_id = response.headers['X-Run-ID']
    assert str(UUID(run_id)) == run_id == response.json()['run_id']
    assert 'private-upstream-body' not in response.text
    assert all('private-upstream-body' not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize('values', [{'upload_max_bytes': 0}, {'upload_max_bytes': 50*1024*1024+1},
                                 {'trace_export_timeout_seconds': 0}, {'phoenix_endpoint': 'http://localhost:6007'},
                                 {'phoenix_endpoint': 'http://user:password@localhost:6007/v1/traces'}])
def test_invalid_export_or_upload_settings_fail_configuration(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)
