"""CLI parsing never connects or authorizes without an explicit local operator."""
import sys
from unittest.mock import Mock

import pytest

from scripts import release_control as cli
from project_copilot.config import Settings


@pytest.mark.parametrize("arguments", [
    ["PERSONAL_DEMO"], ["PERSONAL_DEMO", "--user", "alice", "--minutes", "91"],
    ["PERSONAL_DEMO", "--user", "alice", "--minutes", "0"],
    ["PERSONAL_DEMO", "--user", "alice", "--evidence", "unused.json"],
    ["STAGING", "--user", "alice"],
])
def test_personal_cli_refuses_invalid_options_before_database(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["release_control", *arguments])
    database = Mock(side_effect=AssertionError("unexpected database"))
    monkeypatch.setattr(cli, "Database", database)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    database.assert_not_called()


def test_personal_cli_requires_interactive_terminal(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["release_control", "PERSONAL_DEMO", "--user", "alice"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    database = Mock(side_effect=AssertionError("unexpected database"))
    monkeypatch.setattr(cli, "Database", database)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    database.assert_not_called()


def test_personal_cli_rejects_ordinary_database_before_connecting(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["release_control", "PERSONAL_DEMO", "--user", "alice"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    settings = Settings(_env_file=None, environment="production", public_demo=True,
        public_proxy_secret="a" * 48, auth_public_origin="https://personal.example",
        database_url="postgresql+psycopg://ordinary:synthetic-password@localhost:5433/ordinary")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    database = Mock(side_effect=AssertionError("unexpected database"))
    monkeypatch.setattr(cli, "Database", database)
    assert cli.main() == 1
    database.assert_not_called()
    captured = capsys.readouterr()
    assert "synthetic-password" not in captured.err + captured.out
