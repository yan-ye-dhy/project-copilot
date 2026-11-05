"""Interactive, audited release operations. Default state is CLOSED."""
import argparse
import getpass
import json
from pathlib import Path
import sys

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.services.auth import AuthService
from project_copilot.services.release_control import change_state, code_fingerprint, config_fingerprint, release_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("inspect", "CLOSED", "STAGING", "PUBLIC", "worker-enable", "worker-disable"))
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("interactive administrator terminal required")
    database = None
    try:
        settings = load_settings()
        if settings.database_url is None or not (settings.public_demo or settings.environment == "production"):
            raise ValueError("configured public database required")
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
                result = change_state(session, settings, actor_id=actor_id, operation=args.operation,
                                      reason=reason, evidence=evidence)
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
