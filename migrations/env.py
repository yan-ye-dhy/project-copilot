"""显式迁移入口；CLI读取新项目配置，程序调用可注入独立连接。"""

from alembic import context

from project_copilot.config import load_settings
from project_copilot.db.models import Base
from project_copilot.db.session import Database


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata,
                      compare_type=True, compare_server_default=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    # Produces PostgreSQL DDL without reading .env or connecting to a server.
    context.configure(dialect_name="postgresql", target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        run(connection)
    else:
        settings = load_settings()
        if settings.database_url is None:
            raise ValueError("请配置新项目PROJECT_COPILOT_DATABASE_URL后再执行迁移")
        database = Database(settings.database_url.get_secret_value(),
                            connect_timeout=settings.database_connect_timeout_seconds,
                            pool_timeout=settings.database_pool_timeout_seconds)
        try:
            with database.engine.begin() as connection:
                run(connection)
        finally:
            database.close()
