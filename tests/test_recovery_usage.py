"""Usage scopes on owned PostgreSQL; initiation counts are synthetic, no HTTP."""
import pytest
from project_copilot.db.models import AiRequestReservation
from project_copilot.services.job_recovery import recover_job
from project_copilot.services.job_usage import logical_usage
from test_job_recovery_postgres import recovery_setup, USAGE


@pytest.mark.parametrize("prior_count,prior_usage,new_count,new_usage,expected", [
    (1, USAGE, 1, USAGE, None),
    (1, USAGE, 0, USAGE, USAGE),
    (1, None, 1, USAGE, None),
    (1, None, 0, USAGE, None),
    (0, None, 1, USAGE, USAGE),
])
def test_recovery_never_understates_or_double_counts_known_usage(recovery_setup, prior_count, prior_usage,
                                                                new_count, new_usage, expected):
    setup = recovery_setup
    identity = setup.reserve()
    first = setup.claim(identity)
    with setup.database.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        row.provider_attempts, row.usage, row.state = prior_count, prior_usage, "unknown"
    first.execution_lease.close()  # Controlled local execution ended; no HTTP was ever sent.
    assert recover_job(setup.database, setup.settings, identity, allow_model_repeat=True).decision == "requeued"
    setup.claim(identity)
    with setup.database.transaction() as session:
        row = session.get(AiRequestReservation, identity)
        row.provider_attempts += new_count
        session.flush()
        assert logical_usage(session, identity, new_usage) == expected
        assert row.provider_attempts == prior_count + new_count
