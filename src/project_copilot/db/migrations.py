"""迁移必须显式执行；连接注入避免把密钥写入alembic.ini或日志。"""

from alembic import command
from alembic.config import Config
from io import StringIO

from project_copilot.config import PROJECT_ROOT
from project_copilot.db.session import Database


def migration_config() -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return config


def upgrade(database: Database) -> None:
    with database.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


def check_model_drift(database: Database) -> None:
    with database.engine.connect() as connection:
        config = migration_config()
        config.stdout = StringIO()  # Keep callers' JSON stdout clean; actual errors still raise.
        config.attributes["connection"] = connection
        command.check(config)
