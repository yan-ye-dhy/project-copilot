"""Recovery settings remain an explicit, release-bound policy."""
import pytest
from pydantic import ValidationError
from project_copilot.config import Settings
from project_copilot.services.release_control import config_fingerprint


def test_recovery_is_disabled_and_model_repeat_is_not_implicit():
    settings = Settings(_env_file=None)
    assert not settings.job_recovery_enabled and not settings.job_recovery_allow_model_repeat
    assert settings.job_recovery_max_attempts == 2


@pytest.mark.parametrize("values", [{"job_recovery_enabled": True}, {"job_recovery_allow_model_repeat": True},
                                  {"job_recovery_max_attempts": 3}])
def test_recovery_cannot_bypass_reliable_delivery_or_bounded_attempts(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_model_repeat_policy_changes_approved_configuration():
    common = dict(_env_file=None, message_queue_enabled=True, job_recovery_enabled=True,
                  rabbitmq_url="amqp://test:synthetic@localhost:15672/test",
                  database_url="postgresql+psycopg://test:synthetic@localhost:5434/test")
    assert config_fingerprint(Settings(**common)) != config_fingerprint(
        Settings(**common, job_recovery_allow_model_repeat=True))
