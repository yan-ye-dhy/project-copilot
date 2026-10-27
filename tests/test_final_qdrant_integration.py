"""多进程共享清单及真实Qdrant；独立测试集合按用户要求保留，不删除服务数据。"""

import multiprocessing as mp
import os
from pathlib import Path
from uuid import uuid4

import pytest
from qdrant_client import QdrantClient

from project_copilot.infrastructure.qdrant import DenseQdrantStore
from project_copilot.rag.final_retrieval import SnapshotManager
from project_copilot.rag.ingestion import IngestionService, ManifestStore
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec


pytestmark = pytest.mark.skipif(os.environ.get("PROJECT_COPILOT_RUN_FINAL_QDRANT") != "1",
                                reason="显式启用真实Qdrant及多进程测试")


class Vectors:
    model = "embedding-3"
    expected_dimensions = 2048

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * 2047 for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * 2047


def worker(url, directory, collection, document, barrier, queue):
    client = QdrantClient(url=url, trust_env=False, check_compatibility=False)
    spec = DenseIndexSpec(collection_name=collection, embedding_model="embedding-3", dimensions=2048,
                          splitter_version=SplitConfig().index_version)
    store = DenseQdrantStore(client, collection_name=collection, dimensions=2048)
    try:
        barrier.wait(timeout=15)
        result = IngestionService(Vectors(), store, ManifestStore(Path(directory), spec)).ingest_file(document)
        queue.put(("ok", str(result.document_id)))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))
    finally:
        client.close()


def test_two_process_ingestion_and_snapshot_restart_consistency(tmp_path):
    url = os.environ.get("PROJECT_COPILOT_QDRANT_URL", "http://127.0.0.1:6433")
    collection = "pc_test_final_" + uuid4().hex
    paths = [tmp_path / "first.txt", tmp_path / "second.txt"]
    paths[0].write_text("并发写入第一份文档，术语苹果。"); paths[1].write_text("并发写入第二份文档，术语香蕉。")
    context = mp.get_context("spawn")
    barrier, queue = context.Barrier(3), context.Queue()
    processes = [context.Process(target=worker, args=(url, str(tmp_path), collection, str(path), barrier, queue)) for path in paths]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=15)
        for process in processes:
            process.join(timeout=30)
        assert all(not p.is_alive() and p.exitcode == 0 for p in processes)
        outputs = [queue.get(timeout=3), queue.get(timeout=3)]
        assert all(result[0] == "ok" for result in outputs)
        spec = DenseIndexSpec(collection_name=collection, embedding_model="embedding-3", dimensions=2048,
                              splitter_version=SplitConfig().index_version)
        manifest = ManifestStore(tmp_path, spec)
        assert {str(d.document_id) for d in manifest.load().documents} == {item[1] for item in outputs}
        client = QdrantClient(url=url, trust_env=False, check_compatibility=False)
        try:
            assert client.count(collection, exact=True).count == 2
            first = SnapshotManager(manifest)
            built = first.synchronize(client)
            assert len(built.snapshot.chunks) == 2
            inode = first.path.stat().st_ino
            # New manager models a restarted process and loads the published snapshot without rebuilding.
            restarted = SnapshotManager(manifest)
            restored = restarted.synchronize(client)
            assert restarted.path.stat().st_ino == inode
            assert restored.snapshot.manifest == manifest.load()
            assert restored.search("苹果")[0].metadata.document_id != restored.search("香蕉")[0].metadata.document_id
        finally:
            client.close()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    # No collection deletion: retain only the newly-created scoped test data.
