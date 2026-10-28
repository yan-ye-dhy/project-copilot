"""同一成功清单内编排Dense与BM25，以排名融合；不改变现有问答默认路由。"""

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.rag.bm25 import BM25Index
from project_copilot.rag.fusion import reciprocal_rank_fusion
from project_copilot.rag.retrieval import DenseRetriever
from project_copilot.schemas.retrieval import RetrievedChunk


class HybridError(ValueError):
    """候选预算、清单或片段内容不一致，拒绝继续融合。"""


class HybridRetriever:
    def __init__(
        self, dense: DenseRetriever, bm25: BM25Index, *, dense_candidates: int = 10,
        bm25_candidates: int = 10, top_k: int = 5, rank_constant: int = 60,
    ) -> None:
        for name, value in (
            ("dense_candidates", dense_candidates), ("bm25_candidates", bm25_candidates),
            ("top_k", top_k),
        ):
            if type(value) is not int or not 1 <= value <= 100:
                raise HybridError(f"{name}必须是1～100的整数")
        if type(rank_constant) is not int or rank_constant <= 0:
            raise HybridError("rank_constant必须是正整数")
        if top_k > dense_candidates + bm25_candidates:
            raise HybridError("top_k不能超过两路候选预算之和")
        self.dense, self.bm25 = dense, bm25
        self.dense_candidates, self.bm25_candidates = dense_candidates, bm25_candidates
        self.top_k, self.rank_constant = top_k, rank_constant
        self._validate_binding()

    def _validate_binding(self) -> None:
        if type(self.dense.top_k) is not int or self.dense.top_k != self.dense_candidates:
            raise HybridError("Dense检索预算与dense_candidates不一致")
        if self.dense.manifest.load() != self.bm25.snapshot.manifest:
            raise HybridError("Dense成功清单与BM25快照不一致，请显式重建词项索引")

    def search(self, query: str) -> list[RetrievedChunk]:
        if not isinstance(query, str) or not query.strip():
            raise HybridError("Hybrid查询必须是非空文本")
        # 请求前检查，防止快照构建后新增文档仍静默混用两套语料。
        self._validate_binding()
        dense_hits = self.dense.search(query)
        self._validate_binding()
        with trace_span("rag.bm25", {"openinference.span.kind": "RETRIEVER", "retrieval.top_k": self.bm25_candidates}) as span:
            bm25_hits = self.bm25.search(query, top_k=self.bm25_candidates)
            span.set_attribute("retrieval.result_count", len(bm25_hits))
        self._validate_binding()
        canonical = {
            chunk.metadata.chunk_id: chunk for chunk in self.bm25.snapshot.chunks
        }
        for route, hits, limit in (
            ("Dense", dense_hits, self.dense_candidates),
            ("BM25", bm25_hits, self.bm25_candidates),
        ):
            if len(hits) > limit:
                raise HybridError(f"{route}返回片段数超过候选预算")
            for hit in hits:
                chunk = canonical.get(hit.metadata.chunk_id)
                if chunk is None or (hit.text, hit.metadata) != (chunk.text, chunk.metadata):
                    raise HybridError(f"{route}片段原文或元数据与BM25快照不一致")
        # 真正无命中可为空；任一路执行异常直接向上抛出，不伪装为空列表。
        with trace_span("rag.rrf", {"openinference.span.kind": "CHAIN", "fusion.rank_constant": self.rank_constant,
                        "fusion.dense_count": len(dense_hits), "fusion.bm25_count": len(bm25_hits)}) as span:
            result = reciprocal_rank_fusion(
                [dense_hits, bm25_hits], rank_constant=self.rank_constant, top_k=self.top_k,
            )
            span.set_attribute("fusion.result_count", len(result))
            return result
