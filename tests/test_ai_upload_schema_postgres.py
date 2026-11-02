"""Upload job DB boundaries and transaction races on isolated real PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from project_copilot.db.jobs import AiJob, AiJobUpload
from project_copilot.db.models import AiRequestReservation
from project_copilot.schemas.auth import LoginInput, RegisterInput
from project_copilot.services.auth import AuthService
from project_copilot.services.quotas import QuotaService


def credentials(database):
    with database.transaction() as session:
        auth = AuthService(session)
        user = auth.register(RegisterInput(username="uploader", password="unit-test-password-1357"), open_registration=True)
        token = auth.login(LoginInput(username="uploader", password="unit-test-password-1357")).token
        _, login = auth.authenticate(token)
        return user.id, login.id


def make_job(session, user_id, login_id, *, key="request-key-1", fingerprint="a" * 64):
    reservation, reused = QuotaService(session).reserve(user_id, key, fingerprint, "upload")
    if not reused:
        session.add(AiJob(id=reservation.id, login_session_id=login_id, payload={}))
        session.flush()
    return reservation, reused


def add_upload(session, job_id, content, *, source="sample.txt", media="text/plain", digest=None):
    session.add(AiJobUpload(job_id=job_id, content=content, source_name=source, media_type=media,
                            content_sha256=digest if digest is not None else sha256(content).hexdigest()))
    session.flush()


def test_upload_size_constraints_and_claim_time_cleanup(ticket_database):
    db = ticket_database
    user_id, login_id = credentials(db)
    with db.transaction() as session:
        reservation, _ = make_job(session, user_id, login_id)
        job_id = reservation.id
    for bad_content in (b"", b"x" * (4 * 1024 * 1024 + 1)):
        with pytest.raises(IntegrityError):
            with db.transaction() as session:
                add_upload(session, job_id, bad_content)
    with pytest.raises(IntegrityError):
        with db.transaction() as session:
            add_upload(session, job_id, b"data", digest="not-a-digest")
    content = b"x" * (4 * 1024 * 1024)
    with db.transaction() as session:
        add_upload(session, job_id, content)
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            attachment = session.get(AiJobUpload, job_id)
            assert attachment.content == content and attachment.content_sha256 == sha256(content).hexdigest()
            session.delete(attachment)
            raise RuntimeError("simulate claim rollback")
    with db.transaction() as session:
        attachment = session.get(AiJobUpload, job_id)
        assert attachment is not None
        session.delete(attachment)
    with db.transaction() as session:
        assert session.get(AiJobUpload, job_id) is None
        assert session.get(AiJob, job_id) is not None
    assert db.engine.pool.checkedout() == 0


def test_concurrent_upload_idempotency_creates_one_attachment(ticket_database):
    db = ticket_database
    user_id, login_id = credentials(db)
    barrier = Barrier(4)

    def submit(_):
        barrier.wait(timeout=10)
        with db.transaction() as session:
            reservation, reused = make_job(session, user_id, login_id)
            if not reused:
                add_upload(session, reservation.id, b"synthetic")
            return reservation.id, reused

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))
    assert len({job_id for job_id, _ in results}) == 1
    assert sum(not reused for _, reused in results) == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AiJobUpload)) == 1
        assert session.scalar(select(func.count()).select_from(AiRequestReservation)) == 1
    assert db.engine.pool.checkedout() == 0
