"""真实工单业务：绑定参数查询，flush取回服务器字段，不私自提交外层事务。"""

from uuid import UUID

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from project_copilot.infrastructure.tracing import trace_span
from project_copilot.db.models import Ticket
from project_copilot.schemas.tickets import TicketCreate, TicketFilters, TicketList, TicketRead
from project_copilot.services.auth import Principal


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
    def __init__(self, session: Session, *, principal: Principal | None = None, enforce_ownership: bool = False):
        if enforce_ownership and principal is None:
            raise ValueError("工单访问必须绑定用户")
        self.session = session
        self.principal = principal
        self.enforce_ownership = enforce_ownership

    def _scope(self, query: Select) -> Select:
        """Only administrators can see other owners' and pre-migration rows."""
        if self.enforce_ownership and self.principal.role != "admin":
            return query.where(Ticket.owner_user_id == self.principal.id)
        return query

    def create(self, payload: TicketCreate, *, creation_key: UUID | None = None) -> TicketRead:
        with trace_span("ticket.create", {"openinference.span.kind": "CHAIN", "ticket.has_creation_key": creation_key is not None}) as span:
            values = {"title": payload.title, "description": payload.description, "creation_key": creation_key}
            if self.enforce_ownership:
                values["owner_user_id"] = self.principal.id
            ticket = Ticket(**values)
            self.session.add(ticket)
            self.session.flush()  # Not committed until Database.transaction exits successfully.
            result = TicketRead.model_validate(ticket)
            span.set_attribute("ticket.id", str(result.id))
            span.set_attribute("status", "flushed")
            return result

    def get(self, ticket_id: UUID) -> TicketRead:
        with trace_span("ticket.get", {"openinference.span.kind": "CHAIN", "ticket.id": str(ticket_id)}):
            if self.enforce_ownership:
                ticket = self.session.scalar(self._scope(select(Ticket).where(Ticket.id == ticket_id)))
            else:
                ticket = self.session.get(Ticket, ticket_id)
            if ticket is None:
                raise TicketNotFoundError("工单不存在")
            return TicketRead.model_validate(ticket)

    def search(self, filters: TicketFilters) -> TicketList:
        with trace_span("ticket.search", {"openinference.span.kind": "CHAIN", "ticket.limit": filters.limit,
                        "ticket.offset": filters.offset, "ticket.has_keyword": filters.q is not None}) as span:
            tickets = self.session.scalars(self._scope(ticket_query(filters)))
            result = TicketList(items=tuple(TicketRead.model_validate(ticket) for ticket in tickets),
                                limit=filters.limit, offset=filters.offset)
            span.set_attribute("ticket.result_count", len(result.items))
            return result
