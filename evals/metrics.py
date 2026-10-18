"""离线排名指标：按原文位置精确覆盖；不替代人工判断语义支持。"""

from dataclasses import dataclass

from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.documents import DocumentChunk, LoadedDocument
from project_copilot.schemas.retrieval import RetrievedChunk


class EvaluationError(ValueError):
    """排名身份或证据映射不满足评测契约。"""


@dataclass(frozen=True)
class LocatedChunk:
    """D3片段及其在同页原文中的Unicode字符区间[start_char, end_char)。"""

    chunk: DocumentChunk
    start_char: int
    end_char: int


def build_chunk_catalog(
    documents: list[LoadedDocument], split_config: SplitConfig
) -> dict[str, LocatedChunk]:
    """复用D3切分，并在同页原文中唯一定位；歧义时拒绝猜测首次位置。"""
    catalog: dict[str, LocatedChunk] = {}
    document_ids = set()
    for document in documents:
        if document.document_id in document_ids:
            raise EvaluationError("评测语料包含重复文档身份")
        document_ids.add(document.document_id)
        pages = {page.page: page.text for page in document.pages}
        if len(pages) != len(document.pages):
            raise EvaluationError("同一文档包含重复页身份，不能唯一映射")
        for chunk in split_document(document, split_config):
            text = pages[chunk.metadata.page]
            start = text.find(chunk.text)
            if start < 0 or text.find(chunk.text, start + 1) >= 0:
                raise EvaluationError("片段在同页原文中缺失或位置不唯一，不能精确映射")
            key = str(chunk.metadata.chunk_id)
            if key in catalog:
                raise EvaluationError("评测语料包含重复片段身份")
            catalog[key] = LocatedChunk(chunk, start, start + len(chunk.text))
    return catalog


def _gold_evidence(label: dict) -> list[dict]:
    """这里只检查指标所需字段；完整数据集合法性由冻结校验负责。"""
    answerable, evidence = label.get("answerable"), label.get("evidence")
    if type(answerable) is not bool or not isinstance(evidence, list):
        raise EvaluationError("标签缺少有效的可回答状态或证据列表")
    if answerable != bool(evidence):
        raise EvaluationError("可回答状态与gold证据数量不一致")
    ids = set()
    for unit in evidence:
        if not isinstance(unit, dict) or any(
            not isinstance(unit.get(key), str) or not unit[key]
            for key in ("evidence_id", "document_id", "document_version", "quote")
        ):
            raise EvaluationError("gold证据身份或原文无效")
        start, end, page = unit.get("start_char"), unit.get("end_char"), unit.get("page")
        if (
            type(start) is not int or type(end) is not int
            or start < 0 or end <= start or end - start != len(unit["quote"])
            or (page is not None and (type(page) is not int or page < 0))
        ):
            raise EvaluationError("gold证据的页码或原文字符位置无效")
        if unit["evidence_id"] in ids:
            raise EvaluationError("gold证据ID不能重复")
        ids.add(unit["evidence_id"])
    return evidence


def _fully_covered(unit: dict, chunks: list[LocatedChunk]) -> bool:
    """同文档版本/页的区间无缝并集须覆盖完整quote，且原文逐字相符。"""
    start, end = unit["start_char"], unit["end_char"]
    intervals = []
    for located in chunks:
        metadata = located.chunk.metadata
        if (
            str(metadata.document_id) != unit["document_id"]
            or metadata.content_hash != unit["document_version"]
            or metadata.page != unit["page"]
        ):
            continue
        left, right = max(start, located.start_char), min(end, located.end_char)
        if left < right and (
            located.chunk.text[left - located.start_char : right - located.start_char]
            == unit["quote"][left - start : right - start]
        ):
            intervals.append((left, right))
    cursor = start
    for left, right in sorted(intervals):
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def evaluate_ranking(
    hits: list[RetrievedChunk],
    label: dict,
    catalog: dict[str, LocatedChunk],
    ks: tuple[int, ...] = (1, 3, 5, 10),
) -> dict:
    """k与完成rank均使用原始chunk排名；无答案的指标为None，由汇总排除。"""
    if not ks or any(type(k) is not int or k <= 0 for k in ks) or len(set(ks)) != len(ks):
        raise EvaluationError("k必须是非空、互不重复的正整数")
    evidence = _gold_evidence(label)
    located_hits = []
    for hit in hits:
        located = catalog.get(str(hit.metadata.chunk_id))
        if located is None or hit.text != located.chunk.text or hit.metadata != located.chunk.metadata:
            raise EvaluationError("排名包含未知片段或与当前语料不一致的内容/元数据")
        located_hits.append(located)

    gold_documents = {(unit["document_id"], unit["document_version"]) for unit in evidence}
    document_ranks: dict[tuple[str, str], int] = {}
    completion_ranks: dict[str, int | None] = {unit["evidence_id"]: None for unit in evidence}
    prefix = []
    for rank, located in enumerate(located_hits, start=1):
        metadata = located.chunk.metadata
        identity = (str(metadata.document_id), metadata.content_hash)
        if identity in gold_documents:
            document_ranks.setdefault(identity, rank)
        prefix.append(located)
        for unit in evidence:
            if completion_ranks[unit["evidence_id"]] is None and _fully_covered(unit, prefix):
                completion_ranks[unit["evidence_id"]] = rank

    at_k = {}
    for k in sorted(ks):
        doc_ranks = [rank for rank in document_ranks.values() if rank <= k]
        unit_ranks = [rank for rank in completion_ranks.values() if rank is not None and rank <= k]
        at_k[str(k)] = {
            "document_recall": len(doc_ranks) / len(gold_documents) if evidence else None,
            "document_mrr": 1 / min(doc_ranks) if doc_ranks else (0.0 if evidence else None),
            "evidence_recall": len(unit_ranks) / len(evidence) if evidence else None,
            "evidence_mrr": 1 / min(unit_ranks) if unit_ranks else (0.0 if evidence else None),
            "all_evidence_covered": len(unit_ranks) == len(evidence) if evidence else None,
        }
    return {
        "answerable": label["answerable"],
        "gold_document_count": len(gold_documents),
        "gold_evidence_count": len(evidence),
        "at_k": at_k,
        "evidence_completion_ranks": completion_ranks,
    }
