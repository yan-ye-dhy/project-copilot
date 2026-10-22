"""D14混合编排：真实BM25、可控Dense替身；不调用模型或外部服务。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from unittest.mock import Mock
from uuid import UUID

import pytest

from project_copilot.rag.bm25 import BM25Index
from project_copilot.rag.hybrid import HybridError, HybridRetriever
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.documents import DocumentChunk
from project_copilot.schemas.retrieval import CompletedDocument, DenseIndexSpec, IndexManifest, RetrievedChunk


@dataclass
class ManifestStub:
    current: IndexManifest

    def load(self) -> IndexManifest:
        return self.current


@dataclass
class DenseStub:
    manifest: ManifestStub
    hits: list[RetrievedChunk]
    top_k: int = 2
    queries: list[str] = field(default_factory=list)
    error: Exception | None = None
    on_search: Callable[[], None] | None = None

    def search(self, query: str) -> list[RetrievedChunk]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if self.on_search is not None:
            self.on_search()
        return self.hits


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[BM25Index, dict[str, DocumentChunk]]:
    directory = tmp_path_factory.mktemp("hybrid-corpus")
    config = SplitConfig()
    chunks: list[DocumentChunk] = []
    completed = []
    for name, text in {
        "upload.txt": "上传文件到 /documents/upload 入口。",
        "health.txt": "健康服务使用 /health 接口。",
        "config.txt": "配置通过 load_settings 加载。",
    }.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        document = load_document(path)
        parts = split_document(document, config)
        chunks.extend(parts)
        completed.append(CompletedDocument(
            document_id=document.document_id, content_hash=document.content_hash,
            source_name=document.source_name,
            chunk_ids=tuple(chunk.metadata.chunk_id for chunk in parts),
        ))
    manifest = IndexManifest(spec=DenseIndexSpec(
        collection_name="pc_test_hybrid", embedding_model="embedding-3",
        dimensions=3, splitter_version=config.index_version,
    ), documents=tuple(completed))
    return BM25Index.build(manifest, chunks), {chunk.metadata.source_name: chunk for chunk in chunks}


@pytest.fixture
def routes(corpus: tuple[BM25Index, dict[str, DocumentChunk]]) -> tuple[DenseStub, BM25Index]:
    bm25, chunks = corpus
    dense = DenseStub(ManifestStub(bm25.snapshot.manifest), [
        RetrievedChunk(**chunks[name].model_dump(), score=score)
        for name, score in [("health.txt", 0.8), ("upload.txt", 0.7)]
    ])
    return dense, bm25


def _hybrid(dense: DenseStub, bm25: BM25Index, **kwargs: object) -> HybridRetriever:
    options = {"dense_candidates": 2, "bm25_candidates": 2, "top_k": 3, **kwargs}
    return HybridRetriever(dense, bm25, **options)


def test_real_bm25_and_dense_receive_same_query_once_and_share_identity(
    routes: tuple[DenseStub, BM25Index], monkeypatch: pytest.MonkeyPatch,
) -> None:
    dense, bm25 = routes
    lexical = Mock(wraps=bm25.search)
    monkeypatch.setattr(bm25, "search", lexical)
    hybrid = _hybrid(dense, bm25)
    query = "/documents/upload"

    hits = hybrid.search(query)

    assert dense.queries == [query]
    lexical.assert_called_once_with(query, top_k=2)
    assert [hit.metadata.source_name for hit in hits] == ["upload.txt", "health.txt"]
    assert hits[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert hits[1].score == pytest.approx(1 / 61)
    assert hits[0].text == dense.hits[1].text
    assert hits[0].metadata == dense.hits[1].metadata
    assert len({hit.metadata.chunk_id for hit in hits}) == len(hits)


@pytest.mark.parametrize("empty_route", ["dense", "bm25", "both"])
def test_a_valid_empty_branch_does_not_discard_other_results(
    routes: tuple[DenseStub, BM25Index], empty_route: str,
) -> None:
    dense, bm25 = routes
    if empty_route in {"dense", "both"}:
        dense.hits = []
    query = "unmatchedxyz987" if empty_route in {"bm25", "both"} else "/documents/upload"

    hits = _hybrid(dense, bm25).search(query)

    assert len(hits) == {"dense": 1, "bm25": 2, "both": 0}[empty_route]
    assert dense.queries == [query]


@pytest.mark.parametrize("query", ["", " \n ", None])
def test_invalid_query_is_rejected_before_either_route(
    routes: tuple[DenseStub, BM25Index], monkeypatch: pytest.MonkeyPatch, query: object,
) -> None:
    dense, bm25 = routes
    lexical = Mock(side_effect=AssertionError("不应调用BM25"))
    monkeypatch.setattr(bm25, "search", lexical)
    with pytest.raises(HybridError, match="非空文本"):
        _hybrid(dense, bm25).search(query)
    assert dense.queries == []
    lexical.assert_not_called()


@pytest.mark.parametrize("options", [
    {"dense_candidates": True}, {"dense_candidates": 0}, {"dense_candidates": 101},
    {"bm25_candidates": "2"}, {"bm25_candidates": 2.0}, {"bm25_candidates": 101},
    {"top_k": False}, {"top_k": 0}, {"top_k": 101}, {"top_k": 5},
    {"rank_constant": True}, {"rank_constant": 0}, {"rank_constant": 1.5},
])
def test_invalid_or_unbounded_budgets_are_rejected(
    routes: tuple[DenseStub, BM25Index], options: dict[str, object],
) -> None:
    dense, bm25 = routes
    with pytest.raises(HybridError):
        _hybrid(dense, bm25, **options)
    assert dense.queries == []


def test_dense_budget_must_agree_at_construction_and_afterwards(
    routes: tuple[DenseStub, BM25Index],
) -> None:
    dense, bm25 = routes
    with pytest.raises(HybridError, match="预算"):
        _hybrid(dense, bm25, dense_candidates=3)
    hybrid = _hybrid(dense, bm25)
    dense.top_k = 1
    with pytest.raises(HybridError, match="预算"):
        hybrid.search("上传文件")
    assert dense.queries == []


def _change_manifest(dense: DenseStub) -> None:
    old = dense.manifest.current
    dense.manifest.current = old.model_copy(update={"documents": old.documents[:-1]})


def test_stale_bm25_manifest_is_rejected_at_construction(
    routes: tuple[DenseStub, BM25Index],
) -> None:
    dense, bm25 = routes
    _change_manifest(dense)
    with pytest.raises(HybridError, match="成功清单"):
        _hybrid(dense, bm25)
    assert dense.queries == []


def test_manifest_change_after_construction_is_rejected_before_dense_request(
    routes: tuple[DenseStub, BM25Index],
) -> None:
    dense, bm25 = routes
    hybrid = _hybrid(dense, bm25)
    _change_manifest(dense)
    with pytest.raises(HybridError, match="成功清单"):
        hybrid.search("上传文件")
    assert dense.queries == []


def test_manifest_change_during_dense_search_is_rejected_before_bm25(
    routes: tuple[DenseStub, BM25Index], monkeypatch: pytest.MonkeyPatch,
) -> None:
    dense, bm25 = routes
    dense.on_search = lambda: _change_manifest(dense)
    lexical = Mock(side_effect=AssertionError("陈旧清单不得查询BM25"))
    monkeypatch.setattr(bm25, "search", lexical)
    with pytest.raises(HybridError, match="成功清单"):
        _hybrid(dense, bm25).search("上传文件")
    assert dense.queries == ["上传文件"]
    lexical.assert_not_called()


@pytest.mark.parametrize("route", ["dense", "bm25"])
def test_branch_errors_propagate_instead_of_becoming_valid_empty_results(
    routes: tuple[DenseStub, BM25Index], monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    dense, bm25 = routes
    failure = RuntimeError("执行失败")
    lexical = Mock(wraps=bm25.search)
    if route == "dense":
        dense.error = failure
    else:
        lexical.side_effect = failure
    monkeypatch.setattr(bm25, "search", lexical)
    with pytest.raises(RuntimeError) as caught:
        _hybrid(dense, bm25).search("上传文件")
    assert caught.value is failure
    assert dense.queries == ["上传文件"]
    assert lexical.call_count == (0 if route == "dense" else 1)


@pytest.mark.parametrize("route", ["dense", "bm25"])
def test_route_cannot_return_more_than_its_candidate_budget(
    routes: tuple[DenseStub, BM25Index], monkeypatch: pytest.MonkeyPatch, route: str,
) -> None:
    dense, bm25 = routes
    too_many = [dense.hits[0]] * 3
    if route == "dense":
        dense.hits = too_many
    else:
        monkeypatch.setattr(bm25, "search", Mock(return_value=too_many))
    with pytest.raises(HybridError, match="超过候选预算"):
        _hybrid(dense, bm25).search("上传文件")


@pytest.mark.parametrize("change", ["text", "source", "unknown_id"])
def test_hits_must_match_canonical_snapshot_text_and_metadata(
    routes: tuple[DenseStub, BM25Index], change: str,
) -> None:
    dense, bm25 = routes
    first = dense.hits[0]
    if change == "text":
        changed = first.model_copy(update={"text": "伪造原文"})
    else:
        metadata = first.metadata.model_copy(update=(
            {"source_name": "wrong.txt"} if change == "source"
            else {"chunk_id": UUID(int=1)}
        ))
        changed = first.model_copy(update={"metadata": metadata})
    dense.hits = [changed]
    with pytest.raises(HybridError, match="原文或元数据"):
        _hybrid(dense, bm25).search("上传文件")


def test_final_top_k_is_applied_after_fusion(routes: tuple[DenseStub, BM25Index]) -> None:
    dense, bm25 = routes
    hits = _hybrid(dense, bm25, top_k=1, rank_constant=10).search("/documents/upload")
    assert len(hits) == 1
    assert hits[0].metadata.source_name == "upload.txt"
    assert hits[0].score == pytest.approx(1 / 12 + 1 / 11)
