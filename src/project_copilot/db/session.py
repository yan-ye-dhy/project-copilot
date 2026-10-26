"""Engine属于应用；每次操作独立Session，成功提交、异常回滚、最终关闭。"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str, *, connect_timeout: int = 5, pool_timeout: int = 5) -> None:
        # Engine construction is lazy: no connection on import or app startup.
        self.engine = create_engine(
            url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_timeout=pool_timeout,
            connect_args={"connect_timeout": connect_timeout}, hide_parameters=True,
        )
        self.sessions = sessionmaker(
            bind=self.engine, expire_on_commit=False, autoflush=False, close_resets_only=False,
        )

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """事务由调用边界持有；Service只flush，便于组合原子业务操作。"""
        with self.sessions() as session:
            with session.begin():
                yield session

    def close(self) -> None:
        self.engine.dispose()
