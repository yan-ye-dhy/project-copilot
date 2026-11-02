"""Interactive initial admin creation or one-time invitation issuance; never accepts a password argument."""
import argparse
import getpass
import sys

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService, RegistrationRejected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--create-invite", action="store_true", help="使用现有管理员凭据生成单次邀请码")
    operation.add_argument("--set-role", choices=("user", "admin"), help="使用现有管理员凭据调整账号角色")
    args = parser.parse_args()
    # Avoid redirected logs or non-interactive credential input/one-time token output.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("需要交互式终端；密码不能通过参数或管道传入。", file=sys.stderr)
        return 2
    database = None
    try:
        username = input("管理员账号：").strip()
        password = getpass.getpass("管理员密码：")
        if args.create_invite or args.set_role:
            credentials = LoginInput(username=username, password=password)
            target = input("目标账号：").strip().lower() if args.set_role else None
        else:
            confirmation = getpass.getpass("再次输入密码：")
            if password != confirmation:
                raise ValueError("password mismatch")
            credentials = RegisterInput(username=username, password=password)
        settings = load_settings()
        if settings.database_url is None:
            raise ValueError("database missing")
        database = Database(settings.database_url.get_secret_value(),
                            connect_timeout=settings.database_connect_timeout_seconds,
                            pool_timeout=settings.database_pool_timeout_seconds)
        with database.transaction() as session:
            service = AuthService(session)
            if args.create_invite:
                result = service.issue_invitation(credentials.username, credentials.password.get_secret_value())
            elif args.set_role:
                result = service.set_role(credentials.username, credentials.password.get_secret_value(), target, args.set_role)
            else:
                result = service.bootstrap_admin(credentials)
        if args.create_invite:
            print("单次邀请码（只显示一次，有效期24小时）：", result)
        elif args.set_role:
            print("账号角色已更新：", result.username, result.role)
        else:
            print("管理员创建成功：", result.username)
        return 0
    except (EOFError, KeyboardInterrupt, ValidationError, SQLAlchemyError, RegistrationRejected, ValueError):
        print("操作失败，请检查账号、密码、迁移和数据库配置。", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
