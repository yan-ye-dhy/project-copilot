"""Publish committed dispatch envelopes; no job payloads or model execution."""
import argparse
import logging
import signal
import threading

_STOPPING = threading.Event()
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: _STOPPING.set())
    signal.signal(signal.SIGINT, lambda *_: _STOPPING.set())

from project_copilot.config import load_settings
from project_copilot.db.session import Database
from project_copilot.infrastructure.rabbitmq import RabbitPublisher
from project_copilot.services.messaging import relay_once


def main(argv=None):
    parser = argparse.ArgumentParser(description="Project Copilot transactional outbox relay")
    parser.add_argument("--once", action="store_true", help="attempt one bounded batch and exit")
    args = parser.parse_args(argv)
    if _STOPPING.is_set():
        return 0
    settings = load_settings()
    if not settings.message_queue_enabled or settings.database_url is None:
        parser.error("relay requires explicit message queue enablement and database configuration")
    database = Database(settings.database_url.get_secret_value(),
                        connect_timeout=settings.database_connect_timeout_seconds,
                        pool_timeout=settings.database_pool_timeout_seconds,
                        pool_size=settings.database_pool_size,
                        max_overflow=settings.database_max_overflow)
    signal.signal(signal.SIGTERM, lambda *_: _STOPPING.set())
    signal.signal(signal.SIGINT, lambda *_: _STOPPING.set())
    try:
        with RabbitPublisher(settings) as publisher:
            while not _STOPPING.is_set():
                try:
                    attempted = 0
                    for _ in range(settings.outbox_relay_batch_size):
                        if _STOPPING.is_set():
                            break
                        worked = relay_once(database, publisher,
                            lease_seconds=settings.outbox_lease_seconds,
                            retry_base_seconds=settings.outbox_retry_base_seconds,
                            retry_max_seconds=settings.outbox_retry_max_seconds)
                        publisher.pump()
                        if not worked:
                            break
                        attempted += 1
                except Exception as exc:
                    logging.error("outbox relay failure type=%s", type(exc).__name__)
                    return 1
                if args.once:
                    return 0
                if not attempted:
                    _STOPPING.wait(1)
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
