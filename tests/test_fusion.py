"""可手算的RRF合同：实际D3身份、重复、缺路、并列和索引冲突。"""

import pytest
from pydantic import ValidationError

from project_copilot.rag.fusion import reciprocal_rank_fusion
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.retrieval import RetrievedChunk


@pytest.fixture(scope="module")
def hits(tmp_path_factory: pytest.TempPathFactory) -> tuple[RetrievedChunk, ...]:
    """测试有效身份来自真实加载/切分，按UUID排序方便手算并列。"""
    directory = tmp_path_factory.mktemp("rrf-corpus")
    chunks = []
    for index, text in enumerate(("配置启动阶段。", "上传空文件。", "引用来源。", "向量维度。")):
        path = directory / f"source-{index}.txt"
        path.write_text(text, encoding="utf-8")
        chunk = split_document(load_document(path), SplitConfig())[0]
        chunks.append(RetrievedChunk(**chunk.model_dump(), score=0.5))
    return tuple(sorted(chunks, key=lambda hit: str(hit.metadata.chunk_id)))


def _ids(results: list[RetrievedChunk]) -> list[str]:
    return [str(hit.metadata.chunk_id) for hit in results]


def test_two_routes_have_hand_calculated_fused_scores(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, _ = hits
    result = reciprocal_rank_fusion([[a, b], [b, c]], rank_constant=60)
    assert _ids(result) == _ids([b, a, c])
    assert [hit.score for hit in result] == pytest.approx([1 / 62 + 1 / 61, 1 / 61, 1 / 62])


def test_rank_constant_changes_exact_formula(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, _ = hits
    result = reciprocal_rank_fusion([[a, b], [b, c]], rank_constant=1)
    assert [hit.score for hit in result] == pytest.approx([5 / 6, 1 / 2, 1 / 3])


def test_original_dense_and_bm25_scores_are_not_added(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, _, _ = hits
    dense = [a.model_copy(update={"score": -0.9}), b.model_copy(update={"score": 0.99})]
    lexical = [b.model_copy(update={"score": 900000.0}), a.model_copy(update={"score": 0.001})]
    result = reciprocal_rank_fusion([dense, lexical])
    assert _ids(result) == _ids([a, b])
    assert result[0].score == result[1].score == pytest.approx(1 / 61 + 1 / 62)


def test_same_route_duplicate_contributes_only_first_rank(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, _, _ = hits
    result = reciprocal_rank_fusion([[a, a, b]])
    assert _ids(result) == _ids([a, b])
    assert [hit.score for hit in result] == pytest.approx([1 / 61, 1 / 63])


def test_duplicates_do_not_compress_later_ranks_across_routes(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, _ = hits
    result = reciprocal_rank_fusion([[a, a, b], [b, b, c]])
    assert _ids(result) == _ids([b, a, c])
    assert [hit.score for hit in result] == pytest.approx([1 / 63 + 1 / 61, 1 / 61, 1 / 63])


def test_deduplication_is_by_id_not_text_or_source(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, _, _ = hits
    # 不同ID即使原文恰好相同也不是同一个来源片段，不应合并。
    same_text = RetrievedChunk(text=a.text, metadata=b.metadata, score=1.0)
    result = reciprocal_rank_fusion([[a], [same_text]])
    assert len(result) == 2
    assert _ids(result) == _ids([a, b])


@pytest.mark.parametrize("rankings", [[], [[]], [[], []]])
def test_all_missing_routes_return_empty(rankings: list[list[RetrievedChunk]]) -> None:
    assert reciprocal_rank_fusion(rankings) == []


def test_empty_route_does_not_change_available_route(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, _, _ = hits
    expected = reciprocal_rank_fusion([[b, a]])
    assert reciprocal_rank_fusion([[], [b, a], []]) == expected
    assert _ids(expected) == _ids([b, a])


def test_tuple_inputs_are_supported(hits: tuple[RetrievedChunk, ...]) -> None:
    assert reciprocal_rank_fusion((hits[:2], hits[1:3])) == reciprocal_rank_fusion([list(hits[:2]), list(hits[1:3])])


def test_equal_scores_use_uuid_order_not_route_insertion(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, _, _ = hits
    assert _ids(reciprocal_rank_fusion([[b], [a]])) == _ids([a, b])
    assert _ids(reciprocal_rank_fusion([[b, a], [a, b]])) == _ids([a, b])


def test_route_permutation_does_not_change_result(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, d = hits
    routes = [[a, b, c], [b, a, d], [c, d, a], [d, c, b]]
    assert reciprocal_rank_fusion(routes) == reciprocal_rank_fusion(list(reversed(routes)))


def test_top_k_applies_after_fusion(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, d = hits
    result = reciprocal_rank_fusion([[a, b, c, d], [d, c, b, a]], top_k=1)
    assert _ids(result) == _ids([a])
    assert result[0].score == pytest.approx(1 / 61 + 1 / 64)


def test_returned_objects_preserve_source_but_do_not_mutate_inputs(hits: tuple[RetrievedChunk, ...]) -> None:
    source = hits[0]
    before = source.model_dump()
    result = reciprocal_rank_fusion([[source]])[0]
    assert result.text == source.text
    assert result.metadata == source.metadata
    assert result is not source
    assert source.model_dump() == before
    assert source.score == 0.5


@pytest.mark.parametrize("field,value", [
    ("source_name", "renamed.txt"), ("page", 2), ("content_hash", "b" * 64),
    ("chunk_index", 10), ("index_version", "different-splitter"),
])
def test_same_id_with_conflicting_metadata_is_rejected(
    hits: tuple[RetrievedChunk, ...], field: str, value: object,
) -> None:
    source = hits[0]
    conflicting = RetrievedChunk(
        text=source.text, metadata=source.metadata.model_copy(update={field: value}), score=5.0,
    )
    with pytest.raises(ValueError, match="原文或metadata不一致"):
        reciprocal_rank_fusion([[source], [conflicting]])


def test_same_id_with_conflicting_text_is_rejected(hits: tuple[RetrievedChunk, ...]) -> None:
    source = hits[0]
    conflicting = RetrievedChunk(text=source.text + "修改", metadata=source.metadata, score=1.0)
    with pytest.raises(ValueError, match="原文或metadata不一致"):
        reciprocal_rank_fusion([[source], [conflicting]])


def test_conflicting_duplicate_is_rejected_even_in_same_route(hits: tuple[RetrievedChunk, ...]) -> None:
    source = hits[0]
    conflicting = RetrievedChunk(text="其他文本", metadata=source.metadata, score=1.0)
    with pytest.raises(ValueError, match="原文或metadata不一致"):
        reciprocal_rank_fusion([[source, conflicting]])


def test_conflict_is_not_hidden_by_final_top_k(hits: tuple[RetrievedChunk, ...]) -> None:
    a, b, c, _ = hits
    conflicting = RetrievedChunk(text="冲突", metadata=c.metadata, score=1.0)
    with pytest.raises(ValueError, match="原文或metadata不一致"):
        reciprocal_rank_fusion([[a, b, c], [conflicting]], top_k=1)


@pytest.mark.parametrize("rank_constant", [0, -1, True, False, 60.0, "60", None])
def test_rank_constant_requires_strict_positive_integer(rank_constant: object) -> None:
    with pytest.raises(ValueError, match="rank_constant必须是正整数"):
        reciprocal_rank_fusion([], rank_constant=rank_constant)


@pytest.mark.parametrize("top_k", [0, -1, 101, True, False, 3.0, "3", None])
def test_top_k_requires_strict_bounded_integer(top_k: object) -> None:
    with pytest.raises(ValueError, match="top_k必须是1至100"):
        reciprocal_rank_fusion([], top_k=top_k)


def test_maximum_top_k_accepts_short_list(hits: tuple[RetrievedChunk, ...]) -> None:
    assert len(reciprocal_rank_fusion([hits], top_k=100)) == 4


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), True])
def test_input_model_rejects_invalid_original_score(hits: tuple[RetrievedChunk, ...], score: object) -> None:
    source = hits[0]
    with pytest.raises(ValidationError):
        RetrievedChunk(text=source.text, metadata=source.metadata, score=score)


def test_ranking_entries_require_retrieved_chunk() -> None:
    with pytest.raises(TypeError, match="必须是RetrievedChunk"):
        reciprocal_rank_fusion([[{"score": 1.0}]])
