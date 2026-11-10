"""统一业务检索：完成清单绑定的Dense/BM25→RRF→固定版本本地BGE重排。"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import os
from pathlib import Path
from threading import Lock, RLock
from uuid import uuid4

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.infrastructure.job_progress import progress
from project_copilot.infrastructure.execution_tracking import tracked_submit
from project_copilot.infrastructure.ranking_cache import RankingRecord, create_ranking_cache
from project_copilot.infrastructure.resource_limits import GpuModelOwner
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

    def get_locked(self, client, *, _pending_delete_ok: bool = False) -> BM25Index:
        with trace_span("rag.snapshot", {"openinference.span.kind": "CHAIN"}) as span:
            previous = self._stat
            result = self._get_locked(client, _pending_delete_ok=_pending_delete_ok)
            span.set_attribute("snapshot.reused", previous is not None and previous == self._stat)
            span.set_attribute("snapshot.chunk_count", len(result.snapshot.chunks))
            return result

    def _get_locked(self, client, *, _pending_delete_ok: bool = False) -> BM25Index:
        current = self.manifest.load(_pending_delete_ok=_pending_delete_ok)
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
        self._guard = RLock()
        self._close_guard = Lock()
        self._pending_future = None
        self._closed = False
        self._manifest_dir = settings.manifest_dir
        self._gpu_owner = None
        self._warm = False
        self._unavailable = False

    def _run(self, query, candidates):
        self._own_gpu()
        if not self._warm:
            self.scorer.score("预热", ["本地模型预热。"])
            self._warm = True
        return self.reranker.rerank(query, candidates)

    def _prepare(self):
        self._own_gpu()
        if not self._warm:
            self.scorer.score("预热", ["本地模型预热。"])
            self._warm = True

    def _own_gpu(self):
        # Executed by the one physical executor thread before any model load.
        if self.scorer.config.device == "cuda" and self._gpu_owner is None:
            self._gpu_owner = GpuModelOwner.acquire(self._manifest_dir)

    def _submit(self, function, *args):
        with self._guard:
            if self._unavailable:
                raise FinalRetrievalError("重排实例不可用，请检查模型后重启服务")
            # One running submission and zero queued submissions. A timed-out
            # caller cannot leave an unbounded executor backlog behind it.
            if self._pending_future is not None and not self._pending_future.done():
                raise FinalRetrievalError("重排资源繁忙，请稍后提交新任务")
            future = tracked_submit(self._executor, function, *args)
            self._pending_future = future
            future.add_done_callback(self._submission_finished)
            return future

    @property
    def _pending(self):
        with self._guard:
            return int(self._pending_future is not None and not self._pending_future.done())

    def _submission_finished(self, future):
        with self._guard:
            # Future.result may wake before callbacks run. A late callback must
            # not clear a newer submission or falsely reject a serial caller.
            if self._pending_future is future:
                self._pending_future = None

    def prepare(self):
        with trace_span("rag.rerank.prepare", {"openinference.span.kind": "CHAIN", "rerank.device": self.scorer.config.device,
                        "rerank.dtype": self.scorer.config.dtype, "rerank.already_warm": self._warm}):
            self._prepare_wait()

    def _prepare_wait(self):
        future = self._submit(self._prepare)
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
        future = self._submit(self._run, query, candidates)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            with self._guard:
                self._unavailable = True
            raise FinalRetrievalError("本地重排超时；未返回替代排名") from None
        except Exception as exc:
            raise FinalRetrievalError("本地重排失败；未返回替代排名") from exc

    def close(self):
        with self._close_guard:
            if self._closed:
                return
            with self._guard:
                self._unavailable = True
            # Timeout/cancel is not completion: retain ownership through real
            # executor drain, resident model disposal and allocator cleanup.
            self._executor.shutdown(wait=True, cancel_futures=True)
            torch = self.scorer._torch
            self.scorer._model = None
            self.scorer._tokenizer = None
            if torch is not None and self.scorer.config.device == "cuda":
                torch.cuda.empty_cache()
            self.scorer._torch = None
            if self._gpu_owner is not None:
                self._gpu_owner.close()
                self._gpu_owner = None
            self._closed = True


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
            current = self.manifest.load()
            if not current.documents:
                cache, identity = self._cache_context(query, current)
                if cache is not None and self._cached_ranking(cache, identity, set(), negative=True) is None:
                    lease = cache.acquire(identity) if cache.last_status != "unavailable" else None
                    if lease is not None:
                        try:
                            self._verify_cache_generation(current)
                            cache.publish(identity, lease, [], negative=True)
                        finally:
                            cache.release(identity, lease)
                return []
            with create_embedding_client(self.settings) as embeddings, create_qdrant_store(self.settings) as store:
                index = self.snapshots.get_locked(store._client)
                # A matching manifest/snapshot alone cannot detect a Qdrant
                # volume restored from an older backup or externally damaged.
                # Validate all completed points while holding the same index
                # lock, before spending resources on embedding or reranking.
                current_chunks = read_completed_chunks(store._client, index.snapshot.manifest)
                # Chunk identity, not the order of a backend response, defines
                # equality. Keep duplicates visible by sorting rather than
                # collapsing records into a dictionary.
                by_chunk_id = lambda chunk: str(chunk.metadata.chunk_id)
                if sorted(current_chunks, key=by_chunk_id) != sorted(index.snapshot.chunks, key=by_chunk_id):
                    raise BM25Error("Qdrant完成片段与BM25快照不一致；拒绝检索")
                self.reranker.prepare()
                cache, identity = self._cache_context(query, current, index=index)
                by_id = {chunk.metadata.chunk_id: chunk for chunk in current_chunks}
                cached = self._cached_ranking(cache, identity, set(by_id)) if cache is not None else None
                if cached is not None:
                    return [RetrievedChunk(**by_id[item.chunk_id].model_dump(), score=item.score)
                            for item in cached]
                # The existing file lock still protects the complete business
                # operation. Never wait here for a Redis owner: contention or a
                # failed Redis operation takes the original path without filling.
                lease = cache.acquire(identity) if cache is not None and cache.last_status != "unavailable" else None
                try:
                    dense = DenseRetriever(embeddings, store, self.manifest, top_k=10)
                    hybrid = HybridRetriever(dense, index, dense_candidates=10, bm25_candidates=10,
                                             top_k=10, rank_constant=60)
                    candidates = hybrid.search(query)
                    progress("rerank")
                    ranked = self.reranker.rerank(query, candidates)
                    result = [RetrievedChunk(text=item.hit.text, metadata=item.hit.metadata, score=item.rerank_score)
                              for item in ranked]
                    if lease is not None:
                        self._verify_cache_generation(current)
                        cache.publish(identity, lease,
                            [RankingRecord(item.metadata.chunk_id, item.score) for item in result])
                    return result
                finally:
                    if lease is not None:
                        cache.release(identity, lease)

    def _cache_context(self, query, manifest, *, index=None):
        cache = create_ranking_cache(self.settings)
        if cache is None:
            return None, None
        config = RerankConfig(device=self.settings.final_rerank_device, dtype=self.settings.final_rerank_dtype,
                              max_length=512, batch_size=2, max_candidates=10, top_k=5)
        pipeline = {"contract": "final-hybrid-rerank-v1", "dense_candidates": 10,
                    "bm25_candidates": 10, "fusion_candidates": 10, "rank_constant": 60,
                    "rerank": asdict(config),
                    "bm25": {"tokenizer": index.snapshot.tokenizer,
                             "config": index.snapshot.config.model_dump(mode="json")} if index else "empty"}
        return cache, cache.identity(query, manifest, pipeline=pipeline)

    @staticmethod
    def _cached_ranking(cache, identity, allowed_ids, *, negative=False):
        with trace_span("rag.cache", {"openinference.span.kind": "CHAIN"}) as span:
            result = cache.get(identity, allowed_ids, allow_negative=negative)
            span.set_attribute("cache.status", cache.last_status)
            span.set_attribute("cache.hit", result is not None)
            return result

    def _verify_cache_generation(self, current):
        # All supported writers take this same flock. Recheck at publication so
        # an unexpected external manifest replacement cannot seed a stale key.
        if self.manifest.load() != current:
            raise BM25Error("排名重建期间完成清单发生变化；拒绝发布缓存")

    def close(self):
        self.reranker.close()


_instances: dict[int, tuple[Settings, dict, FinalRetriever]] = {}
_instances_lock = Lock()


def get_final_retriever(settings: Settings) -> FinalRetriever:
    with _instances_lock:
        entry = _instances.get(id(settings))
        if entry is None:
            entry = (settings, deepcopy(settings.model_dump(mode="python")), FinalRetriever(settings))
            _instances[id(settings)] = entry
        elif entry[0] is not settings or entry[1] != settings.model_dump(mode="python"):
            raise FinalRetrievalError("检索配置已变更，请释放资源并重启服务")
        return entry[2]


def close_final_retriever(settings: Settings) -> None:
    with _instances_lock:
        entry = _instances.pop(id(settings), None)
    if entry is not None:
        entry[2].close()
