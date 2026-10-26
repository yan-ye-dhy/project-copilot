"""D19离线/收费开关、固定证据合同与独立归档完整性，不发真实请求。"""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest

from evals import prompt_reliability as run
from project_copilot.config import Settings
from project_copilot.infrastructure.models import ChatGeneration, ChatRequestError


ROOT = Path(__file__).resolve().parents[1]


def forbid(*_args, **_kwargs):
    pytest.fail("此离线路径不得读取真实配置或创建外部客户端")


def reseal(directory):
    path = directory / "manifest.json"
    value = json.loads(path.read_text())
    for name in value["files"]:
        value["files"][name] = sha256((directory / name).read_bytes()).hexdigest()
    run._write(path, value)


def test_plan_has_two_templates_one_factor_eight_calls_and_no_holdout(monkeypatch):
    monkeypatch.setattr(run, "load_settings", forbid)
    monkeypatch.setattr(run, "create_chat_client", forbid)
    plan = run.build_plan()
    assert plan["planned_model_calls"] == 8
    assert plan["embedding_calls"] == plan["qdrant_requests"] == 0
    assert set(plan["prompt_variants"]) == {"baseline", "fewshot"}
    assert plan["dataset"]["origin"].startswith("new-authored")
    assert all(c["case_id"].startswith("d19-") for c in plan["dataset"]["cases"])
    assert plan["generation"]["max_retries"] == 0
    assert plan["generation"]["temperature"] == 0
    for case in plan["dataset"]["cases"]:
        messages = [run.build_messages(case["question"], run.case_chunks(case), template=t) for t in run.VARIANTS.values()]
        assert messages[0][1] == messages[1][1]


def test_offline_runs_real_answerer_but_marks_controlled_responses_and_pending_review(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "load_settings", forbid)
    monkeypatch.setattr(run, "create_chat_client", forbid)
    output = tmp_path / "offline"
    summary = run.collect(output)
    assert summary["model_api_calls"] == 0 and summary["controlled_fake_calls"] == 8
    assert summary["completed_pairs"] == 5
    assert summary["semantic_effect_measured"] is False
    assert summary["fact_support_human_review"] == "pending"
    assert all(g["contract_valid"] == g["cases"] == 5 for g in summary["variants"].values())
    rows = [json.loads(line) for line in (output / "answers.jsonl").read_text().splitlines()]
    assert len(rows) == 10
    assert all(r["result"]["generation"] is None and r["model_calls"] == 0
               for r in rows if r["case_id"] == "d19-no-evidence")
    config = json.loads((output / "config.json").read_text())
    assert "api_key" not in json.dumps(config)
    assert run.verify(output)["state"] == "verified"


def test_live_gate_precedes_configuration_and_output_creation(tmp_path):
    directory = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, "-m", "evals.prompt_reliability", "collect", "--output-dir", str(directory)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2 and "--live" in result.stderr
    assert not directory.exists()


def test_existing_output_refused_before_live_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "load_settings", forbid)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        run.collect(tmp_path, live=True)


@pytest.mark.parametrize("field,value", [("deepseek_max_tokens", 512), ("deepseek_temperature", 0.5)])
def test_live_generation_drift_stops_before_client_creation(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr(run, "load_settings", lambda: Settings(_env_file=None, **{field: value}))
    monkeypatch.setattr(run, "create_chat_client", forbid)
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="冻结对照"):
        run.collect(output, live=True)
    assert not output.exists()


@pytest.mark.parametrize("recompute_fingerprint", [False, True])
def test_modified_v0_template_is_not_reused_even_if_resealed(tmp_path, monkeypatch, recompute_fingerprint):
    value = json.loads(run.BASELINE_PATH.read_text())
    value["prompt"]["system_message"] += "变更"
    if recompute_fingerprint:
        value["configuration_fingerprint"] = run.FINGERPRINT_PREFIX + run.fingerprint({
            k: v for k, v in value.items() if k != "configuration_fingerprint"
        })
    path = tmp_path / "changed.json"
    run._write(path, value)
    monkeypatch.setattr(run, "BASELINE_PATH", path)
    with pytest.raises(ValueError, match="V0"):
        run.build_plan()


@pytest.mark.parametrize("mutation", ["bytes", "summary_resealed", "raw_answer_resealed", "missing_row_resealed"])
def test_verify_rejects_corruption_and_inconsistent_resealed_results(tmp_path, mutation):
    output = tmp_path / "run"
    run.collect(output)
    if mutation == "bytes":
        with (output / "summary.json").open("a") as f:
            f.write("\n")
    elif mutation == "summary_resealed":
        summary = json.loads((output / "summary.json").read_text())
        summary["variants"]["fewshot"]["expected_status_matches"] = 999
        run._write(output / "summary.json", summary)
        reseal(output)
    else:
        rows = [json.loads(line) for line in (output / "answers.jsonl").read_text().splitlines()]
        if mutation == "raw_answer_resealed":
            rows[0]["raw_generation"]["content"] = '{"status":"answered","answer":"伪造变更","citation_numbers":[1]}'
        else:
            rows.pop()
        (output / "answers.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        reseal(output)
    with pytest.raises(ValueError):
        run.verify(output)


def test_live_upstream_failure_stops_remaining_requests_and_closes_client(tmp_path, monkeypatch):
    events = []

    class FailedChat:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("closed")

        def generate_json(self, _messages):
            events.append("requested")
            raise ChatRequestError("private-provider-message")

    monkeypatch.setattr(run, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run, "create_chat_client", lambda _s: FailedChat())
    output = tmp_path / "partial"
    summary = run.collect(output, live=True)
    assert events == ["requested", "closed"]
    assert summary["state"] == "partial" and summary["model_api_calls"] == 1
    assert summary["completed_pairs"] == 0
    assert "private-provider-message" not in (output / "answers.jsonl").read_text()
    assert run.verify(output)["experiment_state"] == "partial"


def test_contract_failure_keeps_failed_pair_and_never_repairs_or_retries(tmp_path, monkeypatch):
    class BrokenChat:
        calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def generate_json(self, _messages):
            self.calls += 1
            return ChatGeneration(content="broken-json", model="controlled-invalid-model", usage=None)

    chat = BrokenChat()
    monkeypatch.setattr(run, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run, "create_chat_client", lambda _s: chat)
    output = tmp_path / "invalid"
    summary = run.collect(output, live=True)
    assert chat.calls == 8 and summary["model_api_calls"] == 8
    assert summary["completed_pairs"] == 5
    assert all(len(g["errors"]) == 4 and g["expected_status_matches"] == 1 for g in summary["variants"].values())
    assert run.verify(output)["state"] == "verified"


def test_unicode_escaped_injection_marker_is_checked_after_json_decode():
    case = run.build_plan()["dataset"]["cases"][3]
    chat = run._ControlledChat()
    escaped = "".join(f"\\u{ord(c):04x}" for c in run.MARKER)
    chat.generation = ChatGeneration(
        content='{"status":"answered","answer":"' + escaped + '","citation_numbers":[1]}',
        model="controlled-injection-output", usage=None,
    )
    row = run.evaluate_case(case, "baseline", chat, run.build_plan()["generation"])
    assert row["contract_valid"] is True and row["expected_status_match"] is True
    assert row["injection_marker_absent"] is False
