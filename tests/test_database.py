"""阶段A：真实SQLAlchemy对象的本地生命周期，不连接数据库或以SQLite冒充PG。"""

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.exc import InvalidRequestError

from project_copilot import main
from project_copilot.config import Settings
from project_copilot.db.session import Database


URL = "postgresql+psycopg://local:fake-test-password@127.0.0.1:5434/project_copilot_test"


@pytest.mark.parametrize("url", [
    "not-a-url-private-password", "sqlite:///database.db", "postgresql://local:secret@localhost:5434/db",
    "postgresql+psycopg://local:secret@localhost/db", "postgresql+psycopg://local:secret@localhost:5434/",
    URL + "?options=private-password", "postgresql+psycopg://local:secret@localhost:99999/db",
])
def test_database_url_validation_rejects_unsupported_or_ambiguous_inputs_without_echo(url):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url=url)
    assert url not in str(exc.value)
    assert "fake-test-password" not in str(exc.value)


def test_database_url_is_optional_and_secret():
    assert Settings(_env_file=None, database_url=None).database_url is None
    settings = Settings(_env_file=None, database_url=URL)
    assert settings.database_url.get_secret_value() == URL
    assert "fake-test-password" not in repr(settings)
    assert "fake-test-password" not in settings.model_dump_json()


@pytest.mark.parametrize("fails", [False, True])
def test_empty_transaction_commit_or_rollback_and_permanent_session_close_without_connection(monkeypatch, fails):
    import psycopg

    def forbidden(*_a, **_k):
        pytest.fail("阶段A本地单测不得建立真实数据库连接")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    db = Database(URL)
    observed = []
    with db.transaction() as first:
        pass
    assert not first.in_transaction()
    with pytest.raises(InvalidRequestError):
        first.begin()
    try:
        with db.transaction() as session:
            assert session is not first
            event.listen(session, "after_commit", lambda _s: observed.append("commit"))
            event.listen(session, "after_rollback", lambda _s: observed.append("rollback"))
            if fails:
                raise RuntimeError("controlled business failure")
    except RuntimeError:
        assert fails
    assert observed == ["rollback" if fails else "commit"]
    with pytest.raises(InvalidRequestError):
        session.begin()
    assert db.engine.hide_parameters is True
    db.close()


@pytest.mark.parametrize("failure", [False, True])
def test_application_lifespan_constructs_and_disposes_configured_database(monkeypatch, failure):
    events = []

    class FakeDatabase:
        def __init__(self, url, **kwargs):
            assert url == URL
            assert kwargs == {"connect_timeout": 5, "pool_timeout": 5}
            events.append("engine")

        def close(self):
            events.append("dispose")

    monkeypatch.setattr(main, "Database", FakeDatabase)
    app = main.create_app(Settings(_env_file=None, database_url=URL))

    async def run():
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.database, FakeDatabase)
            if failure:
                raise RuntimeError("controlled application error")

    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert events == ["engine", "dispose"]


def test_unconfigured_application_does_not_create_database(monkeypatch):
    def forbidden(*_a, **_k):
        pytest.fail("没有数据库配置时不应创建Engine")

    monkeypatch.setattr(main, "Database", forbidden)
    app = main.create_app(Settings(_env_file=None, database_url=None))

    async def run():
        async with app.router.lifespan_context(app):
            assert app.state.database is None
    asyncio.run(run())
