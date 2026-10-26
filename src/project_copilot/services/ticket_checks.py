"""本地诊断：实际flush后主动失败，再用新事务确认未留下工单。无HTTP故障开关。"""

from uuid import uuid4

from project_copilot.db.models import Ticket
from project_copilot.db.session import Database
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.tickets import TicketService


class _ControlledRollback(Exception):
    pass


def verify_rollback(database: Database) -> dict:
    probe_id = None
    try:
        with database.transaction() as session:
            ticket = TicketService(session).create(TicketCreate(
                title="D20 rollback probe " + uuid4().hex,
                description="临时诊断：flush之后主动抛错，本事务必须回滚。",
            ))
            probe_id = ticket.id
            raise _ControlledRollback()
    except _ControlledRollback:
        pass
    with database.transaction() as session:
        absent = session.get(Ticket, probe_id) is None
    if not absent:
        raise RuntimeError("回滚诊断失败：数据库仍可见本次测试工单")
    return {"status": "ok", "probe_ticket_id": str(probe_id), "flushed_before_failure": True,
            "visible_after_rollback": False, "pool_checked_out_after": database.engine.pool.checkedout()}
