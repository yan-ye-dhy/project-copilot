"""Opt-in isolated PostgreSQL + HTTP Qdrant upload/delete, fake embedding only."""
from contextlib import contextmanager
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from project_copilot.api.documents import router as documents_router
from project_copilot.infrastructure.call_accounting import record_provider_attempt
from project_copilot.rag import final_retrieval, ingestion
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.services.ai_worker import process_one
from project_copilot.services.auth import AuthService
from test_public_jobs import app, headers

pytestmark = pytest.mark.skipif(os.environ.get("PC_TEST_WEB_QDRANT") != "1", reason="requires isolated HTTP Qdrant on 16433")


def test_queued_upload_worker_and_delete_use_real_services(ticket_database, tmp_path, monkeypatch):
    db = ticket_database
    password = "synthetic-admin-password-123"
    with db.transaction() as session:
        auth = AuthService(session)
        auth.bootstrap_admin(RegisterInput(username="admin1", password=password))
        token = auth.login(LoginInput(username="admin1", password=password)).token
    application = app(db)
    name = "pc_web_pipeline_" + uuid4().hex
    application.state.settings = application.state.settings.model_copy(update={
        "qdrant_url": "http://127.0.0.1:16433", "qdrant_collection_name": name,
        "embedding_expected_dimensions": 2, "manifest_dir": tmp_path / "manifest",
    })
    split = SplitConfig()
    spec = DenseIndexSpec(collection_name=name, embedding_model="embedding-3", dimensions=2,
                          splitter_version=split.index_version)
    manifest = ManifestStore(tmp_path / "manifest", spec)

    class FakeEmbedding:
        model = "embedding-3"
        expected_dimensions = 2
        def embed_documents(self, texts):
            record_provider_attempt()  # no external model; count this synthetic boundary
            return [[1.0, 0.0] for _ in texts]

    @contextmanager
    def embedding_client(_settings):
        yield FakeEmbedding()

    class Sync:
        def synchronize(self, client):
            return SnapshotManager(manifest).synchronize(client)

    monkeypatch.setattr(ingestion, "create_embedding_client", embedding_client)
    monkeypatch.setattr(final_retrieval, "get_final_retriever", lambda _: Sync())
    application.include_router(documents_router)
    client = QdrantClient(url="http://127.0.0.1:16433", check_compatibility=False)
    cookie = {"__Host-pc_session": token}
    auth_headers = headers(token, "isolated-upload-key")
    try:
        with TestClient(application) as http:
            upload = http.post("/jobs/upload", cookies=cookie, headers=auth_headers,
                               files={"file": ("synthetic.txt", "隔离研发资料".encode(), "text/plain")})
            assert upload.status_code == 202
            job_id = upload.json()["job_id"]
            assert process_one(db, application.state.settings)
            status = http.get("/jobs/" + job_id, cookies=cookie).json()
            assert status["status"] == "succeeded" and status["result"]["status"] == "indexed"
            document_id = status["result"]["document"]["document_id"]
            assert http.get("/documents", cookies=cookie).json()["documents"][0]["document_id"] == document_id
            assert http.delete("/documents/" + document_id, cookies=cookie, headers=auth_headers).status_code == 200
            assert http.get("/documents", cookies=cookie).json()["documents"] == []
    finally:
        if client.collection_exists(name):
            client.delete_collection(name)
        client.close()
