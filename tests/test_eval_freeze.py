"""D8离线划分与冻结契约；confirmed仅用于临时夹具，不登记为用户复核。"""

import json
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from shutil import copytree

import pytest

from evals.freeze_dataset import freeze_dataset, validate_split, verify_freeze
from evals.validate_dataset import CATEGORIES, DatasetValidationError
from project_copilot.rag.loaders import load_document


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_inputs(dataset):
    root, records, split = dataset
    for name, rows in records.items():
        (root / f"{name}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
    (root / "split_manifest.json").write_text(
        json.dumps(split, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return root


@pytest.fixture
def dataset(tmp_path):
    """32题、24/8划分，双方有可答/无答；人工身份为明确的测试占位者。"""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    source_file = corpus / "note.md"
    text = "# 离线测试夹具\n\n启动失败时应检查环境配置。\n"
    source_file.write_text(text, encoding="utf-8")
    document = load_document(source_file)
    quote = "启动失败时应检查环境配置"
    start = text.index(quote)
    records = {
        "sources": [{
            "source_id": "fixture-note", "source_type": "project_experience",
            "title": "明确的临时测试资料", "path": "corpus/note.md",
            "document_id": str(document.document_id),
            "document_version": document.content_hash, "language": "zh-CN",
            "provenance": [{"uri": "https://example.com/test-fixture", "revision": "fixture-v1",
                            "retrieved_on": "2026-10-17", "note": "合成夹具，非真实人工gold"}],
            "prepared_by": "codex",
        }],
        "questions": [], "labels.draft": [],
    }
    for number in range(32):
        question_id = f"fixture-{number + 1:03d}"
        category = CATEGORIES[number % len(CATEGORIES)]
        follow_up = category == "follow_up"
        answerable = category != "unanswerable"
        records["questions"].append({
            "question_id": question_id, "category": category,
            "family_id": f"fixture-family-{number + 1:03d}",
            "question": "那应检查什么？" if follow_up else "启动失败应检查什么？",
            "standalone_question": "应用启动失败应检查什么？" if follow_up else None,
            "history": [{"role": "user", "content": "应用启动失败了。"}] if follow_up else [],
            "source_ids": ["fixture-note"], "prepared_by": "codex",
        })
        evidence = {
            "evidence_id": f"{question_id}-e1", "source_id": "fixture-note",
            "document_id": str(document.document_id), "document_version": document.content_hash,
            "quote": quote, "page": None, "start_char": start,
            "end_char": start + len(quote), "section": "离线测试夹具",
        }
        records["labels.draft"].append({
            "question_id": question_id, "answerable": answerable,
            "reference_answer": "检查环境配置。" if answerable else None,
            "evidence": [evidence] if answerable else [],
            "unanswerable_reason": None if answerable else "测试语料不含此信息。",
            "review_status": "confirmed", "reviewed_by": "test-human-reviewer",
            "reviewed_at": "2026-10-17T12:00:00+08:00",
        })
    ids = [question["question_id"] for question in records["questions"]]
    split = {"schema_version": 1, "split_id": "fixture-split-v1", "dev": ids[:24], "holdout": ids[24:]}
    return tmp_path, records, split


def test_pending_labels_allow_split_preflight_but_cannot_create_freeze(dataset):
    dataset[1]["labels.draft"][-1].update(
        review_status="pending", reviewed_by=None, reviewed_at=None,
    )
    root = write_inputs(dataset)
    result = validate_split(root)
    assert (result["questions"], result["dev"], result["holdout"]) == (32, 24, 8)
    assert (result["confirmed"], result["pending"], result["ready_for_freeze"]) == (31, 1, False)
    with pytest.raises(DatasetValidationError):
        freeze_dataset(root)
    assert not (root / "frozen_manifest.json").exists()


def test_family_can_group_multiple_questions_but_must_stay_on_one_side(dataset):
    questions = dataset[1]["questions"]
    questions[8]["family_id"] = questions[0]["family_id"]
    assert validate_split(write_inputs(dataset))["ready_for_freeze"] is True
    questions[24]["family_id"] = questions[0]["family_id"]
    with pytest.raises(DatasetValidationError):
        validate_split(write_inputs(dataset))


@pytest.mark.parametrize("defect", ["duplicate", "missing", "unknown", "overlap", "empty", "wrong-ratio"])
def test_every_question_belongs_to_exactly_one_correctly_sized_split(dataset, defect):
    split = dataset[2]
    if defect == "duplicate":
        split["dev"][1] = split["dev"][0]
    elif defect == "missing":
        split["dev"].pop()
    elif defect == "unknown":
        split["dev"][0] = "unknown-sensitive-question"
    elif defect == "overlap":
        split["holdout"][0] = split["dev"][0]
    elif defect == "empty":
        split["dev"].extend(split["holdout"])
        split["holdout"].clear()
    else:
        split["dev"].append(split["holdout"].pop())
    with pytest.raises(DatasetValidationError):
        validate_split(write_inputs(dataset))


def test_overall_categories_and_each_side_answerability_remain_represented(dataset):
    questions, labels = dataset[1]["questions"], dataset[1]["labels.draft"]
    # 将holdout唯一无答案题移至dev，并交换一题可答，大小不变但覆盖退化。
    dataset[2]["dev"][0], dataset[2]["holdout"][6] = (
        dataset[2]["holdout"][6], dataset[2]["dev"][0],
    )
    with pytest.raises(DatasetValidationError):
        validate_split(write_inputs(dataset))
    dataset[2]["dev"][0], dataset[2]["holdout"][6] = (
        dataset[2]["holdout"][6], dataset[2]["dev"][0],
    )
    for question, label in zip(questions, labels, strict=True):
        if question["category"] == "terminology":
            question["category"] = "knowledge"
    with pytest.raises(DatasetValidationError):
        validate_split(write_inputs(dataset))


@pytest.mark.parametrize("total", [20, 52])
def test_d8_requires_thirty_to_fifty_questions(dataset, total):
    records = dataset[1]
    if total == 20:
        records["questions"][:] = records["questions"][:total]
        records["labels.draft"][:] = records["labels.draft"][:total]
    else:
        # 保留32题夹具的合法内容，但给新增题与证据独立ID，隔离题量上限。
        for number in range(total - 32):
            question = deepcopy(records["questions"][number])
            label = deepcopy(records["labels.draft"][number])
            question_id = f"fixture-{33 + number:03d}"
            question.update(question_id=question_id, family_id=f"extra-family-{number}")
            label["question_id"] = question_id
            for evidence in label["evidence"]:
                evidence["evidence_id"] = f"{question_id}-e1"
            records["questions"].append(question)
            records["labels.draft"].append(label)
    ids = [question["question_id"] for question in records["questions"]]
    holdout_count = round(total / 4)
    dataset[2].update(dev=ids[:-holdout_count], holdout=ids[-holdout_count:])
    with pytest.raises(DatasetValidationError):
        validate_split(write_inputs(dataset))


def test_freeze_registers_exact_inputs_and_verifies_without_rewriting(dataset):
    root = write_inputs(dataset)
    created = freeze_dataset(root)
    manifest_file = root / "frozen_manifest.json"
    original = manifest_file.read_bytes()
    manifest = json.loads(original)
    assert re.fullmatch(r"dataset-v1-[0-9a-f]{64}", created["fingerprint"])
    assert manifest["fingerprint"] == created["fingerprint"]
    assert set(manifest["files"]) == {
        "sources.jsonl", "questions.jsonl", "labels.draft.jsonl",
        "split_manifest.json", "corpus/note.md",
    }
    assert verify_freeze(root)["fingerprint"] == created["fingerprint"]
    assert manifest_file.read_bytes() == original
    with pytest.raises(DatasetValidationError):
        freeze_dataset(root)
    assert manifest_file.read_bytes() == original


def test_identical_inputs_have_the_same_identity_in_another_directory(dataset, tmp_path_factory):
    root = write_inputs(dataset)
    copied = tmp_path_factory.mktemp("independent-freeze") / "dataset"
    copytree(root, copied)
    assert freeze_dataset(root)["fingerprint"] == freeze_dataset(copied)["fingerprint"]


@pytest.mark.parametrize("field,value", [
    ("fingerprint", "dataset-v1-" + "0" * 64),
    ("files", {"../../PRIVATE-PATH": "0" * 64}),
    ("split_id", "unregistered-split"),
    ("frozen_at", "2026-10-17T12:00:00"),
    ("text_policy", "silently-normalized-text"),
])
def test_freeze_manifest_cannot_substitute_identity_or_text_policy(dataset, field, value):
    root = write_inputs(dataset)
    freeze_dataset(root)
    path = root / "frozen_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(DatasetValidationError) as caught:
        verify_freeze(root)
    assert "PRIVATE-PATH" not in str(caught.value)


def test_freeze_text_policy_requires_lf_even_when_versions_are_coherent(dataset):
    root, records, _ = dataset
    path = root / "corpus/note.md"
    path.write_bytes(path.read_bytes() + b"\r\n")
    document = load_document(path)
    records["sources"][0].update(document_id=str(document.document_id), document_version=document.content_hash)
    for label in records["labels.draft"]:
        for evidence in label["evidence"]:
            evidence.update(document_id=str(document.document_id), document_version=document.content_hash)
    with pytest.raises(DatasetValidationError):
        freeze_dataset(write_inputs(dataset))
    assert not (root / "frozen_manifest.json").exists()


@pytest.mark.parametrize("filename", ["sources.jsonl", "questions.jsonl", "labels.draft.jsonl", "split_manifest.json"])
def test_even_semantically_equivalent_registered_byte_changes_break_freeze(dataset, filename):
    root = write_inputs(dataset)
    freeze_dataset(root)
    path = root / filename
    text = path.read_text(encoding="utf-8")
    path.write_text(text + "\n" if filename.endswith(".json") else text.replace(": ", ":", 1), encoding="utf-8")
    with pytest.raises(DatasetValidationError):
        verify_freeze(root)


@pytest.mark.parametrize("change", ["answer", "review", "coherent-corpus"])
def test_legally_updated_gold_or_source_is_still_a_new_frozen_input(dataset, change):
    root = write_inputs(dataset)
    freeze_dataset(root)
    records = dataset[1]
    if change == "answer":
        records["labels.draft"][0]["reference_answer"] = "先检查环境配置。"
    elif change == "review":
        records["labels.draft"][0]["reviewed_at"] = "2026-10-17T12:05:00+08:00"
    else:
        source_file = root / "corpus/note.md"
        source_file.write_text(source_file.read_text(encoding="utf-8") + "\n增加的排障说明。\n", encoding="utf-8")
        document = load_document(source_file)
        records["sources"][0].update(document_id=str(document.document_id), document_version=document.content_hash)
        for label in records["labels.draft"]:
            for evidence in label["evidence"]:
                evidence.update(document_id=str(document.document_id), document_version=document.content_hash)
    assert validate_split(write_inputs(dataset))["ready_for_freeze"] is True
    with pytest.raises(DatasetValidationError):
        verify_freeze(root)


def test_unregistered_review_notes_do_not_redefine_the_frozen_dataset(dataset):
    root = write_inputs(dataset)
    fingerprint = freeze_dataset(root)["fingerprint"]
    (root / "README.md").write_text("补充教学说明。\n", encoding="utf-8")
    (root / "REVIEW_QUEUE.md").write_text("改进人工阅读视图。\n", encoding="utf-8")
    assert verify_freeze(root)["fingerprint"] == fingerprint


@pytest.mark.parametrize("registered", ["sources.jsonl", "corpus/note.md"])
def test_symlink_cannot_become_a_frozen_input_even_if_content_matches(dataset, registered):
    root = write_inputs(dataset)
    path = root / registered
    outside = root.parent / f"{root.name}-outside-{path.name}"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(DatasetValidationError):
        freeze_dataset(root)


def test_missing_freeze_has_no_implicit_creation(dataset):
    root = write_inputs(dataset)
    with pytest.raises(DatasetValidationError):
        verify_freeze(root)
    assert not (root / "frozen_manifest.json").exists()


@pytest.mark.parametrize("filename", ["split_manifest.json", "frozen_manifest.json"])
@pytest.mark.parametrize("payload", ["not JSON PRIVATE-TEXT", '{"schema_version":1,"schema_version":2,"secret":"PRIVATE-TEXT"}'])
def test_invalid_or_ambiguous_json_reports_a_safe_error(dataset, filename, payload):
    root = write_inputs(dataset)
    if filename == "frozen_manifest.json":
        freeze_dataset(root)
    (root / filename).write_text(payload, encoding="utf-8")
    operation = verify_freeze if filename == "frozen_manifest.json" else validate_split
    with pytest.raises(DatasetValidationError) as caught:
        operation(root)
    assert "PRIVATE-TEXT" not in str(caught.value)


def test_cli_is_offline_preflight_then_explicit_freeze_and_verification(dataset):
    root = write_inputs(dataset)
    command = [sys.executable, "-m", "evals.freeze_dataset", "--dataset", str(root)]
    for args in ([], ["--freeze"], ["--verify"]):
        process = subprocess.run(command + args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
        assert process.returncode == 0, process.stdout + process.stderr
        assert json.loads(process.stdout)["status"] == "ok"
        if not args:
            assert not (root / "frozen_manifest.json").exists()
    (root / "split_manifest.json").write_text("invalid PRIVATE-TEXT", encoding="utf-8")
    process = subprocess.run(command + ["--verify"], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10)
    assert process.returncode == 1
    assert json.loads(process.stdout)["status"] == "error"
    assert "PRIVATE-TEXT" not in process.stdout + process.stderr
