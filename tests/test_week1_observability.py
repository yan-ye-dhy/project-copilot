"""Real OTel span creation; collector export and providers are not contacted."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from project_copilot.config import Settings
from project_copilot.infrastructure.tracing import create_trace_runtime
from project_copilot.infrastructure.redis_runtime import RedisUnavailable
from project_copilot.services import redis_admission


@pytest.mark.parametrize("error,attribute", [(redis_admission.RateLimited(1), "rate_limit_rejected"),
                                          (RedisUnavailable(), "admission.unavailable")])
def test_admission_span_measures_actual_gate_duration_and_safe_outcome(monkeypatch, error, attribute):
    monkeypatch.setattr(redis_admission, "_admit", lambda *_: (_ for _ in ()).throw(error))
    runtime = create_trace_runtime(Settings(_env_file=None, tracing_enabled=True))
    try:
        with runtime.activate("synthetic-observability") as root:
            with pytest.raises(type(error)):
                redis_admission.admit(SimpleNamespace(redis_admission_enabled=True), uuid4())
        spans, overflow = runtime.processor.take(root.get_span_context().trace_id)
        gate = next(span for span in spans if span.name == "admission.redis")
        assert gate.attributes[attribute] is True and gate.attributes["admission_wait_ms"] >= 0
        assert "user_id" not in gate.attributes and "redis_url" not in gate.attributes and not overflow
    finally:
        runtime.close()


def test_real_db_pool_span_is_checkout_duration_not_invented_queue_wait(ticket_database):
    runtime = create_trace_runtime(Settings(_env_file=None, tracing_enabled=True))
    try:
        with runtime.activate("synthetic-db-observability") as root:
            with ticket_database.transaction() as session:
                assert session.scalar(text("SELECT 1")) == 1
        spans, _ = runtime.processor.take(root.get_span_context().trace_id)
        pool = next(span for span in spans if span.name == "db.pool.checkout")
        assert pool.attributes["db_checkout_ms"] >= 0
        assert "db_pool_wait_ms" not in pool.attributes
        assert "db.statement" not in pool.attributes
    finally:
        runtime.close()
