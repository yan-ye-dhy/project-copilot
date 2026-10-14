"""D5单条脚本的收费开关、JSON结果和脱敏错误；假服务不请求模型。"""

from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from project_copilot.infrastructure.models import ChatConfigError, ChatRequestError, ChatResponseError
from project_copilot.rag.answering import AnswerResponseError, prompt_info
from project_copilot.rag.ingestion import ManifestError
from project_copilot.schemas.rag import RagAnswer


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rag_smoke.py"


@pytest.fixture
def cli_module():
    spec = importlib.util.spec_from_file_location("rag_smoke_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("arguments,exit_code", [([], 2), (["--help"], 0)])
def test_live_gate_precedes_settings_and_service_creation(cli_module, monkeypatch, capsys, arguments, exit_code) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("没有--live时不能读取配置或构造问答客户端")
    monkeypatch.setattr(cli_module, "load_settings", forbidden)
    monkeypatch.setattr(cli_module, "create_answer_service", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *arguments])

    with pytest.raises(SystemExit) as error:
        cli_module.main()
    assert error.value.code == exit_code
    output = capsys.readouterr()
    assert "--live" in output.out + output.err


def test_live_success_prints_one_fixed_question_result_and_closes_service(cli_module, monkeypatch, capsys) -> None:
    events = []
    settings = object()
    expected = RagAnswer(
        question=cli_module.SMOKE_QUESTION, status="insufficient_evidence", answer="当前没有可用证据。",
        citations=(), evidence_count=0, prompt=prompt_info(), index_version=None, generation=None,
    )

    def load_fake_settings():
        events.append("settings")
        return settings

    class FakeService:
        def answer(self, question):
            events.append(("answer", question))
            return expected

    @contextmanager
    def fake_factory(active_settings):
        assert active_settings is settings
        events.append("entered")
        try:
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(cli_module, "load_settings", load_fake_settings)
    monkeypatch.setattr(cli_module, "create_answer_service", fake_factory)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--live"])

    assert cli_module.main() == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == expected.model_dump(mode="json")
    assert events == ["settings", "entered", ("answer", cli_module.SMOKE_QUESTION), "closed"]


@pytest.mark.parametrize("error_type", [
    ChatConfigError, ChatRequestError, ChatResponseError, ManifestError, AnswerResponseError,
])
def test_live_failure_has_exit_one_safe_json_no_retry_and_resource_cleanup(
    cli_module, monkeypatch, capsys, error_type,
) -> None:
    events = []
    monkeypatch.setattr(cli_module, "load_settings", lambda: object())

    class FakeService:
        def answer(self, question):
            events.append(("answer", question))
            raise error_type("private-key-and-raw-provider-output")

    @contextmanager
    def fake_factory(_settings):
        events.append("entered")
        try:
            yield FakeService()
        finally:
            events.append("closed")

    monkeypatch.setattr(cli_module, "create_answer_service", fake_factory)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--live"])

    assert cli_module.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"status": "error", "message": "问答未完成，请检查配置、索引或上游结果"}
    assert "private-key-and-raw-provider-output" not in output.err
    assert "Traceback" not in output.err
    assert events == ["entered", ("answer", cli_module.SMOKE_QUESTION), "closed"]


def test_live_settings_failure_is_safe_and_never_creates_service(cli_module, monkeypatch, capsys) -> None:
    def fail_settings():
        raise ChatConfigError("private-config-value")
    def forbidden_factory(*_args, **_kwargs):
        pytest.fail("配置加载失败时不能创建外部客户端")
    monkeypatch.setattr(cli_module, "load_settings", fail_settings)
    monkeypatch.setattr(cli_module, "create_answer_service", forbidden_factory)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--live"])

    assert cli_module.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["status"] == "error"
    assert "private-config-value" not in output.err
