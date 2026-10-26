"""D20真实PG＋Uvicorn验收；仅使用独立测试集群和本次创建的随机数据库。"""

import argparse
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from sqlalchemy import inspect, text

from project_copilot.config import PROJECT_ROOT, Settings
from project_copilot.db.migrations import check_model_drift, upgrade
from project_copilot.db.models import Ticket
from project_copilot.db.session import Database
from project_copilot.main import create_app
from project_copilot.schemas.tickets import TicketRead
from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database


SOURCE_PATHS = (
    "src/project_copilot/config.py", "src/project_copilot/main.py", "src/project_copilot/db/session.py",
    "src/project_copilot/db/models.py", "src/project_copilot/db/migrations.py",
    "src/project_copilot/schemas/tickets.py", "src/project_copilot/services/tickets.py",
    "src/project_copilot/services/ticket_checks.py", "src/project_copilot/api/tickets.py",
    "migrations/env.py", "migrations/versions/0001_create_tickets.py", "alembic.ini",
    "scripts/postgres_support.py", "scripts/ticket_db.py", "scripts/postgres_smoke.py", "uv.lock",
)


def app_factory():
    """Child Uvicorn uses the actual app with explicit settings, never the real .env."""
    return create_app(Settings(_env_file=None, environment="test",
                               database_url=os.environ["PROJECT_COPILOT_DATABASE_URL"],
                               zhipu_api_key=None, deepseek_api_key=None))


def clean_environment(url):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PROJECT_COPILOT_")}
    env.update(PROJECT_COPILOT_DATABASE_URL=url, PROJECT_COPILOT_ENVIRONMENT="test", HF_HUB_OFFLINE="1")
    return env


@contextmanager
def api_process(url, logfile):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with logfile.open("w", encoding="utf-8") as log:
        process = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "scripts.postgres_smoke:app_factory", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
        ], cwd=PROJECT_ROOT, env=clean_environment(url), stdout=log, stderr=subprocess.STDOUT)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("临时API启动失败，检查本次独立输出目录")
                    try:
                        health = client.get("/health")
                        if health.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.05)
                yield client, process.pid, health
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()  # Only the process just spawned by this context.
                    process.wait(timeout=5)


def run(output):
    if output.exists():
        raise ValueError("输出目录已存在，拒绝覆盖")
    output.mkdir(parents=True)
    admin_url = os.environ.get("PROJECT_COPILOT_TEST_DATABASE_URL", TEST_DATABASE_URL)
    report = {
        "status": "running", "executor": "Codex/CLI user", "mode": "real-postgresql-and-loopback-http",
        "code": {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True).strip(),
                 "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, text=True))},
        "source_sha256": {p: sha256((PROJECT_ROOT / p).read_bytes()).hexdigest() for p in SOURCE_PATHS},
        "dependencies": {p: version(p) for p in ("sqlalchemy", "psycopg", "alembic", "fastapi")},
        "model_api_calls": 0, "qdrant_calls": 0, "http": [], "test_database_deleted": False,
    }

    def save():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    def record(name, response, expected):
        assert response.status_code == expected, name
        assert response.headers.get("x-run-id"), name
        report["http"].append({"name": name, "status": response.status_code,
                               "run_id": response.headers["x-run-id"], "body": response.json()})
        return response.json()

    try:
        with owned_test_database(admin_url) as url:
            database = Database(url)
            try:
                report["isolated_database"] = database.engine.url.database
                assert inspect(database.engine).get_table_names() == []
                upgrade(database)
                upgrade(database)
                check_model_drift(database)
                report["migration"] = {"empty_database": True, "revision": "0001_tickets",
                                       "second_upgrade_ok": True, "model_drift": False}
                with database.transaction() as session:
                    report["postgresql_version"] = session.scalar(text("SHOW server_version"))
                payload = {"title": "D20自编中文上传问题", "description": "真实PG验收：HTTP422错误需要排查。"}
                with api_process(url, output / "api-first.log") as (client, first_pid, health):
                    record("first-health", health, 200)
                    created = record("create", client.post("/tickets", json=payload), 201)
                    parsed = TicketRead.model_validate(created)
                    ticket_id = parsed.id
                    report["ticket_id"] = str(ticket_id)
                    with database.transaction() as session:
                        assert TicketRead.model_validate(session.get(Ticket, ticket_id)).model_dump(mode="json") == created
                    record("detail", client.get(f"/tickets/{ticket_id}"), 200)
                    listing = record("filtered-list", client.get("/tickets", params={"status": "open", "q": "中文"}), 200)
                    assert listing["items"] == [created]
                    record("invalid-input", client.post("/tickets", json={"title": " ", "description": "无效输入"}), 422)
                    record("missing-ticket", client.get(f"/tickets/{uuid4()}"), 404)
                    rollback = subprocess.run([sys.executable, "-m", "scripts.ticket_db", "rollback-check"],
                                              cwd=PROJECT_ROOT, env=clean_environment(url), capture_output=True, text=True)
                    (output / "rollback.stdout.json").write_text(rollback.stdout)
                    (output / "rollback.stderr.txt").write_text(rollback.stderr)
                    assert rollback.returncode == 0
                    report["rollback"] = json.loads(rollback.stdout)
                    assert report["rollback"]["flushed_before_failure"]
                    assert report["rollback"]["visible_after_rollback"] is False
                    with database.transaction() as session:
                        assert session.scalar(text("SELECT count(*) FROM tickets")) == 1
                    report["first_api_pid"] = first_pid
                with api_process(url, output / "api-restarted.log") as (client, second_pid, health):
                    record("restarted-health", health, 200)
                    persisted = record("persisted-detail", client.get(f"/tickets/{ticket_id}"), 200)
                    assert first_pid != second_pid and persisted == created
                    report["restarted_api_pid"] = second_pid
                    report["persisted_after_api_process_restart"] = True
                with database.transaction() as session:
                    report["committed_rows_before_cleanup"] = session.scalar(text("SELECT count(*) FROM tickets"))
                    assert report["committed_rows_before_cleanup"] == 1
                report["pool_checked_out_after"] = database.engine.pool.checkedout()
                assert report["pool_checked_out_after"] == 0
                report["owned_api_processes_stopped"] = True
            finally:
                database.close()
        report["test_database_deleted"] = True  # The owned context's exact DROP completed.
        report["status"] = "ok"
    except Exception:
        report["status"] = "failed"
        save()
        raise
    save()
    return {"status": report["status"], "postgresql_version": report["postgresql_version"],
            "http_requests": len(report["http"]), "persisted_after_api_process_restart": True,
            "test_database_deleted": report["test_database_deleted"], "model_api_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if not args.local:
        parser.error("真实PostgreSQL验收需显式添加--local，只操作新建测试库，不调用模型")
    try:
        result = run(args.output_dir)
    except Exception:
        print(json.dumps({"status": "error", "message": "PG验收失败，检查独立输出的报告和服务日志"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
