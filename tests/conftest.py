"""离线采集器使用临时合成凭据；正式历史归档始终只读。"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import os

import pytest


@pytest.fixture
def bind_current_sources():
    """合成向量/假模型测试绑定当前源码；不替正式旧运行重签或放宽源码门禁。"""
    root = Path(__file__).resolve().parents[1]

    def bind(receipt):
        updated = deepcopy(receipt)
        for name in updated["source_sha256"]:
            path = root / name
            assert path.resolve().is_relative_to(root)
            updated["source_sha256"][name] = sha256(path.read_bytes()).hexdigest()
        return updated

    return bind


@pytest.fixture
def postgres_database():
    """Real PostgreSQL only, explicitly enabled; each test owns a newly created empty DB."""
    if os.environ.get("PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("真实PostgreSQL测试需显式启用；不使用SQLite替代")
    from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database
    from project_copilot.db.session import Database

    url = os.environ.get("PROJECT_COPILOT_TEST_DATABASE_URL", TEST_DATABASE_URL)
    with owned_test_database(url) as isolated_url:
        database = Database(isolated_url)
        try:
            yield database
        finally:
            database.close()


@pytest.fixture
def ticket_database(postgres_database):
    from project_copilot.db.migrations import upgrade

    upgrade(postgres_database)
    return postgres_database
