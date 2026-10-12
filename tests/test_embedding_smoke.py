"""真实模型脚本必须显式开启，默认执行只显示参数错误。"""

from pathlib import Path
import json
import runpy
import subprocess
import sys

import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure import models


def test_smoke_requires_explicit_live_flag() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/embedding_smoke.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2
    assert "真实请求需显式添加--live" in result.stderr
    assert result.stdout == ""


def test_live_flag_without_key_reports_error_without_a_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts/embedding_smoke.py"
    main = runpy.run_path(str(script))["main"]
    monkeypatch.setitem(
        main.__globals__, "load_settings", lambda: Settings(_env_file=None, zhipu_api_key=None)
    )

    def forbid_model_client(*_args: object, **_kwargs: object) -> None:
        pytest.fail("缺少智谱密钥时不应创建HTTP客户端")

    monkeypatch.setattr(models, "OpenAI", forbid_model_client)
    monkeypatch.setattr(sys, "argv", [str(script), "--live"])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["status"] == "error"
    assert "PROJECT_COPILOT_ZHIPU_API_KEY" in error["message"]
