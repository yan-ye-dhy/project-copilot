"""Opt-in messaging does not weaken the default release or leak credentials."""
import pytest
from pydantic import ValidationError
from project_copilot.config import Settings
from project_copilot.services.release_control import config_fingerprint


def settings(**values):
    return Settings(_env_file=None, **values)


def test_messaging_disabled_by_default():
    active = settings()
    assert not active.message_queue_enabled and active.rabbitmq_url is None


def test_enabled_messaging_requires_database_and_broker():
    with pytest.raises(ValidationError):
        settings(message_queue_enabled=True)


@pytest.mark.parametrize("url", ["http://private-host:5672/", "amqp://host:5672/", "amqp://u:p@host/",
                                  "amqp://u:p@host:5672/v?password=canary", "amqp://u:p@host:5672/v#canary"])
def test_malformed_broker_rejected_without_secret_in_validation_text(url):
    with pytest.raises(ValidationError) as error:
        settings(rabbitmq_url=url)
    assert url not in str(error.value)


def test_enabled_broker_configuration_bound_without_credentials():
    values = dict(message_queue_enabled=True, database_url="postgresql+psycopg://u:p@localhost:5434/test",
                  rabbitmq_url="amqp://alice:private-canary@localhost:5673/pc_test")
    first = config_fingerprint(settings(**values))
    values["rabbitmq_url"] = "amqp://alice:rotated-canary@localhost:5673/pc_test"
    assert config_fingerprint(settings(**values)) == first
    values["rabbitmq_queue"] = "pc.changed"
    assert config_fingerprint(settings(**values)) != first
    values["rabbitmq_queue"] = "pc.jobs.v1"
    values["rabbitmq_url"] = "amqp://alice:rotated-canary@localhost:5673/other"
    assert config_fingerprint(settings(**values)) != first


def test_backoff_bounds_validated():
    with pytest.raises(ValidationError):
        settings(outbox_retry_base_seconds=60, outbox_retry_max_seconds=5)
