"""D11只读检查的输入身份、单因素约束和非排名输出。"""

import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys

import pytest

from evals.chunking import inspect_chunking
from evals.metrics import EvaluationError


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/copilot_v0"
BASELINE = ROOT / "evals/results/dense-v0-20261019-01"
CANDIDATE = ROOT / "evals/configs/chunk_size500.json"


@pytest.fixture
def candidate(tmp_path: Path) -> Path:
    path = tmp_path / "candidate.json"
    shutil.copyfile(CANDIDATE, path)
    return path


def change(path: Path, **updates) -> None:
    value = json.loads(path.read_text())
    value.update(updates)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_actual_inspection_is_offline_reproducible_and_does_not_claim_retrieval(monkeypatch) -> None:
    def no_network(*args, **kwargs):
        raise AssertionError("D11不应发出网络请求")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    first = inspect_chunking(DATASET, BASELINE, CANDIDATE)
    assert first == inspect_chunking(DATASET, BASELINE, CANDIDATE)
    assert first["model_calls"] == first["qdrant_requests"] == 0
    assert first["retrieval_evaluation"] is first["answer_evaluation"] is None
    assert first["split"] == "dev"
    assert first["stats"]["baseline"]["chunk_count"] == 16
    assert first["stats"]["candidate"]["chunk_count"] == 27
    assert first["splitter_versions"]["candidate"] != first["splitter_versions"]["baseline"]
    assert len(first["cases"]) == 4
    for case in first["cases"]:
        for item in case["evidence"]:
            gold = item["gold"]
            for side in ("baseline", "candidate"):
                assert item[side]["single_chunk_covering_ids"]
                for chunk in item[side]["intersecting_chunks"]:
                    start = gold["start_char"] - chunk["start_char"]
                    end = gold["end_char"] - chunk["start_char"]
                    assert chunk["text"][start:end] == gold["quote"]


@pytest.mark.parametrize("updates", [
    {"chunk_overlap": 100}, {"separators": ["\n\n", "。", ""]}, {"chunk_size": 750},
])
def test_rejects_second_factor_or_unchanged_candidate(candidate: Path, updates: dict) -> None:
    splitter = json.loads(candidate.read_text())["splitter"]
    splitter.update(updates)
    change(candidate, splitter=splitter)
    with pytest.raises(EvaluationError, match="只允许改变chunk_size"):
        inspect_chunking(DATASET, BASELINE, candidate)


@pytest.mark.parametrize("ids", [[], ["pc-d7-003", "pc-d7-003"], ["pc-d7-005"], ["unknown"]])
def test_rejects_empty_duplicate_holdout_or_unknown_focus(candidate: Path, ids: list) -> None:
    change(candidate, focus_question_ids=ids)
    with pytest.raises(EvaluationError, match="开发集ID"):
        inspect_chunking(DATASET, BASELINE, candidate)


def test_requires_actual_dev_retrieval_bad_case(candidate: Path) -> None:
    change(candidate, focus_question_ids=["pc-d7-001"])
    with pytest.raises(EvaluationError, match="真实检索Bad Case"):
        inspect_chunking(DATASET, BASELINE, candidate)


@pytest.mark.parametrize("updates, message", [
    ({"baseline_archive_sha256": "0" * 64}, "归档清单"),
    ({"baseline_run_id": "different"}, "运行身份"),
    ({"baseline_configuration_fingerprint": "different"}, "配置指纹"),
    ({"extra": True}, "字段或版本"),
    ({"schema_version": True}, "字段或版本"),
])
def test_rejects_wrong_identity_or_unknown_contract(candidate: Path, updates: dict, message: str) -> None:
    change(candidate, **updates)
    with pytest.raises(EvaluationError, match=message):
        inspect_chunking(DATASET, BASELINE, candidate)


def test_tampered_record_cannot_be_used_to_motivate_candidate(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    shutil.copytree(BASELINE, baseline)
    path = baseline / "bad_cases.jsonl"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="输入已变化"):
        inspect_chunking(DATASET, baseline, CANDIDATE)


def test_frozen_gold_cannot_be_silently_changed_for_candidate(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    shutil.copytree(DATASET, dataset)
    path = dataset / "labels.draft.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["reference_answer"] += "（合法但变化的答案）"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="冻结"):
        inspect_chunking(dataset, BASELINE, CANDIDATE)


def test_cli_reports_malformed_input_as_error_without_traceback(candidate: Path) -> None:
    candidate.write_text('{"broken":', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "evals.chunking", "--dataset", str(DATASET),
         "--baseline-dir", str(BASELINE), "--candidate", str(candidate)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "error"
    assert result.stderr == ""
