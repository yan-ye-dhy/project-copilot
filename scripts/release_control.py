"""Interactive, audited release operations. Default state is CLOSED."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from sqlalchemy import select

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.db.models import UserAccount
from project_copilot.services.auth import AuthService
from project_copilot.services.release_control import change_state, code_fingerprint, config_fingerprint, release_status, personal_settings
from project_copilot.services.personal_profile import PERSONAL_MAX_MINUTES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "CLOSED", "STAGING", "PUBLIC", "PERSONAL_DEMO", "worker-enable", "worker-disable"))
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--user", help="existing active test username (PERSONAL_DEMO only)")
    parser.add_argument("--minutes", type=int, default=PERSONAL_MAX_MINUTES, help="PERSONAL_DEMO lifetime, 1-1440 minutes; no automatic renewal")
    args = parser.parse_args()
    if args.operation == "PERSONAL_DEMO":
        if not args.user or args.evidence is not None or not 1 <= args.minutes <= PERSONAL_MAX_MINUTES:
            parser.error("PERSONAL_DEMO requires --user, 1-1440 minutes and no evidence file")
    elif args.user is not None or args.minutes != PERSONAL_MAX_MINUTES:
        parser.error("--user and --minutes are PERSONAL_DEMO options")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive administrator terminal required")
    database = None
    try:
        settings = load_settings()
        if settings.database_url is None or not (settings.public_demo or settings.environment == "production"):
            raise ValueError("configured public database required")
        if args.operation == "PERSONAL_DEMO":
            personal_settings(settings)  # Refuse a non-demo DSN before connecting.
        username = input("Administrator username: ").strip().lower()
        password = getpass.getpass("Administrator password: ")
        database = Database(settings.database_url.get_secret_value())
        # Finish credential verification before prompting or waiting for the
        # admission lock. change_state revalidates the current active role.
        with database.transaction() as session:
            admin = AuthService(session)._verify_admin(username, password)
            actor_id = admin.id
        if args.operation == "inspect":
            with database.transaction() as session:
                result = release_status(session)
                result["current_code_fingerprint"] = code_fingerprint()
                result["current_configuration_fingerprint"] = config_fingerprint(settings)
                from project_copilot.services.entry_protection import is_default_domain, source_fingerprint
                if is_default_domain(str(settings.auth_public_origin)):
                    result["entry_source_sha256"] = source_fingerprint()
                    result["default_domain_public_supported"] = False
        else:
            phrase = input(f"Type AUTHORIZE {args.operation} to record your explicit authorization: ")
            if phrase != f"AUTHORIZE {args.operation}":
                raise ValueError("authorization not supplied")
            reason = input("Reason / investigation reference (no credentials): ").strip()
            evidence = None
            if args.operation in {"STAGING", "PUBLIC"}:
                if args.evidence is None or args.evidence.is_symlink() or args.evidence.stat().st_size > 64 * 1024:
                    raise ValueError("bounded local evidence file required")
                evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            with database.transaction() as session:
                personal = {}
                if args.operation == "PERSONAL_DEMO":
                    user_id = session.scalar(select(UserAccount.id).where(
                        UserAccount.username == args.user.strip().lower(), UserAccount.active.is_(True)))
                    if user_id is None:
                        raise ValueError("existing active test user required")
                    personal = {"personal_user_id": user_id, "duration_minutes": args.minutes}
                result = change_state(session, settings, actor_id=actor_id, operation=args.operation,
                                      reason=reason, evidence=evidence, **personal)
        print(json.dumps(result))
        return 0
    except (Exception, KeyboardInterrupt):
        print("Release operation failed; inspect sanitized local diagnostics and configuration.", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
