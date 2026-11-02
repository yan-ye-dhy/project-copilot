"""Cross-process admission for bounded public multipart reception."""
from sqlalchemy import text


class UploadIngressBusy(RuntimeError):
    pass


def acquire_upload_slot(database):
    connection = database.engine.connect()
    try:
        # A dedicated session lock limits all public upload bodies to one in
        # flight across processes, before allocating their bounded buffers.
        if not connection.scalar(text("SELECT pg_try_advisory_lock(576572591123)")):
            raise UploadIngressBusy()
        return connection
    except BaseException:
        connection.close()
        raise


def release_upload_slot(connection):
    try:
        connection.execute(text("SELECT pg_advisory_unlock(576572591123)"))
    except BaseException:
        # Never return a connection with an unknown session-level lock to pool.
        connection.invalidate()
        raise
    finally:
        connection.close()
