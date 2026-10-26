"""数据库CLI门禁、脱敏和资源关闭；真实持久化另由postgres_smoke验收。"""

from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.config import Settings
from scripts import ticket_db


@pytest.mark.parametrize("action", ["check", "upgrade", "rollback-check"])
def test_cli_without_database_returns_sanitized_error(monkeypatch, capsys, action):
    monkeypatch.setattr(sys, "argv", ["ticket_db", action])
    monkeypatch.setattr(ticket_db, "load_settings", lambda: Settings(_env_file=None, database_url=None))
    assert ticket_db.main() == 1
    assert '"status": "error"' in capsys.readouterr().err


@pytest.mark.parametrize("action", ["check", "upgrade", "rollback-check"])
def test_cli_failure_is_sanitized_and_database_is_disposed(monkeypatch, capsys, action):
    events = []
    class FakeDatabase:
        def __init__(self, *_a, **_k):
            pass

        @contextmanager
        def transaction(self):
            raise SQLAlchemyError("private-database-url-and-parameters")
            yield

        def close(self):
            events.append("dispose")

    def fail(_db):
        raise SQLAlchemyError("private-database-url-and-parameters")

    monkeypatch.setattr(sys, "argv", ["ticket_db", action])
    monkeypatch.setattr(ticket_db, "load_settings", lambda: Settings(_env_file=None, database_url="postgresql+psycopg://test:secret@127.0.0.1:5434/test"))
    monkeypatch.setattr(ticket_db, "Database", FakeDatabase)
    monkeypatch.setattr(ticket_db, "upgrade", fail)
    monkeypatch.setattr(ticket_db, "verify_rollback", fail)
    assert ticket_db.main() == 1 and events == ["dispose"]
    assert "private-database-url-and-parameters" not in capsys.readouterr().err


def test_smoke_gate_does_not_create_output_or_database(tmp_path):
    output = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, "-m", "scripts.postgres_smoke", "--output-dir", str(output)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 2 and "--local" in result.stderr
    assert not output.exists()
