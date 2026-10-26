"""D20数据库管理：显式迁移、只读连接检查、受控回滚；不提供删库命令。"""

import argparse
import json
import sys

from alembic.util import CommandError
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.config import load_settings
from project_copilot.db.migrations import upgrade
from project_copilot.db.session import Database
from project_copilot.services.ticket_checks import verify_rollback


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "upgrade", "rollback-check"))
    args = parser.parse_args()
    database = None
    try:
        settings = load_settings()
        if settings.database_url is None:
            raise ValueError("没有工单数据库配置")
        database = Database(settings.database_url.get_secret_value(),
                            connect_timeout=settings.database_connect_timeout_seconds,
                            pool_timeout=settings.database_pool_timeout_seconds)
        if args.action == "upgrade":
            upgrade(database)
            result = {"status": "ok", "migration": "head"}
        elif args.action == "rollback-check":
            result = verify_rollback(database)
        else:
            with database.transaction() as session:
                result = {"status": "ok", "database": session.scalar(text("SELECT current_database()")),
                          "postgresql_version": session.scalar(text("SHOW server_version"))}
    except (CommandError, ValidationError, SQLAlchemyError, ValueError, RuntimeError):
        print(json.dumps({"status": "error", "message": "数据库操作失败，请检查新项目配置、PostgreSQL就绪及迁移状态"},
                         ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
