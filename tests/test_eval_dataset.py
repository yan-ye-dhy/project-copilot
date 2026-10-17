"""离线检查证据可回溯性；确认态仅是临时夹具，不代表真实人工标注。"""

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from evals.validate_dataset import DatasetValidationError, validate_dataset
from project_copilot.rag.loaders import load_document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "evals" / "validate_dataset.py"


@pytest.fixture
def dataset(tmp_path):
    """短中文原文足以区分字符位置、字节位置和版本身份。"""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source_file = corpus / "note.md"
    text = "# 本地排障\n\n启动失败时应检查环境配置，错误信息不应包含密钥。\n"
    source_file.write_text(text, encoding="utf-8")
    document = load_document(source_file)
    quote = "启动失败时应检查环境配置"
    start = text.index(quote)
    records = {
        "sources": [{
            "source_id": "src-001", "source_type": "project_experience",
            "title": "本地排障", "path": "corpus/note.md",
            "document_id": str(document.document_id),
            "document_version": document.content_hash, "language": "zh-CN",
            "provenance": [{"uri": "https://github.com/yan-ye-dhy/project-copilot",
                            "revision": "3f31599", "retrieved_on": "2026-10-16",
                            "note": "测试夹具，非正式语料"}], "prepared_by": "codex",
        }],
        "questions": [{
            "question_id": "q-001", "category": "knowledge", "family_id": "f-001",
            "question": "启动失败时应该检查什么？", "standalone_question": None,
            "history": [], "source_ids": ["src-001"], "prepared_by": "codex",
        }],
        "labels.draft": [{
            "question_id": "q-001", "answerable": True,
            "reference_answer": "应检查环境配置。", "evidence": [{
                "evidence_id": "ev-001", "source_id": "src-001",
                "document_id": str(document.document_id),
                "document_version": document.content_hash, "quote": quote,
                "page": None, "start_char": start, "end_char": start + len(quote),
                "section": "本地排障",
            }], "unanswerable_reason": None, "review_status": "pending",
            "reviewed_by": None, "reviewed_at": None,
        }],
    }
    return tmp_path, records


def write_dataset(dataset):
    directory, records = dataset
    for name, rows in records.items():
        (directory / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
    return directory


def test_draft_validates_without_claiming_human_gold(dataset):
    result = validate_dataset(write_dataset(dataset))
    assert result["status"] == "ok"
    assert (result["sources"], result["questions"], result["answerable"]) == (1, 1, 1)
    assert (result["confirmed"], result["pending"], result["ready_for_baseline"]) == (0, 1, False)


def test_review_gate_rejects_valid_but_unreviewed_draft(dataset):
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset), require_reviewed=True)


def test_explicit_synthetic_reviewer_passes_review_gate(dataset):
    label = dataset[1]["labels.draft"][0]
    label.update(review_status="confirmed", reviewed_by="test-human-reviewer",
                 reviewed_at="2026-10-16T12:00:00+08:00")
    result = validate_dataset(write_dataset(dataset), require_reviewed=True)
    assert result["confirmed"] == 1 and result["ready_for_baseline"] is True


@pytest.mark.parametrize("field,value", [
    ("document_id", "00000000-0000-5000-8000-000000000000"),
    ("document_version", "0" * 64),
])
def test_source_identity_must_match_actual_file(dataset, field, value):
    dataset[1]["sources"][0][field] = value
    dataset[1]["labels.draft"][0]["evidence"][0][field] = value
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("path", ["corpus/missing.md", "../outside.md", "/tmp/outside.md"])
def test_source_paths_must_exist_inside_corpus(dataset, path):
    dataset[1]["sources"][0]["path"] = path
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_source_requires_traceable_provenance(dataset):
    dataset[1]["sources"][0]["provenance"] = []
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_symlink_cannot_escape_dataset_directory(dataset):
    directory = dataset[0]
    outside = directory.parent / f"{directory.name}-outside.md"
    outside.write_bytes((directory / "corpus/note.md").read_bytes())
    (directory / "corpus/escape.md").symlink_to(outside)
    dataset[1]["sources"][0]["path"] = "corpus/escape.md"
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("field,value", [
    ("quote", "不存在的原文"), ("start_char", 0), ("page", 0),
])
def test_evidence_must_point_to_exact_original_text(dataset, field, value):
    dataset[1]["labels.draft"][0]["evidence"][0][field] = value
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("kind", ["question", "evidence"])
def test_duplicate_identifiers_are_rejected(dataset, kind):
    records = dataset[1]
    if kind == "question":
        records["questions"].append(deepcopy(records["questions"][0]))
    else:
        evidence = records["labels.draft"][0]["evidence"]
        evidence.append(deepcopy(evidence[0]))
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_different_evidence_ids_cannot_duplicate_the_same_gold_position(dataset):
    items = dataset[1]["labels.draft"][0]["evidence"]
    duplicate = deepcopy(items[0])
    duplicate["evidence_id"] = "ev-different-id"
    items.append(duplicate)
    with pytest.raises(DatasetValidationError, match="同一原文位置"):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("same_path", [True, False])
def test_sources_cannot_double_count_the_same_document(dataset, same_path):
    directory, records = dataset
    duplicate = deepcopy(records["sources"][0])
    duplicate["source_id"] = "src-duplicate-document"
    if not same_path:
        (directory / "corpus/copy.md").write_bytes((directory / "corpus/note.md").read_bytes())
        duplicate["path"] = "corpus/copy.md"
    records["sources"].append(duplicate)
    with pytest.raises(DatasetValidationError, match="实际路径或文档ID重复"):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("category,answerable", [("knowledge", False), ("unanswerable", True)])
def test_question_category_and_answerability_must_agree(dataset, category, answerable):
    dataset[1]["questions"][0]["category"] = category
    label = dataset[1]["labels.draft"][0]
    label["answerable"] = answerable
    if not answerable:
        label.update(reference_answer=None, evidence=[], unanswerable_reason="语料缺少该信息。")
    with pytest.raises(DatasetValidationError, match="题型与可回答"):
        validate_dataset(write_dataset(dataset))


def test_every_question_requires_one_label(dataset):
    dataset[1]["labels.draft"].clear()
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_unknown_source_cannot_become_gold(dataset):
    dataset[1]["labels.draft"][0]["evidence"][0]["source_id"] = "unknown"
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_unanswerable_question_has_reason_and_no_fabricated_gold(dataset):
    records = dataset[1]
    records["questions"][0]["category"] = "unanswerable"
    label = records["labels.draft"][0]
    label.update(answerable=False, unanswerable_reason="语料未覆盖此信息。")
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))
    label.update(reference_answer=None, evidence=[])
    assert validate_dataset(write_dataset(dataset))["unanswerable"] == 1


@pytest.mark.parametrize("reviewer,timestamp", [
    (None, None), ("codex", "2026-10-16T12:00:00+08:00"),
    ("test-human-reviewer", "2026-10-16T12:00:00"),
])
def test_confirmation_requires_human_attribution_and_zoned_time(dataset, reviewer, timestamp):
    dataset[1]["labels.draft"][0].update(
        review_status="confirmed", reviewed_by=reviewer, reviewed_at=timestamp,
    )
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("missing", ["history", "standalone_question"])
def test_follow_up_requires_history_and_standalone_meaning(dataset, missing):
    question = dataset[1]["questions"][0]
    question.update(category="follow_up", question="那应检查什么？",
                    history=[{"role": "user", "content": "应用启动失败了。"}],
                    standalone_question="应用启动失败时应该检查什么？")
    question[missing] = [] if missing == "history" else None
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


def test_chunk_dependent_gold_field_is_rejected(dataset):
    dataset[1]["labels.draft"][0]["evidence"][0]["chunk_id"] = "current-chunk"
    with pytest.raises(DatasetValidationError):
        validate_dataset(write_dataset(dataset))


@pytest.mark.parametrize("require_reviewed,exit_code,status", [(False, 0, "ok"), (True, 1, "error")])
def test_cli_returns_machine_readable_result(dataset, require_reviewed, exit_code, status):
    command = [sys.executable, str(SCRIPT), "--dataset", str(write_dataset(dataset))]
    if require_reviewed:
        command.append("--require-reviewed")
    process = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
    assert process.returncode == exit_code
    assert json.loads(process.stdout)["status"] == status
