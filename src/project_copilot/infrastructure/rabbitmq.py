"""Optional RabbitMQ transport; PostgreSQL owns execution and replay decisions.

All Pika operations stay in the calling I/O thread. A single business thread
may spend minutes running a job while the I/O thread keeps heartbeats moving.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import logging
from threading import Event, get_ident
import time

from project_copilot.services.messaging import JobMessage, parse_payload


class RabbitMQTransportError(RuntimeError):
    """Redacted transport failure, safe for CLI output and durable backoff."""

    def __init__(self):
        super().__init__("RabbitMQ transport is unavailable")


def _pika():
    import pika

    # Broker replies and adapter diagnostics can include server supplied text.
    # Keep library logs private; this boundary emits only fixed safe messages.
    logger = logging.getLogger("pika")
    logger.addHandler(logging.NullHandler()) if not logger.handlers else None
    logger.propagate = False
    logger.setLevel(logging.CRITICAL + 1)
    return pika


def _connect(settings):
    # Construction is lazy: the default PG Worker never imports Pika or opens
    # an AMQP socket. URL and underlying exception strings never leave here.
    connection = None
    try:
        pika = _pika()

        if not settings.message_queue_enabled or settings.rabbitmq_url is None:
            raise RabbitMQTransportError()
        parameters = pika.URLParameters(settings.rabbitmq_url.get_secret_value())
        parameters.connection_attempts = 1
        parameters.retry_delay = 0
        parameters.heartbeat = settings.rabbitmq_heartbeat_seconds
        parameters.socket_timeout = settings.rabbitmq_socket_timeout_seconds
        parameters.stack_timeout = (settings.rabbitmq_socket_timeout_seconds * 2
                                    + settings.rabbitmq_blocked_timeout_seconds)
        parameters.blocked_connection_timeout = settings.rabbitmq_blocked_timeout_seconds
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        dead_exchange = settings.rabbitmq_exchange + ".dead"
        dead_queue = settings.rabbitmq_queue + ".dead"
        dead_routing_key = settings.rabbitmq_routing_key + ".dead"
        channel.exchange_declare(exchange=dead_exchange, exchange_type="direct", durable=True)
        channel.queue_declare(queue=dead_queue, durable=True, arguments={
            "x-queue-type": "quorum", "x-quorum-initial-group-size": 1,
            "x-max-length": settings.rabbitmq_queue_max_length,
            "x-max-length-bytes": settings.rabbitmq_queue_max_length * 512,
            "x-overflow": "reject-publish",
        })
        channel.queue_bind(queue=dead_queue, exchange=dead_exchange, routing_key=dead_routing_key)
        channel.exchange_declare(exchange=settings.rabbitmq_exchange, exchange_type="direct", durable=True)
        channel.queue_declare(queue=settings.rabbitmq_queue, durable=True, arguments={
            "x-queue-type": "quorum",
            "x-quorum-initial-group-size": 1,
            "x-max-length": settings.rabbitmq_queue_max_length,
            "x-max-length-bytes": settings.rabbitmq_queue_max_length * 512,
            "x-overflow": "reject-publish",
            # Transport redelivery has its own finite budget. Exhausted and
            # malformed envelopes are retained for administrator inspection;
            # they never imply permission to retry a provider call.
            "x-delivery-limit": settings.rabbitmq_delivery_limit,
            "x-dead-letter-exchange": dead_exchange,
            "x-dead-letter-routing-key": dead_routing_key,
            "x-dead-letter-strategy": "at-least-once",
        })
        channel.queue_bind(queue=settings.rabbitmq_queue, exchange=settings.rabbitmq_exchange,
                           routing_key=settings.rabbitmq_routing_key)
        return connection, channel
    except Exception:
        _close(connection)
        raise RabbitMQTransportError() from None


def _close(connection):
    if connection is not None:
        try:
            if connection.is_open:
                connection.close()
        except Exception as exc:
            logging.warning("rabbitmq close failure type=%s", type(exc).__name__)


class RabbitPublisher:
    """One relay-thread connection; every send requires a routed broker confirm."""

    def __init__(self, settings):
        self.settings = settings
        self._connection = None
        self._channel = None
        self._owner_thread = None

    def publish(self, message: JobMessage) -> None:
        if self._owner_thread is not None and self._owner_thread != get_ident():
            raise RabbitMQTransportError() from None
        try:
            pika = _pika()

            body = message.to_payload()
            # Revalidate outbound envelopes as well as inbound deliveries.
            parsed = parse_payload(body)
            if self._connection is None or not self._connection.is_open:
                self._connection, self._channel = _connect(self.settings)
                self._owner_thread = get_ident()
                self._channel.confirm_delivery()
            # BlockingChannel raises UnroutableError on mandatory Basic.Return
            # and NackError on a failed confirm. An ACK alone is insufficient.
            result = self._channel.basic_publish(
                exchange=self.settings.rabbitmq_exchange,
                routing_key=self.settings.rabbitmq_routing_key,
                body=body, mandatory=True,
                properties=pika.BasicProperties(
                    content_type="application/json", content_encoding="utf-8",
                    delivery_mode=2, message_id=str(parsed.event_id), type="job.available.v1",
                ),
            )
            if result is False:
                raise RabbitMQTransportError()
        except Exception:
            self.close()
            raise RabbitMQTransportError() from None

    def close(self):
        _close(self._connection)
        self._connection = self._channel = self._owner_thread = None

    def pump(self):
        """Service heartbeats while the relay has no due outbox rows."""
        if self._connection is not None:
            if self._owner_thread != get_ident():
                raise RabbitMQTransportError() from None
            try:
                self._connection.process_data_events(time_limit=0)
            except Exception:
                # No publish is in flight. The next due row gets a fresh
                # connection and keeps the usual PG-backed delivery policy.
                self.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class RabbitConsumer:
    """Prefetch one; ACK only after the callback's durable PG commit.

    Callback failures close the connection with its delivery unacknowledged.
    There is no in-process model retry or immediate NACK/requeue loop.
    """

    def __init__(self, settings):
        self.settings = settings

    def run(self, callback, stopping, *, once=False, maintenance=None) -> int:
        if stopping.is_set():
            return 0
        connection, channel = _connect(self.settings)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="amqp-job")
        active = None
        maintenance_active = None
        next_maintenance = 0.0
        delivery_tag = None
        completed = 0
        draining = Event()
        skipped = object()

        def business(function, *args):
            # A delivery can queue behind one maintenance operation. Recheck
            # shutdown on its business thread before starting paid work.
            if stopping.is_set() or draining.is_set():
                return skipped
            try:
                return function(*args)
            except Exception:
                # Fence any queued delivery before the I/O thread observes a
                # maintenance failure and closes the unacknowledged channel.
                draining.set()
                raise

        def delivered(_channel, method, _properties, body):
            nonlocal active, delivery_tag
            if stopping.is_set() or draining.is_set():
                # A signal may arrive inside process_data_events. Leave this
                # delivery unacked and close rather than starting another job.
                return
            if active is not None:
                # A protocol/prefetch violation must fail closed.
                raise RabbitMQTransportError()
            try:
                message = parse_payload(body)
            except (ValueError, TypeError):
                # Invalid wire data cannot authorize a PG job or model call;
                # the broker retains it in the bounded durable dead queue.
                channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                logging.warning("rabbitmq rejected invalid envelope")
                return
            delivery_tag = method.delivery_tag
            active = executor.submit(copy_context().run, business, callback, message)

        try:
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(queue=self.settings.rabbitmq_queue,
                                  on_message_callback=delivered, auto_ack=False)
            while True:
                if maintenance_active is not None and maintenance_active.done():
                    maintenance_active.result()
                    maintenance_active = None
                    next_maintenance = time.monotonic() + 10
                if active is not None and active.done():
                    if active.result() is skipped:
                        return completed
                    channel.basic_ack(delivery_tag=delivery_tag)
                    completed += 1
                    active = delivery_tag = None
                    if once:
                        return completed
                if stopping.is_set() and active is None and maintenance_active is None:
                    return completed
                if (maintenance is not None and not stopping.is_set() and active is None
                        and maintenance_active is None and time.monotonic() >= next_maintenance):
                    # One shared executor permits one running callback and at
                    # most one queued prefetch delivery, never queued scans.
                    next_maintenance = time.monotonic() + 10
                    maintenance_active = executor.submit(copy_context().run, business, maintenance)
                # Long provider calls run in the business thread; the same I/O
                # thread continues heartbeats and detects channel cancellation.
                connection.process_data_events(time_limit=0.25)
                if not connection.is_open or not channel.is_open or not channel.consumer_tags:
                    raise RabbitMQTransportError()
        except Exception:
            raise RabbitMQTransportError() from None
        finally:
            draining.set()
            _close(connection)
            # A disconnect cannot cancel a provider call already in progress.
            # Finish its PG result before letting the process create a new
            # consumer, whose duplicate delivery will consult PG first.
            executor.shutdown(wait=True, cancel_futures=True)
