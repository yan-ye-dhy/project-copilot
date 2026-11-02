"""Isolated PDF extraction: no provider calls or external services."""
import os
from pathlib import Path
from time import monotonic

import pytest

from project_copilot.rag import pdf_sandbox
from project_copilot.rag import loaders
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.loaders import DocumentLoadError, load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.schemas.retrieval import DenseIndexSpec
from qdrant_client import QdrantClient
from test_documents import _write_pdf


FIXTURES = Path(__file__).parent / "fixtures" / "documents"


def test_real_pdf_pages_blank_page_hash_and_chunks_match_existing_loader():
    source = FIXTURES / "sample.pdf"
    local = load_document(source)
    isolated = load_document(source, isolated_pdf=True)
    assert isolated == local
    assert len(isolated.pages) == 3 and isolated.pages[1].text == ""
    assert [page.page for page in isolated.pages] == [0, 1, 2]
    assert split_document(isolated) == split_document(local)


def test_bounded_pdf_rejects_page_and_character_budget(tmp_path):
    sample = tmp_path / "budget.pdf"
    _write_pdf(sample, ["First page", "Second page"])
    with pytest.raises(DocumentLoadError, match="资源限制"):
        load_document(sample, isolated_pdf=True, pdf_max_pages=1)
    with pytest.raises(DocumentLoadError, match="资源限制"):
        load_document(sample, isolated_pdf=True, pdf_max_chars=4)
    assert len(load_document(sample, isolated_pdf=True, pdf_max_pages=2, pdf_max_chars=100).pages) == 2


def test_oversized_and_malformed_inputs_fail_closed(tmp_path):
    malformed = tmp_path / "malformed.pdf"
    malformed.write_bytes(b"not a pdf")
    with pytest.raises(DocumentLoadError, match="PDF 解析失败"):
        load_document(malformed, isolated_pdf=True)
    oversized = tmp_path / "large.pdf"
    oversized.write_bytes(b"x" * (pdf_sandbox.MAX_INPUT_BYTES + 1))
    with pytest.raises(DocumentLoadError, match="PDF 解析失败"):
        load_document(oversized, isolated_pdf=True)


def test_parent_wall_timeout_kills_parser_process(tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "hung_child.py"
    script.write_text(f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
    monkeypatch.setattr(pdf_sandbox, "_WORKER", script)
    started = monotonic()
    with pytest.raises(pdf_sandbox.IsolatedPdfError):
        pdf_sandbox.extract_pdf(b"example", timeout_seconds=0.5)
    assert monotonic() - started < 5
    assert pidfile.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_no_nonfinite_timeout_or_nonlinux_fallback(monkeypatch):
    with pytest.raises(pdf_sandbox.IsolatedPdfError):
        pdf_sandbox.extract_pdf(b"example", timeout_seconds=float("nan"))
    monkeypatch.setattr(pdf_sandbox.sys, "platform", "win32")
    with pytest.raises(pdf_sandbox.IsolatedPdfError):
        pdf_sandbox.extract_pdf(b"example")


def test_parser_receives_no_parent_api_secret_or_filename(tmp_path, monkeypatch):
    script = tmp_path / "environment_probe.py"
    script.write_text(
        "import json, os, sys\n"
        "assert 'PROJECT_COPILOT_DEEPSEEK_API_KEY' not in os.environ\n"
        "assert 'PRIVATE_PDF_SENTINEL' not in str(sys.argv)\n"
        "assert sys.stdin.buffer.read() == b'PRIVATE_PDF_SENTINEL'\n"
        "print(json.dumps({'pages': ['ok']}))\n"
    )
    monkeypatch.setenv("PROJECT_COPILOT_DEEPSEEK_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setattr(pdf_sandbox, "_WORKER", script)
    assert pdf_sandbox.extract_pdf(b"PRIVATE_PDF_SENTINEL") == ("ok",)


def test_isolated_ingestion_keeps_chunks_and_never_parses_again_in_parent(tmp_path, monkeypatch):
    class FakeEmbeddings:
        model = "embedding-3"
        expected_dimensions = 2

        def __init__(self):
            self.texts = []

        def embed_documents(self, texts):
            self.texts.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    config = SplitConfig()
    spec = DenseIndexSpec(collection_name="pc_pdf_isolated_test", embedding_model="embedding-3",
                          dimensions=2, splitter_version=config.index_version)
    local_client, isolated_client = QdrantClient(":memory:"), QdrantClient(":memory:")
    try:
        local_embeddings, isolated_embeddings = FakeEmbeddings(), FakeEmbeddings()
        local = IngestionService(local_embeddings, DenseQdrantStore(local_client, collection_name=spec.collection_name, dimensions=2),
                                 ManifestStore(tmp_path / "local", spec))
        isolated = IngestionService(isolated_embeddings, DenseQdrantStore(isolated_client, collection_name=spec.collection_name, dimensions=2),
                                    ManifestStore(tmp_path / "isolated", spec), isolated_pdf=True,
                                    max_chars=32000, max_chunks=64)
        first = local.ingest_file(FIXTURES / "sample.pdf", source_name="sample.pdf")

        def forbidden(_):
            pytest.fail("public path must not parse PDF in the parent process")

        monkeypatch.setattr(loaders, "_load_pdf_pages", forbidden)
        second = isolated.ingest_file(FIXTURES / "sample.pdf", source_name="sample.pdf")
        assert second == first and isolated_embeddings.texts == local_embeddings.texts
        point_ids = [str(chunk_id) for chunk_id in first.chunk_ids]
        a = local_client.retrieve(spec.collection_name, point_ids, with_payload=True, with_vectors=False)
        b = isolated_client.retrieve(spec.collection_name, point_ids, with_payload=True, with_vectors=False)
        assert {str(item.id): item.payload for item in a} == {str(item.id): item.payload for item in b}
    finally:
        local_client.close(); isolated_client.close()
