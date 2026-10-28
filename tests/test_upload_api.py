"""D6上传HTTP契约与资源收尾；所有入库服务均为离线fake。"""

import asyncio
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from project_copilot.api import documents
from project_copilot.config import Settings
from project_copilot.infrastructure.models import (
    EmbeddingConfigError, EmbeddingRequestError, EmbeddingResponseError,
)
from project_copilot.infrastructure.qdrant import (
    QdrantConfigError, QdrantRequestError, QdrantResponseError,
)
from project_copilot.main import create_app
from project_copilot.rag.ingestion import IngestionError, ManifestError
from project_copilot.schemas.retrieval import CompletedDocument
from project_copilot.schemas.uploads import DocumentUploadResult


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, zhipu_api_key=None, deepseek_api_key=None,
                    manifest_dir=tmp_path / "manifests", environment="test")


def request(application, method="POST", path="/documents/upload", **kwargs) -> httpx.Response:
    async def run():
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)
    return asyncio.run(run())


@pytest.fixture
def upload_resources(monkeypatch):
    paths = []
    closed_files = []
    original_temporary = documents.tempfile.NamedTemporaryFile
    original_close = UploadFile.close

    def temporary(*args, **kwargs):
        handle = original_temporary(*args, **kwargs)
        paths.append(Path(handle.name))
        return handle

    async def close(upload):
        await original_close(upload)
        closed_files.append(upload.file)

    monkeypatch.setattr(documents.tempfile, "NamedTemporaryFile", temporary)
    monkeypatch.setattr(UploadFile, "close", close)
    return paths, closed_files


@pytest.mark.parametrize("client_name,source_name", [
    ("上传知识.TXT", "上传知识.TXT"), ("C:\\docs\\escape.txt", "escape.txt"),
])
def test_success_uses_random_path_preserves_name_and_closes_upload(
    monkeypatch, settings, upload_resources, client_name, source_name,
):
    paths, closed_files = upload_resources
    events = []
    payload = "上传内容。".encode()
    completed = CompletedDocument(
        document_id=UUID(int=100), content_hash="a" * 64,
        source_name=source_name, chunk_ids=(UUID(int=1), UUID(int=2)),
    )

    class FakeService:
        manifest = SimpleNamespace(spec=SimpleNamespace(index_version="dense-v1-offline-test"))

        def ingest_file(self, path, *, source_name):
            assert path.is_file()
            assert path.read_bytes() == payload
            assert path.name != source_name
            assert path.suffix == ".txt"
            assert source_name == completed.source_name
            events.append("ingested")
            return completed

    @contextmanager
    def factory(active):
        assert active is settings
        events.append("opened")
        try:
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(documents, "create_ingestion_service", factory)
    response = request(create_app(settings), files={"file": (client_name, payload, "text/plain")},
                       headers={"X-Run-ID": "untrusted-client-value"})

    assert response.status_code == 200
    result = DocumentUploadResult.model_validate(response.json())
    assert result.document == completed
    assert result.chunk_count == 2 and result.size_bytes == len(payload)
    assert result.status == "indexed" and result.mode == "HYBRID_RERANK"
    assert result.index_version == "dense-v1-offline-test"
    assert result.trace_exported is False
    assert str(result.run_id) == response.headers["X-Run-ID"]
    assert result.run_id.version == 4
    assert events == ["opened", "ingested", "closed"]
    assert len(paths) == 1 and not paths[0].exists()
    assert len(closed_files) == 1 and closed_files[0].closed


@pytest.mark.parametrize("case,status", [
    ("missing", 422), ("wrong_field", 422), ("ordinary_field", 422),
    ("extra_field", 400), ("duplicate_files", 400), ("multiple_fields", 400),
    ("json", 415),
])
def test_form_contract_rejects_before_creating_service(monkeypatch, settings, case, status):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法表单不能创建入库或模型客户端")

    monkeypatch.setattr(documents, "create_ingestion_service", forbidden)
    file = ("sample.txt", b"valid text", "text/plain")
    requests = {
        "missing": {"content": b"--offline-boundary--\r\n",
                    "headers": {"Content-Type": "multipart/form-data; boundary=offline-boundary"}},
        "wrong_field": {"files": {"document": file}},
        "ordinary_field": {"files": {"file": (None, "text instead of a file")}},
        "extra_field": {"files": {"file": file}, "data": {"mode": "HYBRID"}},
        "duplicate_files": {"files": [("file", file), ("file", file)]},
        "multiple_fields": {"files": [("file", (None, "text")), ("extra", (None, "text"))]},
        "json": {"json": {"file": "sample.txt"}},
    }
    response = request(create_app(settings), **requests[case])
    assert response.status_code == status
    assert UUID(response.headers["X-Run-ID"]).version == 4


@pytest.mark.parametrize("filename", [
    "../escape.txt", "/tmp/escape.txt", "bad\u200b.txt", "x" * 197 + ".txt",
])
def test_unsafe_names_are_rejected_before_temporary_files_or_services(
    monkeypatch, settings, upload_resources, filename,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("非法文件名不能创建入库服务")

    monkeypatch.setattr(documents, "create_ingestion_service", forbidden)
    response = request(create_app(settings), files={"file": (filename, b"valid text", "text/plain")})
    assert response.status_code == 422
    assert upload_resources[0] == []
    assert all(handle.closed for handle in upload_resources[1])


@pytest.mark.parametrize("filename,media_type,payload,status", [
    ("program.exe", "application/octet-stream", b"not supported", 415),
    ("text.txt", "application/pdf", b"plain text", 415),
    ("book.pdf", "text/plain", b"%PDF", 415),
    ("empty.txt", "text/plain", b"", 422),
    ("large.txt", "text/plain", b"12345", 413),
])
def test_type_empty_and_file_size_boundaries_skip_service_and_cleanup(
    monkeypatch, settings, upload_resources, filename, media_type, payload, status,
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("类型、空文件或超限文件不能创建入库服务")

    monkeypatch.setattr(documents, "create_ingestion_service", forbidden)
    limited = settings.model_copy(update={"upload_max_bytes": 4})
    response = request(create_app(limited), files={"file": (filename, payload, media_type)})
    assert response.status_code == status
    paths, closed_files = upload_resources
    assert all(not path.exists() for path in paths)
    assert closed_files and all(handle.closed for handle in closed_files)


@pytest.mark.parametrize("error_type,status,phase", [
    (EmbeddingConfigError, 503, "open"), (QdrantConfigError, 503, "open"),
    (ManifestError, 503, "open"), (IngestionError, 503, "ingest"),
    (EmbeddingRequestError, 502, "ingest"), (EmbeddingResponseError, 502, "ingest"),
    (QdrantRequestError, 502, "ingest"), (QdrantResponseError, 502, "ingest"),
    (OSError, 500, "ingest"),
])
def test_failure_is_sanitized_and_releases_files_and_fake_service(
    monkeypatch, settings, upload_resources, error_type, status, phase, caplog,
):
    events = []
    private_marker = "private-provider-key-and-document-content"

    class FakeService:
        def ingest_file(self, path, *, source_name):
            assert path.exists()
            events.append("ingest")
            raise error_type(private_marker)

    @contextmanager
    def factory(_settings):
        events.append("opened")
        try:
            if phase == "open":
                raise error_type(private_marker)
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(documents, "create_ingestion_service", factory)
    response = request(create_app(settings), files={"file": ("sample.txt", b"valid", "text/plain")})
    assert response.status_code == status
    assert set(response.json()) == {"detail"} and response.json()["detail"].strip()
    assert private_marker not in response.text and private_marker not in caplog.text
    assert "Traceback" not in response.text
    assert events == (["opened", "closed"] if phase == "open" else ["opened", "ingest", "closed"])
    paths, closed_files = upload_resources
    assert len(paths) == 1 and not paths[0].exists()
    assert closed_files and all(handle.closed for handle in closed_files)


def test_openapi_exposes_one_required_binary_file_and_completed_upload(monkeypatch, settings):
    def forbidden(*_args, **_kwargs):
        pytest.fail("OpenAPI不能创建入库或模型客户端")

    monkeypatch.setattr(documents, "create_ingestion_service", forbidden)
    response = request(create_app(settings), "GET", "/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    operation = schema["paths"]["/documents/upload"]["post"]
    body = operation["requestBody"]
    assert body["required"] is True
    assert set(body["content"]) == {"multipart/form-data"}
    form = body["content"]["multipart/form-data"]["schema"]
    assert form["required"] == ["file"] and form["additionalProperties"] is False
    assert form["properties"] == {"file": {"type": "string", "format": "binary"}}
    result_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    result = schema["components"]["schemas"][result_ref.rsplit("/", 1)[-1]]
    assert set(result["properties"]) == {
        "status", "mode", "document", "chunk_count", "size_bytes", "index_version", "run_id", "trace_exported",
    }
    assert result["properties"]["document"]["$ref"].endswith("/CompletedDocument")
    assert result["properties"]["run_id"]["format"] == "uuid"
    assert result["properties"]["trace_exported"]["type"] == "boolean"
    assert result["properties"]["trace_exported"]["default"] is False


def test_validator_rejects_windows_path_if_parser_has_not_normalized_it():
    # python-multipart会把标准Windows上传路径取basename；校验器仍防御未规范化输入。
    with BytesIO(b"text") as stream:
        upload = UploadFile(stream, filename="C:\\docs\\escape.txt")
        with pytest.raises(HTTPException) as error:
            documents._validate_file(upload)
    assert error.value.status_code == 422


def test_overlapping_upload_returns_409_without_second_ingestion_and_cleans_both_files(
    monkeypatch, settings, upload_resources,
):
    entered = Event()
    release = Event()
    events = []
    completed = CompletedDocument(document_id=UUID(int=100), content_hash="a"*64,
                                  source_name="first.txt", chunk_ids=(UUID(int=1),))

    class FakeService:
        manifest = SimpleNamespace(spec=SimpleNamespace(index_version="dense-v1-offline"))

        def ingest_file(self, path, *, source_name):
            events.append("ingest")
            assert source_name == "first.txt" and path.exists()
            entered.set()
            assert release.wait(timeout=5), "测试没有释放首个入库请求"
            return completed

    @contextmanager
    def factory(active):
        events.append("opened")
        try:
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(documents, "create_ingestion_service", factory)

    async def run():
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first_task = asyncio.create_task(client.post('/documents/upload', files={
                    'file': ('first.txt', b'first', 'text/plain'),
                }))
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    second = await asyncio.wait_for(client.post('/documents/upload', files={
                        'file': ('second.txt', b'second', 'text/plain'),
                    }), timeout=2)
                finally:
                    release.set()
                return await first_task, second

    first, second = asyncio.run(run())
    assert first.status_code == 200 and second.status_code == 409
    assert events == ['opened', 'ingest', 'closed']
    paths, closed_files = upload_resources
    assert len(paths) == 2 and all(not path.exists() for path in paths)
    assert len(closed_files) == 2 and all(handle.closed for handle in closed_files)
