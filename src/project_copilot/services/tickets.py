"""真实工单业务：绑定参数查询，flush取回服务器字段，不私自提交外层事务。"""

from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.db.models import Ticket
from project_copilot.schemas.tickets import TicketCreate, TicketFilters, TicketList, TicketRead


class TicketNotFoundError(LookupError):
    pass


def ticket_query(filters: TicketFilters) -> Select:
    query = select(Ticket)
    if filters.status is not None:
        query = query.where(Ticket.status == filters.status.value)
    if filters.q is not None:
        # Bind values, and treat %/_ as literal input rather than SQL wildcards.
        query = query.where(or_(Ticket.title.icontains(filters.q, autoescape=True),
                               Ticket.description.icontains(filters.q, autoescape=True)))
    return query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(filters.limit).offset(filters.offset)


class TicketService:
    def __init__(self, session: Session):
        self.session = session

    def create(self, payload: TicketCreate, *, creation_key: UUID | None = None) -> TicketRead:
        with trace_span("ticket.create", {"openinference.span.kind": "CHAIN", "ticket.has_creation_key": creation_key is not None}) as span:
            ticket = Ticket(title=payload.title, description=payload.description, creation_key=creation_key)
            self.session.add(ticket)
            self.session.flush()  # Not committed until Database.transaction exits successfully.
            result = TicketRead.model_validate(ticket)
            span.set_attribute("ticket.id", str(result.id))
            span.set_attribute("status", "flushed")
            return result

    def get(self, ticket_id: UUID) -> TicketRead:
        with trace_span("ticket.get", {"openinference.span.kind": "CHAIN", "ticket.id": str(ticket_id)}):
            ticket = self.session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError("工单不存在")
            return TicketRead.model_validate(ticket)

    def search(self, filters: TicketFilters) -> TicketList:
        with trace_span("ticket.search", {"openinference.span.kind": "CHAIN", "ticket.limit": filters.limit,
                        "ticket.offset": filters.offset, "ticket.has_keyword": filters.q is not None}) as span:
            tickets = self.session.scalars(ticket_query(filters))
            result = TicketList(items=tuple(TicketRead.model_validate(ticket) for ticket in tickets),
                                limit=filters.limit, offset=filters.offset)
            span.set_attribute("ticket.result_count", len(result.items))
            return result
