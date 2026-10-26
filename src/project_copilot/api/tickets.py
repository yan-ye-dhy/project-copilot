"""普通工单HTTP接口；事务提交/关闭在响应发送前完成，不提供Agent写入路径。"""

from collections.abc import Iterator
import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from project_copilot.schemas.tickets import TicketCreate, TicketFilters, TicketList, TicketRead
from project_copilot.services.tickets import TicketNotFoundError, TicketService


router = APIRouter(prefix="/tickets", tags=["tickets"])
logger = logging.getLogger(__name__)


def ticket_service(request: Request) -> Iterator[TicketService]:
    database = request.app.state.database
    if database is None:
        raise HTTPException(status_code=503, detail="工单数据库未配置，请检查本项目配置")
    try:
        with database.transaction() as session:
            yield TicketService(session)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="工单不存在") from None
    except SQLAlchemyError as exc:
        # Never log the exception text, SQL parameters, DSN or driver diagnostics.
        logger.warning("Ticket database operation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="工单数据库操作失败，请根据run_id排查") from None


# Function scope ensures commit errors become HTTP errors before any 201 is sent.
Service = Annotated[TicketService, Depends(ticket_service, scope="function")]


@router.post("", status_code=201, response_model=TicketRead)
def create_ticket(payload: TicketCreate, service: Service) -> TicketRead:
    return service.create(payload)


@router.get("", response_model=TicketList)
def search_tickets(filters: Annotated[TicketFilters, Query()], service: Service) -> TicketList:
    return service.search(filters)


@router.get("/{ticket_id}", response_model=TicketRead)
def get_ticket(ticket_id: UUID, service: Service) -> TicketRead:
    return service.get(ticket_id)
