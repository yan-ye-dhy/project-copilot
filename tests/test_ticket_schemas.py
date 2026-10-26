"""阶段B离线契约/DDL；实际约束另由真实PostgreSQL测试验证。"""

import subprocess
import ast
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from project_copilot.db.models import Ticket
from project_copilot.schemas.tickets import TicketCreate, TicketFilters, TicketRead, TicketStatus


@pytest.mark.parametrize("change", [
    {"title": ""}, {"title": " \t\n"}, {"title": "文" * 201}, {"title": 12},
    {"description": ""}, {"description": " "}, {"description": "文" * 10001},
    {"description": True}, {"id": str(uuid4())}, {"status": "closed"},
    {"title": "text\x00nul"}, {"description": "text\x00nul"}, {"title": "\ud800"},
])
def test_create_rejects_bad_fields_and_client_generated_database_fields(change):
    with pytest.raises(ValidationError):
        TicketCreate.model_validate({"title": "标题", "description": "描述", **change})


def test_create_strips_input_and_keeps_response_orm_separate():
    payload = TicketCreate(title=" 标题 ", description=" 描述 ")
    assert payload.model_dump() == {"title": "标题", "description": "描述"}
    entity = Ticket(**payload.model_dump())
    assert entity.id is None and entity.created_at is None and entity.status is None
    with pytest.raises(ValidationError):
        TicketRead.model_validate(entity)  # Server defaults exist only after a real flush.
    now = datetime.now(timezone.utc)
    entity.id, entity.status, entity.created_at, entity.updated_at = uuid4(), "open", now, now
    result = TicketRead.model_validate(entity)
    assert result.id == entity.id and result.status is TicketStatus.OPEN


@pytest.mark.parametrize("values", [
    {"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": 10001}, {"status": "invalid"},
    {"q": " "}, {"q": "x" * 201}, {"q": "\x00"}, {"sort": "unsupported"},
])
def test_filter_bounds(values):
    with pytest.raises(ValidationError):
        TicketFilters(**values)


def test_postgres_ddl_has_database_generated_uuid_timestamps_and_constraints():
    sql = str(CreateTable(Ticket.__table__).compile(dialect=postgresql.dialect()))
    assert "UUID DEFAULT gen_random_uuid()" in sql
    assert "TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in sql
    assert "VARCHAR(200) NOT NULL" in sql
    assert all(constraint in sql for constraint in (
        "ck_tickets_title_not_blank", "ck_tickets_description_not_blank",
        "ck_tickets_description_length", "ck_tickets_status",
    ))


def test_alembic_offline_ddl_works_without_database_or_env():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([str(root / ".venv/bin/alembic"), "-c", "alembic.ini", "upgrade", "head", "--sql"],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE tickets" in result.stdout and "0001_tickets" in result.stdout
    assert "DROP" not in result.stdout


def test_alembic_can_generate_a_followup_revision_in_an_isolated_copy(tmp_path):
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    target = tmp_path / "migrations"
    shutil.copytree(root / "migrations", target)
    config = Config()
    config.set_main_option("script_location", str(target))
    generated = command.revision(config, message="local template probe", rev_id="local_probe")
    content = Path(generated.path).read_text()
    ast.parse(content)
    assert generated.down_revision == "0001_tickets"
    assert "local template probe" in content
