"""Dense检索只返回完成清单内的文档证据，不生成回答。"""

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.infrastructure.qdrant import DenseQdrantStore, QdrantResponseError
from project_copilot.rag.ingestion import Embeddings, ManifestStore, validate_index_binding
from project_copilot.schemas.retrieval import RetrievedChunk


class DenseRetriever:
    def __init__(
        self, embeddings: Embeddings, store: DenseQdrantStore,
        manifest: ManifestStore, *, top_k: int = 5,
    ) -> None:
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k必须是1～100的整数")
        validate_index_binding(embeddings, store, manifest.spec)
        self.embeddings = embeddings
        self.store = store
        self.manifest = manifest
        self.top_k = top_k

    def search(self, query: str) -> list[RetrievedChunk]:
        with trace_span("rag.dense", {"openinference.span.kind": "RETRIEVER", "retrieval.top_k": self.top_k}) as span:
            result = self._search(query)
            span.set_attribute("retrieval.result_count", len(result))
            return result

    def _search(self, query: str) -> list[RetrievedChunk]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索问题必须是非空文本")
        manifest = self.manifest.load()
        if not manifest.documents:
            return []
        self.store.ensure_collection()
        with trace_span("embedding.query", {"openinference.span.kind": "EMBEDDING", "embedding.model": "embedding-3"}):
            vector = self.embeddings.embed_query(query)
        with trace_span("qdrant.search", {"openinference.span.kind": "RETRIEVER", "retrieval.top_k": self.top_k}):
            results = self.store.query(
                vector, document_ids=[d.document_id for d in manifest.documents],
                dense_index_version=manifest.spec.index_version, limit=self.top_k,
            )
        # 完成清单中的精确片段身份再校验，不能把未登记的点当证据。
        allowed = {d.document_id: d for d in manifest.documents}
        for item in results:
            document = allowed.get(item.metadata.document_id)
            if document is None or item.metadata.chunk_id not in document.chunk_ids:
                raise QdrantResponseError("检索结果包含未登记的片段身份")
            if item.metadata.content_hash != document.content_hash:
                raise QdrantResponseError("检索结果的内容指纹与完成清单不一致")
            if item.metadata.index_version != manifest.spec.splitter_version:
                raise QdrantResponseError("检索结果的切分版本与当前索引不一致")
        return results
