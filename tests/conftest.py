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


@pytest.fixture(autouse=True)
def _controlled_worker_release(request, monkeypatch):
    """Legacy runner-contract tests use synthetic admission, never a live release.

    Their provider and health boundaries are controlled separately; production
    defaults remain CLOSED and dedicated release tests exercise the actual gate.
    """
    controlled_modules = {"test_public_jobs.py", "test_ai_jobs_security_postgres.py",
                          "test_public_upload_real_services.py", "test_upload_worker_security.py",
                          "test_public_upload_jobs.py", "test_ai_attempt_audit_postgres.py"}
    if request.node.path.name not in controlled_modules:
        return
    from datetime import datetime, timedelta, timezone
    from project_copilot.db.release import ReleaseControl
    from project_copilot.services import ai_worker, release_control
    # These older contract tests vary controlled model/storage settings. Their
    # synthetic release must not be mistaken for a configuration-drift test;
    # dedicated release tests exercise the actual fingerprint function.
    monkeypatch.setattr(release_control, "config_fingerprint", lambda _: "synthetic-contract-configuration")
    database = request.getfixturevalue("ticket_database")
    with database.transaction() as session:
        row = session.get(ReleaseControl, 1)
        row.state, row.worker_enabled = "PUBLIC", True
        row.resource_id, row.origin = "public-demo-v2", "https://demo.example"
        row.code_fingerprint = release_control.code_fingerprint()
        row.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        row.evidence = {"checks": {key: "a" * 64 for key in release_control._PUBLIC},
                        "configuration_fingerprint": "synthetic-contract-configuration"}
    monkeypatch.setattr(release_control, "dependencies_ready", lambda *_: None)
    monkeypatch.setattr(ai_worker, "dependencies_ready", lambda *_: None)
