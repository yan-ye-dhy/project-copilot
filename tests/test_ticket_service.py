"""阶段C本地SQL编译检查；不将编译/Mock结果当真实数据库验收。"""

import pytest
from sqlalchemy.dialects import postgresql

from scripts.postgres_support import owned_test_database
from project_copilot.schemas.tickets import TicketFilters
from project_copilot.services.tickets import ticket_query


def test_untrusted_keyword_is_bound_and_wildcards_are_literal():
    phrase = "%' OR 1=1; DROP TABLE tickets; --_"
    compiled = ticket_query(TicketFilters(q=phrase, status="open", limit=3, offset=2)).compile(
        dialect=postgresql.dialect(),
    )
    sql = str(compiled)
    assert phrase not in sql and "DROP TABLE" not in sql
    assert "ILIKE" in sql and "ESCAPE '/'" in sql
    assert "ORDER BY tickets.created_at DESC, tickets.id DESC" in sql
    assert compiled.params["status_1"] == "open"
    assert compiled.params["param_1"] == 3 and compiled.params["param_2"] == 2
    assert any(v == phrase.replace("%", "/%").replace("_", "/_") for v in compiled.params.values())


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://project_copilot:secret@127.0.0.1:5433/project_copilot",
    "postgresql+psycopg://project_copilot_test:secret@remote.example:5434/project_copilot_test",
    "postgresql+psycopg://project_copilot_test:secret@localhost:5434/not_test",
])
def test_test_database_guard_rejects_other_databases_before_connect(monkeypatch, url):
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: pytest.fail("不得连接非测试目标"))
    with pytest.raises(ValueError, match="测试"):
        with owned_test_database(url):
            pytest.fail("不得创建或清理非测试数据库")
