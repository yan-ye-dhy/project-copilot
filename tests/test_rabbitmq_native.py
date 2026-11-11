"""Opt-in real RabbitMQ tests; use only a dedicated loopback synthetic broker."""
import os
from threading import Event
import time
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import SecretStr
import pytest

from project_copilot.config import Settings
from project_copilot.infrastructure.rabbitmq import RabbitConsumer, RabbitPublisher, RabbitMQTransportError, _connect
from project_copilot.services.messaging import JobMessage, parse_payload


@pytest.fixture
def broker():
    url = os.environ.get("PROJECT_COPILOT_TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("dedicated loopback RabbitMQ URL not supplied")
    parsed = urlsplit(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 15672:
        pytest.fail("real messaging tests require dedicated loopback port 15672")
    name = "pc.test." + uuid4().hex
    settings = Settings(_env_file=None, environment="test", database_url=SecretStr(
        "postgresql+psycopg://synthetic:unused@127.0.0.1:5434/project_copilot_test"),
        message_queue_enabled=True, rabbitmq_url=SecretStr(url), rabbitmq_exchange=name,
        rabbitmq_queue=name, rabbitmq_queue_max_length=100, rabbitmq_heartbeat_seconds=5,
        rabbitmq_delivery_limit=3)
    try:
        yield settings
    finally:
        connection, channel = _connect(settings)
        try:
            channel.queue_delete(queue=name)
            channel.queue_delete(queue=name + ".dead")
            channel.exchange_delete(exchange=name)
            channel.exchange_delete(exchange=name + ".dead")
        finally:
            connection.close()


def get_message(channel, queue):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = channel.basic_get(queue, auto_ack=False)
        if result[0] is not None:
            return result
        time.sleep(0.025)
    pytest.fail("confirmed message was not delivered")


def test_real_confirmed_persistent_envelope_redelivers_after_lost_uncommitted_ack(broker):
    message = JobMessage(uuid4(), uuid4())
    with RabbitPublisher(broker) as publisher:
        publisher.publish(message)
    connection, channel = _connect(broker)
    method, props, body = get_message(channel, broker.rabbitmq_queue)
    assert parse_payload(body) == message and props.delivery_mode == 2
    connection.close()  # No ACK: simulate a disconnect before durable decision.
    connection, channel = _connect(broker)
    try:
        method, _, body = get_message(channel, broker.rabbitmq_queue)
        assert method.redelivered and parse_payload(body) == message
        channel.basic_ack(method.delivery_tag)
    finally:
        connection.close()


def test_real_mandatory_return_is_not_accepted_as_a_successful_confirm(broker):
    with RabbitPublisher(broker) as publisher:
        publisher.publish(JobMessage(uuid4(), uuid4()))
        publisher._channel.queue_unbind(queue=broker.rabbitmq_queue,
            exchange=broker.rabbitmq_exchange, routing_key=broker.rabbitmq_routing_key)
        with pytest.raises(RabbitMQTransportError):
            publisher.publish(JobMessage(uuid4(), uuid4()))


def test_real_queue_overflow_rejects_publisher_without_dropping_oldest(broker):
    broker.rabbitmq_queue_max_length = 1
    first = JobMessage(uuid4(), uuid4())
    rejected = False
    with RabbitPublisher(broker) as publisher:
        publisher.publish(first)
        # Quorum reject-publish permits bounded overshoot while notifications
        # reach publishers, so test rejection and preservation, not exact size.
        for _ in range(20):
            try:
                publisher.publish(JobMessage(uuid4(), uuid4()))
            except RabbitMQTransportError:
                rejected = True
                break
    assert rejected
    connection, channel = _connect(broker)
    try:
        method, _, body = get_message(channel, broker.rabbitmq_queue)
        assert parse_payload(body) == first
        channel.basic_ack(method.delivery_tag)
    finally:
        connection.close()


def test_real_long_callback_keeps_heartbeat_alive_and_acks_only_after_commit(broker):
    message = JobMessage(uuid4(), uuid4())
    with RabbitPublisher(broker) as publisher:
        publisher.publish(message)
    seen = []
    def process(received):
        assert received == message
        time.sleep(12)  # Exceeds two negotiated five-second heartbeat intervals.
        seen.append("durable-decision")
    assert RabbitConsumer(broker).run(process, Event(), once=True) == 1
    assert seen == ["durable-decision"]
    connection, channel = _connect(broker)
    try:
        assert channel.basic_get(broker.rabbitmq_queue, auto_ack=False)[0] is None
    finally:
        connection.close()


def test_real_malformed_envelope_is_retained_in_durable_dead_letter_queue(broker):
    connection, channel = _connect(broker)
    body = b'{"unsupported":"synthetic-poison"}'
    channel.confirm_delivery()
    channel.basic_publish(exchange=broker.rabbitmq_exchange, routing_key=broker.rabbitmq_routing_key,
                          body=body, mandatory=True)
    connection.close()
    stopping = Event()
    from threading import Timer
    timer = Timer(0.5, stopping.set)
    timer.start()
    try:
        assert RabbitConsumer(broker).run(lambda _: pytest.fail("invalid business callback"), stopping) == 0
    finally:
        timer.cancel()
    connection, channel = _connect(broker)
    try:
        method, props, retained = get_message(channel, broker.rabbitmq_queue + ".dead")
        assert retained == body
        assert props.headers["x-first-death-reason"] == "rejected"
        channel.basic_ack(method.delivery_tag)
    finally:
        connection.close()


def test_real_finite_broker_delivery_limit_routes_to_dead_letter_queue(broker):
    message = JobMessage(uuid4(), uuid4())
    with RabbitPublisher(broker) as publisher:
        publisher.publish(message)
    connection, channel = _connect(broker)
    try:
        for _ in range(broker.rabbitmq_delivery_limit + 1):
            method, _, _ = get_message(channel, broker.rabbitmq_queue)
            channel.basic_reject(method.delivery_tag, requeue=True)
        method, props, body = get_message(channel, broker.rabbitmq_queue + ".dead")
        assert parse_payload(body) == message
        assert props.headers["x-first-death-reason"] == "delivery_limit"
        channel.basic_ack(method.delivery_tag)
    finally:
        connection.close()


def test_real_outbox_pg_commit_lost_ack_and_redelivery_execute_business_once(broker, ticket_database):
    """Real PG + real broker; business work is a controlled local surrogate."""
    from project_copilot.db.jobs import AiJob
    from project_copilot.db.messaging import AiJobInbox, AiJobOutbox
    from project_copilot.db.models import AiRequestReservation
    from project_copilot.services.messaging import begin_delivery, complete_delivery, enqueue_outbox, relay_batch
    from project_copilot.services.quotas import QuotaService
    from scripts.week1_queue_lab import seed_identity

    database = ticket_database
    user_id, login_id = seed_identity(database)
    with database.transaction() as session:
        reservation, reused = QuotaService(session, settings=broker).reserve(
            user_id, "native-outbox-" + uuid4().hex, "a" * 64, "rag")
        assert not reused
        session.add(AiJob(id=reservation.id, login_session_id=login_id,
                          payload={"question": "private synthetic business payload"}))
        message = enqueue_outbox(session, reservation.id)
    with RabbitPublisher(broker) as publisher:
        assert relay_batch(database, publisher) == 1
    with database.transaction() as session:
        assert session.get(AiJobOutbox, message.event_id).sent_at is not None

    attempts = []
    def committed_then_disconnect(received):
        claim = begin_delivery(database, received)
        assert claim.decision == "ready"
        with database.transaction() as session:
            quota = QuotaService(session)
            assert quota.claim(user_id, received.job_id)
            attempts.append(received.job_id)
            session.get(AiJob, received.job_id).result = {"answer": "synthetic durable result"}
            quota.finish(user_id, received.job_id, outcome="succeeded")
        # Simulate the process/connection failing after result COMMIT and
        # before inbox completion/ACK, the expensive duplication window.
        raise RuntimeError("controlled lost ACK after durable PG result")
    with pytest.raises(RabbitMQTransportError):
        RabbitConsumer(broker).run(committed_then_disconnect, Event(), once=True)

    def duplicate(received):
        claim = begin_delivery(database, received)
        assert claim.decision == "completed"
        assert complete_delivery(database, claim)
    assert RabbitConsumer(broker).run(duplicate, Event(), once=True) == 1
    assert attempts == [message.job_id]
    with database.transaction() as session:
        assert session.get(AiJobInbox, message.event_id).state == "completed"
        assert session.get(AiJobInbox, message.event_id).deliveries == 2
        reservation = session.get(AiRequestReservation, message.job_id)
        assert reservation.state == "succeeded" and reservation.provider_attempts == 0
        assert session.get(AiJob, message.job_id).result == {"answer": "synthetic durable result"}
