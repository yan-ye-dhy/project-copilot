"""Only creates/drops databases it just created in the separate test cluster."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


TEST_DATABASE_URL = (
    "postgresql+psycopg://project_copilot_test:local-test-only@127.0.0.1:5434/project_copilot_test"
)


@contextmanager
def owned_test_database(admin_url: str) -> Iterator[str]:
    """Require the test-only database/role before connecting; never drop existing databases."""
    url = make_url(admin_url)
    if (url.drivername != "postgresql+psycopg" or url.database != "project_copilot_test"
            or url.username != "project_copilot_test"
            or url.host not in ("127.0.0.1", "localhost", "postgres-test")
            or url.port not in (5434, 5432) or url.query):
        raise ValueError("集成验收只允许独立project_copilot_test集群及测试角色")
    name = "pc_test_d20_" + uuid4().hex
    dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            yield url.set(database=name).render_as_string(hide_password=False)
        finally:
            # Only this just-created random database; no CASCADE, FORCE or volume cleanup.
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
