"""PERSONAL_DEMO execution proofs: real OS leases plus transactional fencing.

There is deliberately no lease age/heartbeat timeout that releases a slot.
Missing historical evidence, a live holder, or an uncertain provider stays held.
"""
import fcntl
import os
from pathlib import Path
import stat
from uuid import UUID, uuid4

from sqlalchemy import func, select

from project_copilot.db.executions import AiJobExecution
from project_copilot.db.jobs import AiJob
from project_copilot.db.models import AiRequestReservation, PendingAction, Ticket


class ExecutionFenced(RuntimeError):
    """An obsolete execution must not make another request or business write."""


class ExecutionProofError(RuntimeError):
    """An execution lease cannot be independently verified."""


_DIRECTORY = ".job-executions"


def _uuid(value):
    if not isinstance(value, UUID):
        raise ExecutionProofError("invalid execution identity")
    return value


def _directory(manifest_dir, *, create):
    root = Path(manifest_dir).absolute()
    if create:
        root.mkdir(parents=True, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if create:
            try:
                os.mkdir(_DIRECTORY, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        descriptor = os.open(_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(descriptor)
            raise ExecutionProofError("execution lease directory is not private")
        return root, descriptor
    finally:
        os.close(root_fd)


def _header(job_id, token):
    return f"pc-execution-v1\n{job_id}\n{token}\n".encode("ascii")


class ExecutionLease:
    """One immutable-inode flock. Existing evidence is never recreated/deleted."""
    def __init__(self, root, job_id, token, descriptor, inode):
        self.root, self.job_id, self.token = root, job_id, token
        self.inode, self._fd = inode, descriptor
        self.path = root / _DIRECTORY / f"{token}.lock"

    @classmethod
    def create(cls, manifest_dir, job_id, token=None):
        job_id, token = _uuid(job_id), _uuid(token if token is not None else uuid4())
        descriptor = None
        try:
            root, directory = _directory(manifest_dir, create=True)
            try:
                descriptor = os.open(f"{token}.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = _header(job_id, token)
                written = 0
                while written < len(data):
                    written += os.write(descriptor, data[written:])
                os.fsync(descriptor)
                os.fsync(directory)
                info = os.fstat(descriptor)
                if not 1 <= info.st_ino <= 2**63 - 1:
                    raise ExecutionProofError("invalid execution lease inode")
                return cls(root, job_id, token, descriptor, info.st_ino)
            finally:
                os.close(directory)
        except (OSError, ExecutionProofError):
            if descriptor is not None:
                os.close(descriptor)
            raise ExecutionProofError("execution lease creation failed") from None

    @classmethod
    def try_acquire(cls, manifest_dir, job_id, token, inode):
        """Return None on missing/corrupt/live proof. Never create a replacement."""
        descriptor = None
        lease = None
        try:
            job_id, token = _uuid(job_id), _uuid(token)
            root, directory = _directory(manifest_dir, create=False)
            try:
                descriptor = os.open(f"{token}.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lease = cls(root, job_id, token, descriptor, inode)
                if not lease.valid_owned():
                    raise ExecutionProofError("execution lease identity differs")
                return lease
            finally:
                os.close(directory)
        except (OSError, ExecutionProofError):
            if lease is not None:
                lease.close()
            elif descriptor is not None:
                os.close(descriptor)
            return None

    def valid_owned(self):
        if self._fd is None:
            return False
        try:
            root, directory = _directory(self.root, create=False)
            try:
                info = os.fstat(self._fd)
                named = os.stat(f"{self.token}.lock", dir_fd=directory, follow_symlinks=False)
                return (stat.S_ISREG(info.st_mode) and stat.S_ISREG(named.st_mode)
                    and info.st_uid == os.getuid() and not info.st_mode & 0o077 and info.st_nlink == 1
                    and info.st_ino == self.inode == named.st_ino and info.st_dev == named.st_dev
                    and info.st_size == len(_header(self.job_id, self.token))
                    and os.pread(self._fd, info.st_size + 1, 0) == _header(self.job_id, self.token))
            finally:
                os.close(directory)
        except (OSError, ExecutionProofError):
            return False

    def close(self):
        if self._fd is not None:
            descriptor, self._fd = self._fd, None
            os.close(descriptor)  # OS releases the flock even on process death.

    def __del__(self):
        # Claim-only callers may drop an unconsumed lease. Active Worker scopes
        # retain a strong reference until all tracked execution has drained.
        try:
            self.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def register_execution(session, reservation, lease):
    if (reservation.personal_demo_grant_id is None or reservation.state != "running"
            or reservation.slot_released_at is not None or reservation.id != lease.job_id or not lease.valid_owned()):
        raise ExecutionFenced("execution enrollment rejected")
    row = AiJobExecution(job_id=reservation.id, token=lease.token, lease_inode=lease.inode)
    session.add(row)
    session.flush()
    return row


def _locked(session, job_id):
    # Consistent order shared with quota completion, provider boundaries and tools.
    reservation = session.scalar(select(AiRequestReservation).where(AiRequestReservation.id == job_id)
        .with_for_update().execution_options(populate_existing=True))
    execution = session.scalar(select(AiJobExecution).where(AiJobExecution.job_id == job_id)
        .with_for_update().execution_options(populate_existing=True))
    return reservation, execution


def guard_execution(session, job_id, token):
    reservation, execution = _locked(session, _uuid(job_id))
    if (reservation is None or reservation.personal_demo_grant_id is None or execution is None
            or execution.token != _uuid(token) or execution.fenced_at is not None
            or execution.local_done_at is not None or reservation.slot_released_at is not None):
        raise ExecutionFenced("execution is no longer admitted")
    return execution


def provider_started(session, job_id, token):
    execution = guard_execution(session, job_id, token)
    execution.provider_open += 1
    session.flush()


def provider_finished(database, job_id, token, *, completed):
    if type(completed) is not bool:
        raise ValueError("invalid provider completion proof")
    with database.transaction() as session:
        execution = guard_execution(session, job_id, token)
        if execution.provider_open < 1:
            raise ExecutionProofError("provider completion has no matching initiation")
        execution.provider_open -= 1
        if not completed:
            execution.provider_uncertain = True
    return True


def _business_valid(session, reservation, execution):
    if reservation.kind != "agent":
        return execution.action_id is None
    if execution.action_id is None:
        # Protocol v1 requires every proposal to publish its ID in this very
        # execution row and transaction. Old jobs lack v1 enrollment entirely.
        return True
    job = session.get(AiJob, reservation.id)
    action = session.get(PendingAction, execution.action_id, with_for_update=True)
    if (job is None or action is None or job.context_id is None
            or action.context_id != job.context_id or action.owner_user_id != reservation.user_id):
        return False
    if action.ticket_id is None:
        return action.status != "completed"
    ticket = session.get(Ticket, action.ticket_id)
    return bool(action.status == "completed" and ticket and ticket.creation_key == action.id
                and ticket.owner_user_id == reservation.user_id)


def _release(session, reservation, execution, *, reason):
    if (reservation.slot_released_at is not None or execution.fenced_at is not None
            or execution.provider_open != 0 or execution.provider_uncertain
            or not _business_valid(session, reservation, execution)):
        return False
    now = session.scalar(select(func.clock_timestamp()))
    # A crash can lose only delivery status. It never authorizes retry, billing
    # refund, or inference of a successful business operation.
    if reservation.state == "running":
        reservation.state, reservation.finished_at = "unknown", None
    elif reservation.state not in {"unknown", "succeeded", "failed"}:
        return False
    execution.fenced_at, execution.release_reason = now, reason
    reservation.slot_released_at = now
    session.flush()
    return True


def mark_local_done_and_release(database, settings, job_id, token, lease):
    """Called only after the executor's actual tracked futures have drained."""
    if (lease.job_id != job_id or lease.token != token or not lease.valid_owned()):
        raise ExecutionProofError("local execution lease is not held")
    with database.transaction() as session:
        reservation, execution = _locked(session, job_id)
        if (execution is None or execution.token != token or reservation is None
                or execution.lease_inode != lease.inode):
            raise ExecutionFenced("execution completion identity differs")
        if execution.fenced_at is not None:
            return reservation.slot_released_at is not None
        execution.local_done_at = session.scalar(select(func.clock_timestamp()))
        return _release(session, reservation, execution, reason="drained")


def finish_personal_execution(session, job, lease, *, outcome, usage):
    """Finalize delivery only after real Futures drain; proof still gates release.

    A known local failure is not provider/business uncertainty. Incomplete
    acknowledgements or inconsistent business bindings always remain unknown.
    The caller commits final delivery, diagnostic and terminal event atomically.
    """
    if (lease.job_id != job.id or not lease.valid_owned()):
        raise ExecutionProofError("final execution lease unavailable")
    reservation, execution = _locked(session, job.id)
    if (reservation is None or execution is None or reservation.user_id != job.user_id
            or execution.token != lease.token or execution.lease_inode != lease.inode
            or execution.fenced_at is not None or reservation.state != "running"):
        raise ExecutionFenced("final execution identity differs")
    settled = (execution.provider_open == 0 and not execution.provider_uncertain
               and _business_valid(session, reservation, execution))
    final = outcome if settled else "unknown"
    from project_copilot.services.quotas import QuotaService
    QuotaService(session).finish(job.user_id, job.id, outcome=final, usage=usage)
    execution.local_done_at = session.scalar(select(func.clock_timestamp()))
    _release(session, reservation, execution, reason="drained")
    return final


def reclaim_ended(session, settings, user_id=None, *, batch_size=20):
    """Reclaim only verifiably unheld protocol-v1 leases. Never use task age."""
    if type(batch_size) is not int or not 1 <= batch_size <= 20:
        raise ValueError("invalid execution inspection batch")
    query = select(AiRequestReservation, AiJobExecution).join(AiJobExecution).where(
        AiRequestReservation.personal_demo_grant_id.is_not(None),
        AiRequestReservation.slot_released_at.is_(None), AiJobExecution.fenced_at.is_(None),
        AiRequestReservation.kind.in_(("rag", "agent")),
    ).order_by(AiJobExecution.created_at, AiJobExecution.job_id).limit(batch_size)
    if user_id is not None:
        query = query.where(AiRequestReservation.user_id == user_id)
    count = 0
    for candidate, proof in session.execute(query).all():
        lease = ExecutionLease.try_acquire(settings.manifest_dir, candidate.id, proof.token, proof.lease_inode)
        if lease is None:
            continue
        try:
            reservation, execution = _locked(session, candidate.id)
            if (reservation is None or execution is None or execution.token != lease.token
                    or execution.lease_inode != lease.inode or not lease.valid_owned()):
                continue
            reason = "dead_readonly" if reservation.kind == "rag" else "dead_agent_fenced"
            count += int(_release(session, reservation, execution, reason=reason))
        finally:
            lease.close()
    return count
