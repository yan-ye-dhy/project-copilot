"""PostgreSQL owns dispatch, deduplication and deferral; RabbitMQ carries IDs only.

Every public database operation owns a short transaction, except enqueue_outbox,
which participates in the API's existing transaction. Publishing never holds one.
Leases fence delivery evidence; they never authorize replay of a running job.
"""
from dataclasses import dataclass
from datetime import timedelta
import json
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from project_copilot.db.messaging import AiJobInbox, AiJobOutbox
from project_copilot.db.models import AiRequestReservation

_FIELDS = {"event_id", "job_id", "dispatch_generation", "schema_version"}
_DEFER_REASONS = {"capacity", "active", "release", "unavailable"}
_OUTCOMES = {"succeeded", "failed", "unknown"}


def _integer(value, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("invalid messaging configuration")
    return value


@dataclass(frozen=True)
class JobMessage:
    event_id: UUID
    job_id: UUID
    dispatch_generation: int = 1
    schema_version: int = 1

    def __post_init__(self):
        if type(self.event_id) is not UUID or type(self.job_id) is not UUID:
            raise ValueError("invalid dispatch identity")
        _integer(self.dispatch_generation, 1, 2**31 - 1)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported dispatch schema")

    def to_payload(self) -> bytes:
        return json.dumps({"event_id": str(self.event_id), "job_id": str(self.job_id),
            "dispatch_generation": self.dispatch_generation, "schema_version": self.schema_version},
            separators=(",", ":"), sort_keys=True).encode("ascii")


def parse_payload(payload: bytes) -> JobMessage:
    """Reject extra fields, duplicate keys, noncanonical IDs and oversized bodies."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid dispatch message")
            result[key] = value
        return result
    try:
        if type(payload) is not bytes or not 1 <= len(payload) <= 512:
            raise ValueError
        data = json.loads(payload, object_pairs_hook=unique)
        if type(data) is not dict or set(data) != _FIELDS:
            raise ValueError
        for name in ("event_id", "job_id"):
            value = data[name]
            if type(value) is not str or str(UUID(value)) != value:
                raise ValueError
        return JobMessage(UUID(data["event_id"]), UUID(data["job_id"]),
                          data["dispatch_generation"], data["schema_version"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("invalid dispatch message") from None


def _message(row):
    return JobMessage(row.event_id, row.job_id, row.dispatch_generation, row.schema_version)


def enqueue_outbox(session, job_id: UUID, *, dispatch_generation: int = 1) -> JobMessage:
    """Append atomically with a new job; a repeated generation reuses its event."""
    if type(job_id) is not UUID:
        raise ValueError("invalid dispatch identity")
    _integer(dispatch_generation, 1, 2**31 - 1)
    # The API's Session disables autoflush. Persist its pending AiJob first, in
    # this same transaction, before the outbox foreign key is checked.
    session.flush()
    session.execute(insert(AiJobOutbox).values(job_id=job_id, dispatch_generation=dispatch_generation)
        .on_conflict_do_nothing(constraint="uq_job_outbox_generation"))
    row = session.scalar(select(AiJobOutbox).where(AiJobOutbox.job_id == job_id,
        AiJobOutbox.dispatch_generation == dispatch_generation))
    return _message(row)


@dataclass(frozen=True)
class OutboxClaim:
    message: JobMessage
    token: UUID
    publish_attempts: int


def claim_outbox(database, *, lease_seconds: int = 30) -> OutboxClaim | None:
    _integer(lease_seconds, 1, 300)
    with database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        row = session.scalar(select(AiJobOutbox).where(AiJobOutbox.sent_at.is_(None),
            AiJobOutbox.available_at <= now,
            or_(AiJobOutbox.lease_until.is_(None), AiJobOutbox.lease_until <= now))
            .order_by(AiJobOutbox.available_at, AiJobOutbox.event_id)
            .with_for_update(skip_locked=True).limit(1))
        if row is None:
            return None
        row.lease_token, row.lease_until = uuid4(), now + timedelta(seconds=lease_seconds)
        row.publish_attempts += 1
        return OutboxClaim(_message(row), row.lease_token, row.publish_attempts)


def mark_outbox_sent(database, claim: OutboxClaim) -> bool:
    with database.transaction() as session:
        result = session.execute(update(AiJobOutbox).where(
            AiJobOutbox.event_id == claim.message.event_id, AiJobOutbox.lease_token == claim.token,
            AiJobOutbox.lease_until > func.clock_timestamp(), AiJobOutbox.sent_at.is_(None),
        ).values(sent_at=func.clock_timestamp(), lease_token=None, lease_until=None, failure_code=None))
        return result.rowcount == 1


def _retry_publish(database, claim, *, retry_base_seconds, retry_max_seconds):
    delay = min(retry_max_seconds, retry_base_seconds * (2 ** min(claim.publish_attempts - 1, 16)))
    with database.transaction() as session:
        session.execute(update(AiJobOutbox).where(AiJobOutbox.event_id == claim.message.event_id,
            AiJobOutbox.lease_token == claim.token, AiJobOutbox.sent_at.is_(None))
            .values(available_at=func.clock_timestamp() + timedelta(seconds=delay),
                    lease_token=None, lease_until=None, failure_code="publish_failed"))


def publish_claim(database, publisher, claim: OutboxClaim, *, retry_base_seconds: int = 5,
                  retry_max_seconds: int = 300) -> bool:
    _integer(retry_base_seconds, 1, 3600)
    _integer(retry_max_seconds, retry_base_seconds, 86400)
    try:
        # No transaction spans publisher confirms or a broker network timeout.
        publisher.publish(claim.message)
    except Exception:
        # Persist one allowlisted code, never exception text, credentials or body.
        _retry_publish(database, claim, retry_base_seconds=retry_base_seconds,
                       retry_max_seconds=retry_max_seconds)
        return False
    return mark_outbox_sent(database, claim)


def relay_once(database, publisher, *, lease_seconds: int = 30,
               retry_base_seconds: int = 5, retry_max_seconds: int = 300) -> bool:
    _integer(retry_base_seconds, 1, 3600)
    _integer(retry_max_seconds, retry_base_seconds, 86400)
    claim = claim_outbox(database, lease_seconds=lease_seconds)
    if claim is None:
        return False
    publish_claim(database, publisher, claim, retry_base_seconds=retry_base_seconds,
                  retry_max_seconds=retry_max_seconds)
    return True


def relay_batch(database, publisher, *, limit: int = 100, lease_seconds: int = 30,
                retry_base_seconds: int = 5, retry_max_seconds: int = 300) -> int:
    _integer(limit, 1, 1000)
    attempted = 0
    while attempted < limit and relay_once(database, publisher, lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds, retry_max_seconds=retry_max_seconds):
        attempted += 1
    return attempted


@dataclass(frozen=True)
class DeliveryClaim:
    message: JobMessage
    token: UUID | None
    decision: str


def _outbox_for_delivery(session, message):
    row = session.get(AiJobOutbox, message.event_id, with_for_update=True)
    if row is None or _message(row) != message:
        return None
    latest = session.scalar(select(func.max(AiJobOutbox.dispatch_generation))
        .where(AiJobOutbox.job_id == message.job_id))
    return row if latest == message.dispatch_generation else None


def _defer(outbox, inbox, now, delay_seconds, reason, *, release_owner):
    available = max(inbox.available_at, now + timedelta(seconds=delay_seconds))
    if not release_owner and inbox.lease_until is not None:
        available = max(available, inbox.lease_until)
    inbox.available_at, inbox.defer_reason = available, reason
    if release_owner:
        inbox.state, inbox.lease_token, inbox.lease_until = "deferred", None, None
    # Fence a publisher whose confirm races this consumer decision. A successful
    # late confirm must not overwrite this committed future dispatch.
    outbox.sent_at, outbox.lease_token, outbox.lease_until = None, None, None
    outbox.available_at = available


def _complete(outbox, inbox, outcome, now):
    inbox.state, inbox.completed_at, inbox.outcome = "completed", now, outcome
    inbox.lease_token, inbox.lease_until, inbox.defer_reason = None, None, None
    # The received event proves publication; cancel any pending duplicate wakeup.
    outbox.sent_at, outbox.lease_token, outbox.lease_until = now, None, None


def begin_delivery(database, message: JobMessage, *, lease_seconds: int = 60,
                   defer_seconds: int = 5) -> DeliveryClaim:
    """Return only after a duplicate/defer/outcome decision is durable.

    An active duplicate schedules a future outbox dispatch before its ACK. It
    keeps the first consumer's token so that consumer can still record outcome.
    An expired inbox lease can reclaim only a still-reserved PostgreSQL job.
    """
    if type(message) is not JobMessage:
        raise ValueError("invalid dispatch message")
    _integer(lease_seconds, 1, 3600)
    _integer(defer_seconds, 1, 3600)
    with database.transaction() as session:
        outbox = _outbox_for_delivery(session, message)
        if outbox is None:
            return DeliveryClaim(message, None, "invalid")
        now = session.scalar(select(func.clock_timestamp()))
        inbox = session.get(AiJobInbox, message.event_id, with_for_update=True)
        if inbox is None:
            inbox = AiJobInbox(event_id=message.event_id, job_id=message.job_id,
                dispatch_generation=message.dispatch_generation, state="deferred", deliveries=1,
                available_at=now, first_received_at=now, last_received_at=now)
            session.add(inbox)
        else:
            inbox.deliveries += 1
            inbox.last_received_at = now
        if inbox.state == "completed":
            return DeliveryClaim(message, None, "completed")
        reservation = session.get(AiRequestReservation, message.job_id, with_for_update=True)
        if reservation.state in _OUTCOMES:
            _complete(outbox, inbox, reservation.state, now)
            return DeliveryClaim(message, None, "completed")
        active = inbox.lease_until is not None and inbox.lease_until > now
        if active or reservation.state == "running" or inbox.available_at > now:
            preserve_owner = active or (inbox.state == "processing" and reservation.state == "running")
            _defer(outbox, inbox, now, defer_seconds, "active", release_owner=not preserve_owner)
            return DeliveryClaim(message, None, "deferred")
        inbox.state, inbox.lease_token = "processing", uuid4()
        inbox.lease_until = now + timedelta(seconds=lease_seconds)
        inbox.defer_reason = None
        return DeliveryClaim(message, inbox.lease_token, "ready")


def defer_delivery(database, claim: DeliveryClaim, *, delay_seconds: int = 5,
                   reason: str = "capacity") -> bool:
    _integer(delay_seconds, 1, 3600)
    if reason not in _DEFER_REASONS:
        raise ValueError("invalid delivery defer reason")
    with database.transaction() as session:
        outbox = _outbox_for_delivery(session, claim.message)
        if outbox is None:
            return False
        inbox = session.get(AiJobInbox, claim.message.event_id, with_for_update=True)
        if inbox is None:
            return False
        if inbox.state == "completed":
            return True
        if claim.token is None or inbox.lease_token != claim.token:
            return False
        now = session.scalar(select(func.clock_timestamp()))
        _defer(outbox, inbox, now, delay_seconds, reason, release_owner=True)
        return True


def complete_delivery(database, claim: DeliveryClaim) -> bool:
    """Never complete a processing inbox on the strength of a local return."""
    with database.transaction() as session:
        outbox = _outbox_for_delivery(session, claim.message)
        if outbox is None:
            return False
        inbox = session.get(AiJobInbox, claim.message.event_id, with_for_update=True)
        if inbox is None:
            return False
        if inbox.state == "completed":
            return True
        if claim.token is None or inbox.lease_token != claim.token:
            return False
        reservation = session.get(AiRequestReservation, claim.message.job_id, with_for_update=True)
        if reservation.state not in _OUTCOMES:
            return False
        _complete(outbox, inbox, reservation.state, session.scalar(select(func.clock_timestamp())))
        return True
