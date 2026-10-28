"""统一业务检索：完成清单绑定的Dense/BM25→RRF→固定版本本地BGE重排。"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
import os
from pathlib import Path
from threading import Lock
from uuid import uuid4

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.config import Settings
from project_copilot.infrastructure.models import create_embedding_client
from project_copilot.infrastructure.qdrant import create_qdrant_store
from project_copilot.rag.bm25 import BM25Error, BM25Index, read_completed_chunks
from project_copilot.rag.hybrid import HybridRetriever
from project_copilot.rag.index_lock import index_lock
from project_copilot.rag.ingestion import ManifestStore
from project_copilot.rag.rerank import LocalCrossEncoder, RerankConfig, RerankError, Reranker
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.retrieval import DenseIndexSpec, RetrievedChunk


class FinalRetrievalError(RuntimeError):
    """最终检索或本地模型不可用，不能退回单路检索。"""


class SnapshotManager:
    """单清单绑定单快照；更新在索引锁内完整写入并原子替换。"""

    def __init__(self, manifest: ManifestStore):
        self.manifest = manifest
        self.path = manifest.path.with_suffix(".bm25.json")
        self._cached: BM25Index | None = None
        self._stat: tuple[int, int, int] | None = None

    def get_locked(self, client) -> BM25Index:
        with trace_span("rag.snapshot", {"openinference.span.kind": "CHAIN"}) as span:
            previous = self._stat
            result = self._get_locked(client)
            span.set_attribute("snapshot.reused", previous is not None and previous == self._stat)
            span.set_attribute("snapshot.chunk_count", len(result.snapshot.chunks))
            return result

    def _get_locked(self, client) -> BM25Index:
        current = self.manifest.load()
        try:
            st = self.path.stat()
            identity = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            identity = None
        except OSError:
            raise BM25Error("无法检查BM25快照") from None
        if self._cached is not None and self._cached.snapshot.manifest == current and identity == self._stat:
            return self._cached
        if identity is not None:
            try:
                index = BM25Index.load(self.path, current)
                self._cached, self._stat = index, identity
                return index
            except BM25Error:
                # Rebuild from the completed manifest; never serve the inconsistent snapshot.
                pass
        chunks = read_completed_chunks(client, current) if current.documents else []
        index = BM25Index.build(current, chunks)
        temporary = self.path.with_name(self.path.name + "." + uuid4().hex + ".tmp")
        try:
            index.save(temporary)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            verified = BM25Index.load(temporary, current)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            st = self.path.stat()
            self._cached, self._stat = verified, (st.st_ino, st.st_mtime_ns, st.st_size)
            return verified
        except (OSError, BM25Error):
            raise BM25Error("BM25快照构建或原子发布失败；未使用旧快照") from None
        finally:
            temporary.unlink(missing_ok=True)

    def synchronize(self, client) -> BM25Index:
        with index_lock(self.manifest.path):
            return self.get_locked(client)


class SharedReranker:
    """每进程复用一个固定缓存模型；串行预热/推断，超时后实例失效。"""

    def __init__(self, settings: Settings):
        config = RerankConfig(device=settings.final_rerank_device, dtype=settings.final_rerank_dtype,
                              max_length=512, batch_size=2, max_candidates=10, top_k=5)
        self.scorer = LocalCrossEncoder(config, cache_dir=settings.final_model_cache_dir)
        self.reranker = Reranker(self.scorer, config=config)
        self.timeout = settings.final_rerank_timeout_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="final-rerank")
        self._guard = Lock()
        self._warm = False
        self._unavailable = False

    def _run(self, query, candidates):
        if not self._warm:
            self.scorer.score("预热", ["本地模型预热。"])
            self._warm = True
        return self.reranker.rerank(query, candidates)

    def _prepare(self):
        if not self._warm:
            self.scorer.score("预热", ["本地模型预热。"])
            self._warm = True

    def prepare(self):
        with trace_span("rag.rerank.prepare", {"openinference.span.kind": "CHAIN", "rerank.device": self.scorer.config.device,
                        "rerank.dtype": self.scorer.config.dtype, "rerank.already_warm": self._warm}):
            self._prepare_wait()

    def _prepare_wait(self):
        with self._guard:
            if self._unavailable:
                raise FinalRetrievalError("重排实例不可用，请检查模型后重启服务")
            future = self._executor.submit(self._prepare)
        try:
            future.result(timeout=self.timeout)
        except FutureTimeout:
            with self._guard:
                self._unavailable = True
            raise FinalRetrievalError("本地重排预热超时；未调用Embedding") from None
        except Exception as exc:
            raise FinalRetrievalError("本地重排加载或预热失败；未调用Embedding") from exc

    def rerank(self, query, candidates):
        with trace_span("rag.rerank", {"openinference.span.kind": "RERANKER", "rerank.candidate_count": len(candidates),
                        "rerank.model": self.scorer.config.model_name, "rerank.revision": self.scorer.config.revision,
                        "rerank.device": self.scorer.config.device}) as span:
            result = self._rerank_wait(query, candidates)
            span.set_attribute("rerank.result_count", len(result))
            return result

    def _rerank_wait(self, query, candidates):
        if not candidates:
            return []
        with self._guard:
            if self._unavailable:
                raise FinalRetrievalError("重排实例不可用，请检查模型后重启服务")
            future = self._executor.submit(self._run, query, candidates)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            with self._guard:
                self._unavailable = True
            raise FinalRetrievalError("本地重排超时；未返回替代排名") from None
        except Exception as exc:
            raise FinalRetrievalError("本地重排失败；未返回替代排名") from exc

    def close(self):
        with self._guard:
            self._unavailable = True
        # A running CUDA call cannot safely be killed by cancelling its Python future.
        self._executor.shutdown(wait=True, cancel_futures=True)
        torch = self.scorer._torch
        self.scorer._model = None
        self.scorer._tokenizer = None
        self.scorer._torch = None
        if torch is not None and self.scorer.config.device == "cuda":
            torch.cuda.empty_cache()


class FinalRetriever:
    def __init__(self, settings: Settings, *, reranker=None):
        self.settings = settings
        self.spec = DenseIndexSpec(collection_name=settings.qdrant_collection_name,
                                   embedding_model=settings.embedding_model,
                                   dimensions=settings.embedding_expected_dimensions,
                                   splitter_version=SplitConfig().index_version)
        if (self.spec.embedding_model, self.spec.dimensions) != ("embedding-3", 2048):
            raise FinalRetrievalError("最终业务检索必须使用embedding-3的2048维索引")
        self.manifest = ManifestStore(settings.manifest_dir, self.spec)
        self.snapshots = SnapshotManager(self.manifest)
        self.reranker = reranker if reranker is not None else SharedReranker(settings)

    def synchronize(self, client):
        return self.snapshots.synchronize(client)

    def search(self, query: str) -> list[RetrievedChunk]:
        with trace_span("rag.retrieve", {"openinference.span.kind": "RETRIEVER", "retrieval.mode": "HYBRID_RERANK",
                        "retrieval.dense_candidates": 10, "retrieval.bm25_candidates": 10,
                        "retrieval.fusion_candidates": 10, "retrieval.final_top_k": 5}) as span:
            result = self._search(query)
            span.set_attribute("retrieval.result_count", len(result))
            return result

    def _search(self, query: str) -> list[RetrievedChunk]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索问题必须是非空文本")
        with index_lock(self.manifest.path):
            if not self.manifest.load().documents:
                return []
            with create_embedding_client(self.settings) as embeddings, create_qdrant_store(self.settings) as store:
                index = self.snapshots.get_locked(store._client)
                self.reranker.prepare()
                dense = DenseRetriever(embeddings, store, self.manifest, top_k=10)
                hybrid = HybridRetriever(dense, index, dense_candidates=10, bm25_candidates=10,
                                         top_k=10, rank_constant=60)
                candidates = hybrid.search(query)
                ranked = self.reranker.rerank(query, candidates)
                return [RetrievedChunk(text=item.hit.text, metadata=item.hit.metadata, score=item.rerank_score)
                        for item in ranked]

    def close(self):
        self.reranker.close()


_instances: dict[int, tuple[Settings, FinalRetriever]] = {}
_instances_lock = Lock()


def get_final_retriever(settings: Settings) -> FinalRetriever:
    with _instances_lock:
        entry = _instances.get(id(settings))
        if entry is None:
            entry = (settings, FinalRetriever(settings))
            _instances[id(settings)] = entry
        return entry[1]


def close_final_retriever(settings: Settings) -> None:
    with _instances_lock:
        entry = _instances.pop(id(settings), None)
    if entry is not None:
        entry[1].close()
