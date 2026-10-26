"""D18本地恢复验收：真实Qdrant＋合成二维向量，不读取.env或调用模型。"""

import argparse
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import uuid4

from qdrant_client import QdrantClient

from project_copilot.config import PROJECT_ROOT
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantRequestError
from project_copilot.rag.bm25 import BM25Index, read_completed_chunks
from project_copilot.rag.hybrid import HybridError, HybridRetriever
from project_copilot.rag.ingestion import IngestionService, ManifestError, ManifestStore
from project_copilot.rag.loaders import load_document
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import DenseIndexSpec


class SyntheticEmbeddings:
    model = "d18-synthetic-vectors"
    expected_dimensions = 2

    def __init__(self):
        self.document_batches = 0

    def embed_documents(self, texts):
        self.document_batches += 1
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, _text):
        return [1.0, 0.0]


class FailOnceStore(DenseQdrantStore):
    fail_next = False

    def upsert(self, chunks, vectors, *, dense_index_version):
        if self.fail_next:
            self.fail_next = False
            super().upsert(chunks[:1], vectors[:1], dense_index_version=dense_index_version)
            raise QdrantRequestError("D18主动注入：仅写入首片段后中断")
        super().upsert(chunks, vectors, dense_index_version=dense_index_version)


class FailOnceManifest(ManifestStore):
    fail_next = False

    def record(self, manifest, document):
        if self.fail_next:
            self.fail_next = False
            raise ManifestError("D18主动注入：向量完成后、清单保存前中断")
        super().record(manifest, document)


def run_recovery(output_dir: Path, *, client=None) -> dict:
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise ValueError("输出目录已存在，拒绝覆盖")
    output.mkdir(parents=True, exist_ok=False)
    started = perf_counter()
    collection = "pc_d18_recovery_" + uuid4().hex
    embeddings, split = SyntheticEmbeddings(), SplitConfig()
    spec = DenseIndexSpec(collection_name=collection, embedding_model=embeddings.model,
                          dimensions=2, splitter_version=split.index_version)
    manifest = FailOnceManifest(output / "manifests", spec)
    active = client if client is not None else QdrantClient(
        url="http://127.0.0.1:6433", timeout=10, check_compatibility=False, trust_env=False,
    )
    store = FailOnceStore(active, collection_name=collection, dimensions=2)
    owned = False
    report = {"status": "running", "mode": "local-qdrant-synthetic-vectors", "model_api_calls": 0,
              "transport": "qdrant-http" if client is None else "injected-client",
              "synthetic_dimensions": 2, "collection": collection, "events": [],
              "test_collection_deleted": False, "semantic_quality_measured": False}

    def ingest(path, **kwargs):
        return IngestionService(embeddings, store, manifest).ingest_file(path, **kwargs)

    def snapshot(name):
        complete = manifest.load()
        dense = DenseRetriever(embeddings, store, manifest, top_k=100).search("恢复")
        bm25 = BM25Index.build(complete, read_completed_chunks(active, complete))
        expected = {str(d.document_id) for d in complete.documents}
        assert {str(h.metadata.document_id) for h in dense} == expected
        assert {str(c.metadata.document_id) for c in bm25.snapshot.chunks} == expected
        report["events"].append({"name": name, "registered_documents": len(complete.documents),
            "points": active.count(collection, exact=True).count, "visible_document_ids": sorted(expected),
            "synthetic_embedding_batches": embeddings.document_batches,
            "manifest_sha256": sha256(manifest.path.read_bytes()).hexdigest()})

    try:
        if active.collection_exists(collection):
            raise ValueError("随机测试集合已存在，拒绝使用")
        owned = True
        report["qdrant_version"] = active.info().version
        report["qdrant_client_version"] = version("qdrant-client")
        report["code"] = {
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True).strip()),
        }
        first = output / "first.txt";first.write_text("首次完成的独立说明文档。", encoding="utf-8")
        done = ingest(first)
        snapshot("first_completed")
        previous = manifest.path.read_bytes()
        old_bm25 = BM25Index.build(manifest.load(), read_completed_chunks(active, manifest.load()))
        pending = output / "same.txt"
        pending.write_text("\n\n".join(f"段落{i}：失败后按稳定标识重试，未完成文档不能作为检索证据。" * 12 for i in range(6)), encoding="utf-8")
        chunks = split_document(load_document(pending))
        assert len(chunks) > 1
        store.fail_next = True
        try:
            ingest(pending)
        except QdrantRequestError:
            pass
        else:
            raise AssertionError("部分写入故障未触发")
        assert manifest.path.read_bytes() == previous
        assert active.count(collection, exact=True).count == len(done.chunk_ids) + 1
        snapshot("partial_write_not_published")
        manifest = FailOnceManifest(output / "manifests", spec)
        retried = ingest(pending)
        assert retried.chunk_ids == tuple(c.metadata.chunk_id for c in chunks)
        assert active.count(collection, exact=True).count == len(done.chunk_ids) + len(chunks)
        snapshot("partial_retry_same_ids")
        try:
            HybridRetriever(DenseRetriever(embeddings, store, manifest, top_k=10), old_bm25).search("恢复")
        except HybridError:
            report["stale_bm25_rejected"] = True
        else:
            raise AssertionError("旧BM25快照未被拒绝")
        before = embeddings.document_batches
        assert ingest(pending, source_name="renamed.txt") == retried
        assert embeddings.document_batches == before
        snapshot("duplicate_skips_embedding_and_write")
        pending.write_text("相同文件名的新内容，与旧内容作为不同文档保留。", encoding="utf-8")
        changed = ingest(pending)
        assert changed.document_id != retried.document_id and changed.source_name == retried.source_name
        snapshot("same_name_new_content_is_separate")
        unrecorded = output / "manifest-failure.txt"
        unrecorded.write_text("\n\n".join(f"清单恢复段落{i}：只在全部片段确认后登记文档。" * 14 for i in range(4)), encoding="utf-8")
        previous = manifest.path.read_bytes();manifest.fail_next = True
        try:
            ingest(unrecorded)
        except ManifestError:
            pass
        else:
            raise AssertionError("清单保存故障未触发")
        assert manifest.path.read_bytes() == previous
        snapshot("manifest_failure_not_published")
        points = active.count(collection, exact=True).count
        manifest = FailOnceManifest(output / "manifests", spec)
        recovered = ingest(unrecorded)
        assert active.count(collection, exact=True).count == points
        snapshot("manifest_retry_same_point_count")
        before = embeddings.document_batches
        assert ingest(unrecorded) == recovered and embeddings.document_batches == before
        snapshot("final_duplicate_is_read_only")
        assert not list((output / "manifests").glob(".manifest-*.tmp"))
        report.update({"status": "ok", "synthetic_embedding_batches": embeddings.document_batches,
                       "failed_document_excluded_from_dense_and_bm25": True})
        sources = ["src/project_copilot/rag/ingestion.py", "src/project_copilot/infrastructure/qdrant.py",
                   "src/project_copilot/api/documents.py", "scripts/ingestion_recovery_smoke.py"]
        report["source_sha256"] = {p: sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in sources}
    except Exception:
        report["status"] = "error"
        raise
    finally:
        try:
            if owned and active.collection_exists(collection):
                active.delete_collection(collection)
                report["test_collection_deleted"] = not active.collection_exists(collection)
                if not report["test_collection_deleted"]:
                    raise RuntimeError("未确认测试集合清理完成")
        except Exception:
            report["status"] = "error"
            raise
        finally:
            active.close()
            report["elapsed_ms"] = (perf_counter() - started) * 1000
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not args.local:
        parser.error("本地Qdrant恢复验收需显式--local；不读取.env或调用模型")
    if args.output_dir is None:
        parser.error("请提供尚不存在的--output-dir")
    try:
        result = run_recovery(args.output_dir)
    except Exception:
        print(json.dumps({"status": "error", "message": "本地恢复验收失败；检查服务和局部report，无自动重试"}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": result["status"], "events": len(result["events"]), "model_api_calls": 0,
                      "test_collection_deleted": result["test_collection_deleted"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
