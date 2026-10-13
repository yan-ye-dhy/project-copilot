"""用户查看脚本的真实进程输出、空结果、错误与跨进程ID稳定性。"""

import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import httpx
import pytest

from project_copilot import config
from project_copilot.infrastructure import models
from project_copilot.rag.loaders import load_document
from project_copilot.rag.splitters import split_document


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "inspect_document.py"
FIXTURES = ROOT / "tests" / "fixtures" / "documents"


def _run(path: Path, *, seed: str = "1", options: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = seed
    # 即使项目模型/应用配置不可用，离线查看器也不应读取它们。
    environment["PROJECT_COPILOT_ENVIRONMENT"] = "invalid"
    environment["PROJECT_COPILOT_ZHIPU_API_KEY"] = " "
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path), *options],
        cwd=path.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_inspector_exports_original_text_and_stable_ids_across_processes() -> None:
    path = FIXTURES / "sample.md"
    first = _run(path, seed="1")
    second = _run(path, seed="2")
    assert first.returncode == second.returncode == 0
    assert first.stderr == second.stderr == ""
    result = json.loads(first.stdout)
    assert result == json.loads(second.stdout)
    assert result["status"] == "ok"
    assert result["document"]["pages"][0]["text"] == path.read_text(encoding="utf-8")
    assert result["document"]["pages"][0]["page"] is None
    assert result["document"]["source_name"] == path.name
    assert result["splitter"]["chunk_size"] == 750
    assert result["splitter"]["chunk_overlap"] == 150
    assert result["max_chunk_chars"] <= 750
    assert result["chunks"] == [
        chunk.model_dump(mode="json") for chunk in split_document(load_document(path))
    ]
    assert result["chunk_count"] == len(result["chunks"])


def test_inspector_empty_document_is_explicit_successful_empty_result(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    completed = _run(path)
    assert completed.returncode == 0
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["status"] == "empty"
    assert result["chunk_count"] == result["max_chunk_chars"] == 0
    assert result["chunks"] == []
    assert result["message"]


@pytest.mark.parametrize("options", [
    ("--chunk-size", "0"),
    ("--chunk-size", "100", "--chunk-overlap", "100"),
])
def test_inspector_rejects_invalid_config_as_json(options: tuple[str, ...]) -> None:
    completed = _run(FIXTURES / "sample.txt", options=options)
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["status"] == "error"


def test_inspector_missing_file_is_clear_json_error(tmp_path: Path) -> None:
    completed = _run(tmp_path / "missing.pdf")
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["status"] == "error"


def test_inspector_does_not_load_settings_or_construct_model_clients(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("离线查看器不能读取应用配置、构造模型客户端或发起HTTP请求")

    monkeypatch.setattr(config, "load_settings", forbidden)
    monkeypatch.setattr(models, "OpenAI", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), str(FIXTURES / "sample.pdf")])
    main = runpy.run_path(str(SCRIPT))["main"]
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    assert [page["page"] for page in result["document"]["pages"]] == [0, 1, 2]
    assert [chunk["metadata"]["page"] for chunk in result["chunks"]] == [0, 2]
