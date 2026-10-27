"""Approval state and ticket creation share a caller-owned transaction."""
from datetime import timedelta
from hashlib import sha256
import hmac
import json
import secrets
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from project_copilot.db.models import AgentContext, PendingAction
from project_copilot.schemas.approvals import ActionRead, ContextCredentials
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.tickets import TicketService


class ContextRejected(PermissionError):
    pass


class ActionNotFound(LookupError):
    pass


class FingerprintMismatch(ValueError):
    pass


def fingerprint(payload: TicketCreate) -> str:
    canonical = json.dumps(payload.model_dump(mode='json'), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return sha256(canonical.encode('utf-8')).hexdigest()


class ApprovalService:
    def __init__(self, session: Session, ttl_seconds: int = 900):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError('invalid approval ttl')
        self.session, self.ttl_seconds = session, ttl_seconds

    def now(self):
        return self.session.scalar(select(func.clock_timestamp()))

    def create_context(self):
        token = secrets.token_urlsafe(32)
        context = AgentContext(token_hash=sha256(token.encode('ascii')).hexdigest(),
                               expires_at=self.now() + timedelta(hours=24))
        self.session.add(context)
        self.session.flush()
        return ContextCredentials(context_id=context.id, context_token=token, expires_at=context.expires_at)

    def authenticate(self, context_id: UUID, token: str):
        if not isinstance(context_id, UUID) or not isinstance(token, str) or len(token) != 43 or not token.isascii():
            raise ContextRejected()
        context = self.session.get(AgentContext, context_id)
        supplied = sha256(token.encode('ascii')).hexdigest()
        matches = hmac.compare_digest(context.token_hash if context else '0' * 64, supplied)
        if not context or not matches or context.expires_at <= self.now():
            raise ContextRejected()
        return context

    def read(self, action):
        payload = TicketCreate(title=action.title, description=action.description)
        if not hmac.compare_digest(fingerprint(payload), action.payload_fingerprint):
            raise FingerprintMismatch()
        return ActionRead(action_id=action.id, context_id=action.context_id, status=action.status,
                          payload=payload, payload_fingerprint=action.payload_fingerprint,
                          created_at=action.created_at, expires_at=action.expires_at, ticket_id=action.ticket_id)

    def preview(self, context, payload):
        now = self.now()
        if context.expires_at <= now:
            raise ContextRejected()
        action = PendingAction(context_id=context.id, title=payload.title, description=payload.description,
                               payload_fingerprint=fingerprint(payload),
                               expires_at=min(now + timedelta(seconds=self.ttl_seconds), context.expires_at))
        self.session.add(action)
        self.session.flush()
        return self.read(action)

    def locked(self, context, action_id):
        self.session.execute(text("SET LOCAL lock_timeout = '5s'"))
        action = self.session.scalar(select(PendingAction).where(PendingAction.id == action_id,
                                      PendingAction.context_id == context.id).with_for_update())
        if action is None:
            raise ActionNotFound()
        now = self.now()
        if context.expires_at <= now:
            raise ContextRejected()
        self.read(action)
        return action, now

    def decide(self, context, action_id, payload_fingerprint, *, approve):
        action, now = self.locked(context, action_id)
        if not hmac.compare_digest(action.payload_fingerprint, payload_fingerprint):
            raise FingerprintMismatch()
        if action.status == 'pending' and action.expires_at <= now:
            action.status = 'expired'
        if action.status == 'pending':
            if approve:
                result = TicketService(self.session).create(TicketCreate(title=action.title, description=action.description),
                                                            creation_key=action.id)
                action.ticket_id = result.id
                action.status = 'completed'
            else:
                action.status = 'cancelled'
        self.session.flush()
        return self.read(action)

    def get(self, context, action_id):
        action, now = self.locked(context, action_id)
        if action.status == 'pending' and action.expires_at <= now:
            action.status = 'expired'
            self.session.flush()
        return self.read(action)
