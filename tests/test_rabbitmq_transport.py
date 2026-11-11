"""Transport policy tests; no external service or provider requests."""
from contextvars import ContextVar
import logging
from threading import Event, get_ident
from types import SimpleNamespace
import time
from uuid import uuid4

import pytest
from pydantic import SecretStr

from project_copilot.infrastructure import rabbitmq
from project_copilot.services.messaging import JobMessage


def settings():
    return SimpleNamespace(message_queue_enabled=True, rabbitmq_url=SecretStr(
        "amqp://synthetic-user:PRIVATE_PASSWORD@127.0.0.1:15672/%2F"),
        rabbitmq_exchange="pc.test.exchange", rabbitmq_queue="pc.test.queue",
        rabbitmq_routing_key="job", rabbitmq_queue_max_length=100,
        rabbitmq_delivery_limit=20,
        rabbitmq_heartbeat_seconds=5, rabbitmq_socket_timeout_seconds=1,
        rabbitmq_blocked_timeout_seconds=2)


class Channel:
    def __init__(self):
        self.calls = []
        self.is_open = True
        self.consumer_tags = {"test"}
        self.publish_error = None
    def __getattr__(self, name):
        def call(**kwargs):
            self.calls.append((name, kwargs, get_ident()))
            if name == "basic_publish" and self.publish_error:
                raise self.publish_error
            if name == "basic_consume":
                self.callback = kwargs["on_message_callback"]
        return call


class Connection:
    def __init__(self, channel, body=None):
        self.channel_instance = channel
        self.body = body
        self.is_open = True
        self.pumps = 0
        self.closed = False
    def channel(self):
        return self.channel_instance
    def close(self):
        self.closed = True
        self.is_open = False
    def process_data_events(self, **_):
        self.pumps += 1
        if self.body is not None:
            body, self.body = self.body, None
            self.channel_instance.callback(self.channel_instance,
                SimpleNamespace(delivery_tag=7), None, body)
        time.sleep(0.005)


def install(monkeypatch, body=None):
    channel = Channel()
    connection = Connection(channel, body)
    monkeypatch.setattr(rabbitmq, "_connect", lambda _: (connection, channel))
    return connection, channel


def test_connect_declares_bounded_durable_quorum_topology_with_no_connection_retry(monkeypatch):
    import pika
    channel = Channel()
    connection = Connection(channel)
    parameters = []
    monkeypatch.setattr(pika, "BlockingConnection", lambda p: parameters.append(p) or connection)
    rabbitmq._connect(settings())
    assert parameters[0].connection_attempts == 1 and parameters[0].retry_delay == 0
    assert parameters[0].heartbeat == 5 and parameters[0].blocked_connection_timeout == 2
    declarations = dict((name, values) for name, values, _ in channel.calls)
    assert declarations["exchange_declare"]["durable"] is True
    queues = {values["queue"]: values for name, values, _ in channel.calls if name == "queue_declare"}
    queue = queues["pc.test.queue"]
    assert queue["durable"] is True
    assert queue["arguments"] == {"x-queue-type": "quorum", "x-quorum-initial-group-size": 1,
        "x-max-length": 100, "x-max-length-bytes": 51200, "x-overflow": "reject-publish",
        "x-delivery-limit": 20, "x-dead-letter-exchange": "pc.test.exchange.dead",
        "x-dead-letter-routing-key": "job.dead", "x-dead-letter-strategy": "at-least-once"}
    assert queues["pc.test.queue.dead"]["durable"] is True
    assert queues["pc.test.queue.dead"]["arguments"]["x-overflow"] == "reject-publish"


def test_publish_requires_confirm_mandatory_persistence_and_only_minimal_envelope(monkeypatch):
    connection, channel = install(monkeypatch)
    message = JobMessage(uuid4(), uuid4())
    with rabbitmq.RabbitPublisher(settings()) as publisher:
        publisher.publish(message)
    assert [call[0] for call in channel.calls] == ["confirm_delivery", "basic_publish"]
    publish = channel.calls[1][1]
    assert publish["mandatory"] is True and publish["body"] == message.to_payload()
    assert publish["properties"].delivery_mode == 2
    assert publish["properties"].message_id == str(message.event_id)
    assert connection.closed


@pytest.mark.parametrize("failure", [RuntimeError("PRIVATE_PASSWORD amqp://private-host"),
    OSError("PRIVATE_PASSWORD network diagnostics")])
def test_publish_failure_closes_ambiguous_connection_and_redacts_cause(monkeypatch, caplog, failure):
    connection, channel = install(monkeypatch)
    channel.publish_error = failure
    with pytest.raises(rabbitmq.RabbitMQTransportError) as error:
        rabbitmq.RabbitPublisher(settings()).publish(JobMessage(uuid4(), uuid4()))
    assert connection.closed and error.value.__suppress_context__
    assert "PRIVATE_PASSWORD" not in str(error.value) + caplog.text


def test_business_callback_runs_in_copied_context_while_io_pumps_before_ack(monkeypatch):
    message = JobMessage(uuid4(), uuid4())
    connection, channel = install(monkeypatch, message.to_payload())
    scope = ContextVar("test_worker_scope")
    token = scope.set("retained-scope")
    io_thread = get_ident()
    seen = []
    def process(received):
        assert received == message and scope.get() == "retained-scope"
        assert get_ident() != io_thread
        time.sleep(0.05)
        assert connection.pumps >= 3
        assert not any(name == "basic_ack" for name, *_ in channel.calls)
        seen.append("committed")
    try:
        assert rabbitmq.RabbitConsumer(settings()).run(process, Event(), once=True) == 1
    finally:
        scope.reset(token)
    assert seen == ["committed"]
    assert [(values, thread) for name, values, thread in channel.calls if name == "basic_ack"] == [
        ({"delivery_tag": 7}, io_thread)]
    assert connection.closed


def test_callback_failure_leaves_delivery_unacked_and_has_no_automatic_business_retry(monkeypatch):
    connection, channel = install(monkeypatch, JobMessage(uuid4(), uuid4()).to_payload())
    attempts = []
    def process(_):
        attempts.append(1)
        raise RuntimeError("private-model-diagnostics")
    with pytest.raises(rabbitmq.RabbitMQTransportError) as error:
        rabbitmq.RabbitConsumer(settings()).run(process, Event(), once=True)
    assert attempts == [1] and connection.closed
    assert not any(name in {"basic_ack", "basic_nack", "basic_reject"} for name, *_ in channel.calls)
    assert "private-model-diagnostics" not in str(error.value)


def test_stop_during_execution_drains_commit_before_ack(monkeypatch):
    connection, channel = install(monkeypatch, JobMessage(uuid4(), uuid4()).to_payload())
    stopping = Event()
    def process(_):
        stopping.set()
        time.sleep(0.03)
    assert rabbitmq.RabbitConsumer(settings()).run(process, stopping) == 1
    assert connection.pumps >= 3 and any(name == "basic_ack" for name, *_ in channel.calls)


def test_stop_during_delivery_never_starts_a_new_business_callback(monkeypatch):
    connection, channel = install(monkeypatch, JobMessage(uuid4(), uuid4()).to_payload())
    stopping = Event()
    original = connection.process_data_events
    def pump(**kwargs):
        stopping.set()
        original(**kwargs)
    connection.process_data_events = pump
    assert rabbitmq.RabbitConsumer(settings()).run(lambda _: pytest.fail("new job started"), stopping) == 0
    assert connection.closed and not any(name == "basic_ack" for name, *_ in channel.calls)


def test_idle_publisher_pumps_heartbeat_and_reconnects_only_on_next_publish(monkeypatch):
    connection, _ = install(monkeypatch)
    publisher = rabbitmq.RabbitPublisher(settings())
    publisher.pump()
    assert connection.pumps == 0
    publisher.publish(JobMessage(uuid4(), uuid4()))
    publisher.pump()
    assert connection.pumps == 1
    def disconnected(**_):
        raise OSError("private-network-text")
    connection.process_data_events = disconnected
    publisher.pump()
    assert connection.closed and publisher._connection is None


def test_malformed_envelope_is_rejected_without_executing_callback(monkeypatch, caplog):
    connection, channel = install(monkeypatch, b'{"payload":"PRIVATE_CONTENT"}')
    stopping = Event()
    original = connection.process_data_events
    def pump(**kwargs):
        original(**kwargs)
        stopping.set()
    connection.process_data_events = pump
    assert rabbitmq.RabbitConsumer(settings()).run(lambda _: pytest.fail("business callback ran"), stopping) == 0
    assert [(name, values) for name, values, _ in channel.calls if name == "basic_reject"] == [
        ("basic_reject", {"delivery_tag": 7, "requeue": False})]
    assert "PRIVATE_CONTENT" not in caplog.text


def test_pika_diagnostics_do_not_propagate_private_server_text(caplog):
    rabbitmq._pika()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("pika.adapters.utils.connection_workflow").error("PRIVATE_PASSWORD amqp://private")
    assert "PRIVATE_PASSWORD" not in caplog.text
