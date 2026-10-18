"""D9固定排名算例：手算分母、raw rank与完整证据区间覆盖。"""

from dataclasses import FrozenInstanceError
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from evals.metrics import EvaluationError, build_chunk_catalog, evaluate_ranking
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig
from project_copilot.schemas.documents import DocumentPage, LoadedDocument
from project_copilot.schemas.retrieval import RetrievedChunk


def document(text: str, *, page: int | None = None) -> LoadedDocument:
    return LoadedDocument(
        document_id=uuid4(), content_hash=sha256(text.encode()).hexdigest(),
        source_name="synthetic.txt", pages=(DocumentPage(text=text, page=page),),
    )


def label_for(doc: LoadedDocument, spans: list[tuple[int, int]], *, page: int | None = None) -> dict:
    text = next(item.text for item in doc.pages if item.page == page)
    return {
        "answerable": True,
        "evidence": [
            {
                "evidence_id": f"unit-{index}", "document_id": str(doc.document_id),
                "document_version": doc.content_hash, "page": page,
                "start_char": start, "end_char": end, "quote": text[start:end],
            }
            for index, (start, end) in enumerate(spans, start=1)
        ],
    }


def hit(located, score: float = 0.8) -> RetrievedChunk:
    return RetrievedChunk(**located.chunk.model_dump(), score=score)


def test_catalog_reuses_d3_and_preserves_exact_unicode_positions(tmp_path: Path) -> None:
    path = tmp_path / "中文.md"
    text = "# 标题\n\n一二三四五六七八九十。\n"
    path.write_text(text, encoding="utf-8")
    doc = load_document(path)
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=8, chunk_overlap=2))

    assert len(catalog) > 1
    for key, located in catalog.items():
        assert key == str(located.chunk.metadata.chunk_id)
        assert text[located.start_char : located.end_char] == located.chunk.text
        assert located.chunk.metadata.source_name == path.name
        assert located.chunk.metadata.page is None
    with pytest.raises(FrozenInstanceError):
        next(iter(catalog.values())).start_char = 99


def test_catalog_rejects_ambiguous_repeated_text_instead_of_guessing() -> None:
    with pytest.raises(EvaluationError, match="位置不唯一"):
        build_chunk_catalog([document("abcabc")], SplitConfig(chunk_size=3, chunk_overlap=0))


def test_catalog_rejects_chunk_missing_from_original_page(monkeypatch) -> None:
    doc = document("abcdef")
    chunk = next(iter(build_chunk_catalog([doc], SplitConfig()).values())).chunk
    changed = chunk.model_copy(update={"text": "not present"})
    monkeypatch.setattr("evals.metrics.split_document", lambda document, config: [changed])
    with pytest.raises(EvaluationError, match="缺失"):
        build_chunk_catalog([doc], SplitConfig())


def test_catalog_rejects_duplicate_documents_or_page_identity() -> None:
    doc = document("abcdef")
    with pytest.raises(EvaluationError, match="重复文档"):
        build_chunk_catalog([doc, doc], SplitConfig())
    doc = doc.model_copy(update={"pages": (DocumentPage(text="abc"), DocumentPage(text="def"))})
    with pytest.raises(EvaluationError, match="重复页"):
        build_chunk_catalog([doc], SplitConfig())


def test_catalog_empty_document_has_no_chunks() -> None:
    assert build_chunk_catalog([document("\n  ")], SplitConfig()) == {}


def test_two_gold_documents_and_duplicate_hits_keep_raw_ranks() -> None:
    first, second, other = document("abcdefgh"), document("ijklmnop"), document("qrstuvwx")
    catalog = build_chunk_catalog([first, second, other], SplitConfig(chunk_size=4, chunk_overlap=0))
    chunks = list(catalog.values())
    label = label_for(first, [(0, 3)])
    second_unit = label_for(second, [(0, 3)])["evidence"][0]
    second_unit["evidence_id"] = "unit-2"
    label["evidence"].append(second_unit)
    result = evaluate_ranking([hit(chunks[4]), hit(chunks[0]), hit(chunks[0]), hit(chunks[2])], label, catalog, (1, 3, 4))

    assert result["gold_document_count"] == result["gold_evidence_count"] == 2
    assert result["at_k"]["1"] == {
        "document_recall": 0.0, "document_mrr": 0.0,
        "evidence_recall": 0.0, "evidence_mrr": 0.0, "all_evidence_covered": False,
    }
    assert result["at_k"]["3"] == {
        "document_recall": 0.5, "document_mrr": 0.5,
        "evidence_recall": 0.5, "evidence_mrr": 0.5, "all_evidence_covered": False,
    }
    assert result["at_k"]["4"] == {
        "document_recall": 1.0, "document_mrr": 0.5,
        "evidence_recall": 1.0, "evidence_mrr": 0.5, "all_evidence_covered": True,
    }
    assert result["evidence_completion_ranks"] == {"unit-1": 2, "unit-2": 4}


def test_same_document_has_one_document_but_two_independent_gold_units() -> None:
    doc = document("abcdefghijkl")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=4, chunk_overlap=0))
    chunks = list(catalog.values())
    result = evaluate_ranking([hit(chunks[0])], label_for(doc, [(0, 2), (8, 10)]), catalog)

    assert result["gold_document_count"] == 1
    assert result["gold_evidence_count"] == 2
    assert result["at_k"]["1"]["document_recall"] == 1
    assert result["at_k"]["1"]["evidence_recall"] == 0.5
    assert not result["at_k"]["1"]["all_evidence_covered"]


def test_cross_chunk_unit_completes_only_when_all_positions_are_covered() -> None:
    doc = document("abcdefghij")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=4, chunk_overlap=1))
    chunks = list(catalog.values())
    assert [item.chunk.text for item in chunks] == ["abcd", "defg", "ghij"]
    result = evaluate_ranking([hit(item) for item in chunks], label_for(doc, [(2, 8)]), catalog, (1, 2, 3))

    assert result["at_k"]["1"]["document_recall"] == 1
    assert result["at_k"]["1"]["evidence_recall"] == 0
    assert result["at_k"]["2"]["evidence_recall"] == 0
    assert result["at_k"]["3"]["evidence_recall"] == 1
    assert result["at_k"]["3"]["evidence_mrr"] == pytest.approx(1 / 3)
    assert result["evidence_completion_ranks"] == {"unit-1": 3}


def test_adjacent_chunks_form_contiguous_evidence_without_overlap() -> None:
    doc = document("abcdefgh")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=4, chunk_overlap=0))
    result = evaluate_ranking([hit(item) for item in catalog.values()], label_for(doc, [(1, 7)]), catalog, (1, 2))
    assert result["evidence_completion_ranks"] == {"unit-1": 2}
    assert result["at_k"]["2"]["all_evidence_covered"]


def test_gap_in_intervals_cannot_be_filled_by_matching_document() -> None:
    doc = document("abcdefghijkl")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=3, chunk_overlap=0))
    chunks = list(catalog.values())
    result = evaluate_ranking([hit(chunks[0]), hit(chunks[2])], label_for(doc, [(0, 9)]), catalog)
    assert result["at_k"]["3"]["document_recall"] == 1
    assert result["at_k"]["3"]["evidence_recall"] == 0
    assert result["evidence_completion_ranks"] == {"unit-1": None}


def test_overlap_and_duplicate_chunks_do_not_inflate_coverage_or_compress_rank() -> None:
    doc = document("abcdefgh")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=4, chunk_overlap=2))
    chunks = list(catalog.values())
    result = evaluate_ranking([hit(chunks[0]), hit(chunks[0]), hit(chunks[1])], label_for(doc, [(2, 5)]), catalog)
    assert result["at_k"]["1"]["evidence_recall"] == 0
    assert result["at_k"]["3"]["evidence_recall"] == 1
    assert result["at_k"]["3"]["evidence_mrr"] == pytest.approx(1 / 3)
    assert result["evidence_completion_ranks"] == {"unit-1": 3}


def test_identical_quote_at_another_position_is_not_the_gold_span() -> None:
    doc = document("abcXdefYabcZ")
    catalog = build_chunk_catalog([doc], SplitConfig(chunk_size=4, chunk_overlap=0))
    result = evaluate_ranking([hit(next(iter(catalog.values())))], label_for(doc, [(8, 11)]), catalog)
    assert result["at_k"]["1"]["document_recall"] == 1
    assert result["at_k"]["1"]["evidence_recall"] == 0


def test_same_quote_in_another_document_is_not_relevant() -> None:
    gold, other = document("abc"), document("abc")
    catalog = build_chunk_catalog([gold, other], SplitConfig())
    result = evaluate_ranking([hit(list(catalog.values())[1])], label_for(gold, [(0, 3)]), catalog)
    assert result["at_k"]["1"]["document_recall"] == 0
    assert result["at_k"]["1"]["evidence_recall"] == 0


def test_wrong_page_does_not_support_evidence_even_with_same_text() -> None:
    doc = document("abc", page=0)
    doc = doc.model_copy(update={"pages": (DocumentPage(text="abc", page=0), DocumentPage(text="abc", page=2))})
    catalog = build_chunk_catalog([doc], SplitConfig())
    result = evaluate_ranking([hit(list(catalog.values())[0])], label_for(doc, [(0, 3)], page=2), catalog)
    assert result["at_k"]["1"]["document_recall"] == 1
    assert result["at_k"]["1"]["evidence_recall"] == 0


def test_wrong_document_version_does_not_count_document_or_evidence() -> None:
    doc = document("abcdef")
    catalog = build_chunk_catalog([doc], SplitConfig())
    label = label_for(doc, [(0, 3)])
    label["evidence"][0]["document_version"] = "f" * 64
    result = evaluate_ranking([hit(next(iter(catalog.values())))], label, catalog)
    assert result["at_k"]["1"]["document_recall"] == 0
    assert result["at_k"]["1"]["evidence_recall"] == 0


def test_incorrect_quote_cannot_be_supported_by_only_matching_offsets() -> None:
    doc = document("abcdef")
    catalog = build_chunk_catalog([doc], SplitConfig())
    label = label_for(doc, [(0, 3)])
    label["evidence"][0]["quote"] = "XYZ"
    result = evaluate_ranking([hit(next(iter(catalog.values())))], label, catalog)
    assert result["at_k"]["1"]["evidence_recall"] == 0


@pytest.mark.parametrize("change", ["text", "source_name", "page", "content_hash", "chunk_id", "index_version"])
def test_unknown_or_tampered_ranked_chunk_is_rejected(change: str) -> None:
    doc = document("abcdef")
    catalog = build_chunk_catalog([doc], SplitConfig())
    original = hit(next(iter(catalog.values())))
    if change == "text":
        changed = original.model_copy(update={"text": "sensitive synthetic input"})
    else:
        values = {"source_name": "renamed", "page": 1, "content_hash": "f" * 64, "chunk_id": uuid4(), "index_version": "other"}
        changed = original.model_copy(update={"metadata": original.metadata.model_copy(update={change: values[change]})})
    with pytest.raises(EvaluationError) as error:
        evaluate_ranking([changed], label_for(doc, [(0, 3)]), catalog)
    assert "sensitive synthetic input" not in str(error.value)


def test_scores_do_not_change_raw_rank_order() -> None:
    doc, distractor = document("abcdef"), document("uvwxyz")
    catalog = build_chunk_catalog([doc, distractor], SplitConfig())
    chunks = list(catalog.values())
    result = evaluate_ranking([hit(chunks[1], 0.01), hit(chunks[0], 0.99)], label_for(doc, [(0, 3)]), catalog)
    assert result["at_k"]["1"]["document_mrr"] == 0
    assert result["at_k"]["3"]["document_mrr"] == 0.5


def test_all_returned_hits_are_identity_checked_even_beyond_requested_k() -> None:
    doc = document("abcdef")
    catalog = build_chunk_catalog([doc], SplitConfig())
    original = hit(next(iter(catalog.values())))
    changed = original.model_copy(update={"text": "unknown chunk"})
    with pytest.raises(EvaluationError):
        evaluate_ranking([original, changed], label_for(doc, [(0, 3)]), catalog, (1,))


def test_no_hits_answerable_question_keeps_zero_in_denominator() -> None:
    doc = document("abcdef")
    result = evaluate_ranking([], label_for(doc, [(0, 3)]), build_chunk_catalog([doc], SplitConfig()))
    assert result["gold_evidence_count"] == result["gold_document_count"] == 1
    assert all(value == 0 for key, value in result["at_k"]["1"].items() if key != "all_evidence_covered")
    assert result["at_k"]["1"]["all_evidence_covered"] is False


def test_unanswerable_question_metrics_are_null_not_success_or_zero() -> None:
    doc = document("abcdef")
    catalog = build_chunk_catalog([doc], SplitConfig())
    result = evaluate_ranking([hit(next(iter(catalog.values())))], {"answerable": False, "evidence": []}, catalog)
    assert result["answerable"] is False
    assert result["gold_document_count"] == result["gold_evidence_count"] == 0
    assert result["evidence_completion_ranks"] == {}
    assert all(value is None for metrics in result["at_k"].values() for value in metrics.values())


@pytest.mark.parametrize("ks", [(), (0,), (-1,), (True,), (1.5,), (1, 1)])
def test_k_values_must_be_distinct_positive_integers(ks) -> None:
    with pytest.raises(EvaluationError, match="正整数"):
        evaluate_ranking([], {"answerable": False, "evidence": []}, {}, ks)


@pytest.mark.parametrize("change", ["answerable", "empty", "duplicate", "offset", "page", "quote_length"])
def test_invalid_metric_label_contract_is_rejected(change: str) -> None:
    doc = document("abcdef")
    label = label_for(doc, [(0, 3)])
    if change == "answerable":
        label["answerable"] = "true"
    elif change == "empty":
        label["evidence"] = []
    elif change == "duplicate":
        label["evidence"] *= 2
    else:
        key, value = {"offset": ("start_char", -1), "page": ("page", True), "quote_length": ("quote", "x")}[change]
        label["evidence"][0][key] = value
    with pytest.raises(EvaluationError):
        evaluate_ranking([], label, {})
