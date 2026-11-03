"""Non-interactive read-only probes; cannot authorize state transitions."""
import argparse

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.services.release_control import require_open


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("worker-ready",))
    parser.parse_args()
    database = None
    try:
        settings = load_settings()
        if settings.database_url is None or not (settings.public_demo or settings.environment == "production"):
            raise ValueError("public settings required")
        database = Database(settings.database_url.get_secret_value())
        with database.transaction() as session:
            require_open(session, settings, worker=True)
        return 0
    except Exception:
        print("Public worker admission is closed or unavailable.")
        return 1
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
