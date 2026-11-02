"""Real isolated PostgreSQL authentication with in-memory synthetic Qdrant."""
from contextlib import contextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from project_copilot.api import documents
from project_copilot.config import Settings
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.schemas.retrieval import DenseIndexSpec
from project_copilot.services.auth import AuthService, csrf_token


class OfflineEmbedding:
    model = "embedding-3"
    expected_dimensions = 2

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


def test_public_list_and_admin_delete_upload_remains_fail_closed(ticket_database, tmp_path, monkeypatch):
    db = ticket_database
    password = "isolation-password-2894"
    with db.transaction() as session:
        auth = AuthService(session)
        auth.bootstrap_admin(RegisterInput(username="admin", password=password))
        auth.register(RegisterInput(username="alice", password=password), open_registration=True)
        tokens = {name: auth.login(LoginInput(username=name, password=password)).token for name in ("admin", "alice")}
    client = QdrantClient(":memory:")
    split = SplitConfig()
    spec = DenseIndexSpec(collection_name="pc_public_documents_test", embedding_model="embedding-3",
                          dimensions=2, splitter_version=split.index_version)
    manifest = ManifestStore(tmp_path / "manifests", spec)
    store = DenseQdrantStore(client, collection_name=spec.collection_name, dimensions=2)
    file_path = tmp_path / "public.txt"
    file_path.write_text("用于删除权限测试的合成文档。", encoding="utf-8")
    document = IngestionService(OfflineEmbedding(), store, manifest, split_config=split).ingest_file(file_path)
    SnapshotManager(manifest).synchronize(client)

    @contextmanager
    def fake_store(_settings):
        yield SimpleNamespace(_client=client)

    monkeypatch.setattr(documents, "create_qdrant_store", fake_store)
    monkeypatch.setattr(documents, "_manifest", lambda _: manifest)
    monkeypatch.setattr(documents, "_ingest_upload", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call embeddings")))
    app = FastAPI()
    app.state.database = db
    app.state.settings = Settings(_env_file=None, public_demo=True, public_proxy_secret="a" * 32,
                                  auth_public_origin="https://demo.example")
    app.include_router(documents.router)
    cookies = {name: {"__Host-pc_session": token} for name, token in tokens.items()}
    headers = {name: {"Origin": "https://demo.example", "X-CSRF-Token": csrf_token(token)}
               for name, token in tokens.items()}
    try:
        with TestClient(app) as http:
            assert http.get("/documents").status_code == 401
            listing = http.get("/documents", cookies=cookies["alice"])
            assert listing.status_code == 200
            assert listing.json()["documents"][0]["document_id"] == str(document.document_id)
            path = "/documents/" + str(document.document_id)
            assert http.delete(path, cookies=cookies["alice"], headers=headers["alice"]).status_code == 403
            assert http.delete(path, cookies=cookies["admin"]).status_code == 403
            for name, code in (("alice", 403), ("admin", 503)):
                response = http.post("/documents/upload", cookies=cookies[name], headers=headers[name],
                                     files={"file": ("new.txt", b"synthetic", "text/plain")})
                assert response.status_code == code
            deleted = http.delete(path, cookies=cookies["admin"], headers=headers["admin"])
            assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
            assert http.get("/documents", cookies=cookies["alice"]).json()["documents"] == []
            assert http.delete(path, cookies=cookies["admin"], headers=headers["admin"]).status_code == 404
    finally:
        client.close()
