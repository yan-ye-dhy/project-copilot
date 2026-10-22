"""按排名融合检索结果；Dense与BM25的原始分数不具备相同尺度。"""

from collections.abc import Sequence
from math import fsum
from uuid import UUID

from project_copilot.schemas.retrieval import RetrievedChunk


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievedChunk]],
    *,
    rank_constant: int = 60,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """将每路的1-based排名转为1/(rank_constant + rank)，同ID累加。

    同路重复只计首次，但不压缩后续排名；缺一路直接使用已有排名。
    同ID必须对应完全一致的原文和来源，防止混用不同索引快照。
    返回的新对象以RRF分数排序，分数并列按chunk UUID字符串排序。
    """
    if type(rank_constant) is not int or rank_constant < 1:
        raise ValueError("RRF rank_constant必须是正整数，不能使用布尔值")
    if type(top_k) is not int or not 1 <= top_k <= 100:
        raise ValueError("RRF top_k必须是1至100之间的整数，不能使用布尔值")

    chunks: dict[UUID, RetrievedChunk] = {}
    contributions: dict[UUID, list[float]] = {}
    for ranking in rankings:
        seen: set[UUID] = set()
        for rank, chunk in enumerate(ranking, start=1):
            if not isinstance(chunk, RetrievedChunk):
                raise TypeError("RRF排名条目必须是RetrievedChunk")
            chunk_id = chunk.metadata.chunk_id
            existing = chunks.get(chunk_id)
            if existing is not None and (
                existing.text != chunk.text or existing.metadata != chunk.metadata
            ):
                raise ValueError("RRF同一chunk_id的原文或metadata不一致")
            chunks.setdefault(chunk_id, chunk)
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            # 不读取chunk.score：各检索路线的原始分数尺度可能完全不同。
            contributions.setdefault(chunk_id, []).append(1 / (rank_constant + rank))

    scores = {chunk_id: fsum(values) for chunk_id, values in contributions.items()}
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], str(chunk_id)))
    return [
        RetrievedChunk(
            text=chunks[chunk_id].text,
            metadata=chunks[chunk_id].metadata,
            score=scores[chunk_id],
        )
        for chunk_id in ordered[:top_k]
    ]
