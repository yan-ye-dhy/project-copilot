"""Run a local durable worker. Running jobs are never automatically retried."""
import argparse
import logging
import signal
import threading
import time

# Container PID 1 must handle termination even during the heavier imports.
_STOPPING = threading.Event()
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: _STOPPING.set())
    signal.signal(signal.SIGINT, lambda *_: _STOPPING.set())

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.services.ai_worker import expire_unclaimed, process_one


def main():
    parser = argparse.ArgumentParser(description="Project Copilot AI job worker")
    parser.add_argument("--once", action="store_true", help="process at most one job")
    args = parser.parse_args()
    if _STOPPING.is_set():
        return
    settings = load_settings()
    if not (settings.public_demo or settings.environment == "production") or settings.database_url is None:
        parser.error("worker requires configured public demo mode and database")
    database = Database(settings.database_url.get_secret_value(), connect_timeout=settings.database_connect_timeout_seconds,
                        pool_timeout=settings.database_pool_timeout_seconds)
    stopping = _STOPPING
    # PID 1 in a container must explicitly handle termination. Finish the
    # current job before exiting; forced termination remains an unknown outcome.
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    next_cleanup = 0.0
    try:
        while not stopping.is_set():
            try:
                if time.monotonic() >= next_cleanup:
                    expire_unclaimed(database, ttl_seconds=settings.public_job_queue_ttl_seconds)
                    next_cleanup = time.monotonic() + 60
                worked = process_one(database, settings)
            except Exception as exc:
                # Never log job content, credentials, DSNs, provider errors or response bodies.
                logging.error("worker failure type=%s", type(exc).__name__)
                raise SystemExit(1) from None
            if args.once:
                return
            if not worked:
                stopping.wait(1)
    finally:
        from project_copilot.infrastructure.redis_runtime import close_redis_runtime
        try:
            close_redis_runtime(settings)
        finally:
            database.close()


if __name__ == "__main__":
    main()
