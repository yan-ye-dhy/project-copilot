"""Engine属于应用；每次操作独立Session，成功提交、异常回滚、最终关闭。"""

from collections.abc import Iterator
from contextlib import contextmanager
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from project_copilot.infrastructure.tracing import detached_trace_span


class ObservedQueuePool(QueuePool):
    def _do_get(self):
        started = time.perf_counter()
        with detached_trace_span("db.pool.checkout", {"db.system": "postgresql"}) as span:
            try:
                return super()._do_get()
            except BaseException:
                span.set_attribute("db.pool.checkout_failed", True)
                raise
            finally:
                # Includes establishing a new connection, not only queue wait.
                span.set_attribute("db_checkout_ms", (time.perf_counter() - started) * 1000)


class Database:
    def __init__(self, url: str, *, connect_timeout: int = 5, pool_timeout: int = 5,
                 pool_size: int = 5, max_overflow: int = 5) -> None:
        if (type(pool_size) is not int or not 1 <= pool_size <= 20 or
                type(max_overflow) is not int or not 0 <= max_overflow <= 20):
            raise ValueError("invalid database pool capacity")
        # Engine construction is lazy: no connection on import or app startup.
        self.engine = create_engine(
            url, poolclass=ObservedQueuePool, pool_pre_ping=True, pool_size=pool_size,
            max_overflow=max_overflow, pool_timeout=pool_timeout,
            connect_args={"connect_timeout": connect_timeout}, hide_parameters=True,
        )
        self.sessions = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False, close_resets_only=False,
        )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """事务由调用边界持有；Service只flush，便于组合原子业务操作。"""
        with detached_trace_span("db.transaction", {"openinference.span.kind": "CHAIN", "db.system": "postgresql"}) as span:
            try:
                with self.sessions() as session:
                    with session.begin():
                        yield session
                span.set_attribute("transaction.state", "committed")
            except BaseException:
                span.set_attribute("transaction.state", "rolled_back")
                raise

    def close(self) -> None:
        self.engine.dispose()
