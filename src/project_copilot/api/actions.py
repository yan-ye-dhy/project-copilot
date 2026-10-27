"""Capability-bound proposal preview and explicit decisions."""
from collections.abc import Iterator
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.schemas.approvals import ActionRead, ContextCredentials, Decision
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ActionNotFound, ApprovalService, ContextRejected, FingerprintMismatch

router = APIRouter(tags=['actions'])
logger = logging.getLogger(__name__)


def credentials(request: Request, *, optional=False):
    ids = request.headers.getlist('x-copilot-context-id')
    tokens = request.headers.getlist('x-copilot-context-token')
    if optional and not ids and not tokens:
        return None
    if len(ids) != 1 or len(tokens) != 1:
        raise HTTPException(403, '上下文凭据无效或已过期')
    try:
        return UUID(ids[0]), tokens[0]
    except ValueError:
        raise HTTPException(403, '上下文凭据无效或已过期') from None


def service(request: Request) -> Iterator[ApprovalService]:
    database = request.app.state.database
    if database is None:
        raise HTTPException(503, '工单数据库未配置')
    try:
        with database.transaction() as session:
            yield ApprovalService(session)
    except ContextRejected:
        raise HTTPException(403, '上下文凭据无效或已过期') from None
    except ActionNotFound:
        raise HTTPException(404, '提案不存在') from None
    except FingerprintMismatch:
        raise HTTPException(409, '提案指纹不匹配') from None
    except SQLAlchemyError as exc:
        logger.warning('approval database error run_id=%s type=%s', request.state.run_id, type(exc).__name__)
        raise HTTPException(503, '数据库操作失败，请根据run_id排查') from None


Service = Annotated[ApprovalService, Depends(service, scope='function')]


@router.post('/agent/contexts', status_code=201, response_model=ContextCredentials)
def new_context(svc: Service):
    return svc.create_context()


@router.post('/actions/preview', status_code=201, response_model=ActionRead)
def preview(payload: TicketCreate, request: Request, svc: Service):
    return svc.preview(svc.authenticate(*credentials(request)), payload)


@router.get('/actions/{action_id}', response_model=ActionRead)
def read_action(action_id: UUID, request: Request, svc: Service):
    return svc.get(svc.authenticate(*credentials(request)), action_id)


@router.post('/actions/{action_id}/confirm', response_model=ActionRead)
def confirm(action_id: UUID, decision: Decision, request: Request, svc: Service):
    return svc.decide(svc.authenticate(*credentials(request)), action_id, decision.payload_fingerprint, approve=True)


@router.post('/actions/{action_id}/reject', response_model=ActionRead)
def reject(action_id: UUID, decision: Decision, request: Request, svc: Service):
    return svc.decide(svc.authenticate(*credentials(request)), action_id, decision.payload_fingerprint, approve=False)
