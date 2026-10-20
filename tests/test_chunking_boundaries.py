"""D11只检查候选切分的原文边界，不把局部覆盖当成检索效果。"""

from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from evals.metrics import build_chunk_catalog, evaluate_ranking
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import SplitConfig, split_document
from project_copilot.schemas.documents import DocumentPage, LoadedDocument
from project_copilot.schemas.retrieval import RetrievedChunk


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/copilot_v0"
BASELINE = ROOT / "evals/results/dense-v0-20261019-01"
CANDIDATE = SplitConfig(chunk_size=500)


def _document(text: str) -> LoadedDocument:
    content_hash = sha256(text.encode("utf-8")).hexdigest()
    return LoadedDocument(
        document_id=uuid5(NAMESPACE_URL, content_hash),
        content_hash=content_hash,
        source_name="boundary.md",
        pages=(DocumentPage(text=text),),
    )


def test_long_chinese_uses_character_fallback_without_sentence_guarantee() -> None:
    text = "甲" * 167 + "。" + "乙" * 173 + "！" + "丙" * 181 + "？" + "丁" * 197 + "；" + "戊" * 223
    chunks = split_document(_document(text), CANDIDATE)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 500 for chunk in chunks)
    # 默认分隔符没有中文标点；无换行/空格时按字符回退，可能截断句子。
    assert chunks[0].text[-1] not in "。！？；，"
    for previous, current in zip(chunks, chunks[1:]):
        assert current.text.startswith(previous.text[-150:])
    assert chunks[0].text + "".join(chunk.text[150:] for chunk in chunks[1:]) == text


def test_markdown_heading_is_preserved_but_not_a_semantic_section_boundary() -> None:
    text = "# 手册\n\n" + "序" * 470 + "\n\n## 配置加载\n\n" + "配" * 320 + "\n\n## 模型密钥\n\n" + "钥" * 320 + "\n"
    catalog = build_chunk_catalog([_document(text)], CANDIDATE)
    chunks = [located.chunk for located in catalog.values()]

    assert all(text[located.start_char : located.end_char] == located.chunk.text for located in catalog.values())
    assert any(chunk.text.endswith("## 配置加载") for chunk in chunks)
    assert any("## 配置加载\n\n配" in chunk.text for chunk in chunks)
    assert all(heading in "".join(chunk.text for chunk in chunks) for heading in ("# 手册", "## 配置加载", "## 模型密钥"))
    # 标题可能在上一片段结尾，又随重叠进入下一片段；没有Markdown解析器。
    assert sum("## 配置加载" in chunk.text for chunk in chunks) > 1


def test_paragraph_boundaries_can_leave_less_than_requested_overlap() -> None:
    text = "甲" * 300 + "\n\n" + "乙" * 300 + "\n\n" + "丙" * 300
    catalog = list(build_chunk_catalog([_document(text)], CANDIDATE).values())

    assert len(catalog) == 3
    assert catalog[0].start_char == 0
    assert catalog[-1].end_char == len(text)
    for previous, current in zip(catalog, catalog[1:]):
        actual_overlap = previous.end_char - current.start_char
        assert 0 <= actual_overlap < CANDIDATE.chunk_overlap
        assert current.chunk.text.startswith("\n\n")


@pytest.mark.parametrize("question_id", ["pc-d7-003", "pc-d7-012", "pc-d8-034", "pc-d8-039"])
def test_real_dev_bad_cases_keep_original_gold_under_smaller_chunks(question_id: str) -> None:
    labels = [json.loads(line) for line in (DATASET / "labels.draft.jsonl").read_text(encoding="utf-8").splitlines()]
    label = next(row for row in labels if row["question_id"] == question_id)
    sources = [json.loads(line) for line in (DATASET / "sources.jsonl").read_text(encoding="utf-8").splitlines()]
    documents = [load_document(DATASET / source["path"]) for source in sources]
    baseline = build_chunk_catalog(documents, SplitConfig())
    candidate = build_chunk_catalog(documents, CANDIDATE)
    rankings = [json.loads(line) for line in (BASELINE / "rankings.jsonl").read_text(encoding="utf-8").splitlines()]
    row = next(row for row in rankings if row["question_id"] == question_id)

    # 先证明问题确实来自V0的实际排名；不把这些旧排名套用到候选chunk上。
    observed = evaluate_ranking([RetrievedChunk.model_validate(hit) for hit in row["hits"]], label, baseline, ks=(3, 5))
    assert observed["at_k"]["3"]["all_evidence_covered"] is False
    assert observed["at_k"]["5"]["all_evidence_covered"] is True
    for evidence in label["evidence"]:
        lengths = []
        for catalog in (baseline, candidate):
            containing = [
                located for located in catalog.values()
                if str(located.chunk.metadata.document_id) == evidence["document_id"]
                and located.chunk.metadata.content_hash == evidence["document_version"]
                and located.chunk.metadata.page == evidence["page"]
                and located.start_char <= evidence["start_char"]
                and located.end_char >= evidence["end_char"]
            ]
            assert containing
            assert all(
                located.chunk.text[evidence["start_char"] - located.start_char : evidence["end_char"] - located.start_char] == evidence["quote"]
                for located in containing
            )
            lengths.append(min(len(located.chunk.text) for located in containing))
        assert lengths[1] < lengths[0]


def test_gold_longer_than_candidate_requires_multiple_contiguous_chunks() -> None:
    text = "".join(chr(0x4E00 + offset) for offset in range(790))
    document = _document(text)
    baseline = list(build_chunk_catalog([document], SplitConfig()).values())
    candidate = list(build_chunk_catalog([document], CANDIDATE).values())
    gold_start, gold_end = 70, 590  # 520字证据，比500字候选上限更长。

    assert any(located.start_char <= gold_start and located.end_char >= gold_end for located in baseline)
    # 保留完整原文并不保证单个检索命中已含充分证据。
    assert not any(located.start_char <= gold_start and located.end_char >= gold_end for located in candidate)
    cursor = gold_start
    for located in candidate:
        if located.end_char <= cursor:
            continue
        assert located.start_char <= cursor
        cursor = min(gold_end, located.end_char)
        if cursor == gold_end:
            break
    assert cursor == gold_end
