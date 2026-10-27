"""Record real isolated PostgreSQL concurrency and rollback, without credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

from sqlalchemy import func, select

from project_copilot.db.migrations import upgrade
from project_copilot.db.models import PendingAction, Ticket
from project_copilot.db.session import Database
from project_copilot.schemas.tickets import TicketCreate
from project_copilot.services.approvals import ApprovalService
from project_copilot.services.tickets import TicketService
from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('拒绝覆盖')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with owned_test_database(TEST_DATABASE_URL) as url:
        db=Database(url)
        try:
            upgrade(db)
            def setup():
                with db.transaction() as s:
                    service=ApprovalService(s); credentials=service.create_context()
                    action=service.preview(service.authenticate(credentials.context_id,credentials.context_token),
                                           TicketCreate(title='隔离事务验收',description='真实并发'))
                    return credentials,action
            def decision(credentials,action,approve):
                with db.transaction() as s:
                    svc=ApprovalService(s)
                    return svc.decide(svc.authenticate(credentials.context_id,credentials.context_token),action.action_id,
                                      action.payload_fingerprint,approve=approve)
            c,a=setup(); gate=Barrier(8)
            def concurrent(_):
                gate.wait(timeout=10)
                return decision(c,a,True)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results=list(pool.map(concurrent,range(8)))
            with db.transaction() as s:
                count=s.scalar(select(func.count()).select_from(Ticket).where(Ticket.creation_key==a.action_id))
            assert count==1 and len({x.ticket_id for x in results})==1
            races=[]
            for _ in range(3):
                c2,a2=setup(); gate=Barrier(2)
                def race(approve):
                    gate.wait(timeout=10)
                    return decision(c2,a2,approve)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    result=list(pool.map(race,[True,False]))
                with db.transaction() as s:
                    n=s.scalar(select(func.count()).select_from(Ticket).where(Ticket.creation_key==a2.action_id))
                assert result[0].status==result[1].status and n==(1 if result[0].status=='completed' else 0)
                races.append({'action_id':str(a2.action_id),'status':result[0].status,'ticket_count':n})
            c3,a3=setup()
            try:
                with db.transaction() as s:
                    svc=ApprovalService(s); result=svc.decide(svc.authenticate(c3.context_id,c3.context_token),a3.action_id,a3.payload_fingerprint,approve=True)
                    assert result.ticket_id
                    raise RuntimeError('controlled transaction failure')
            except RuntimeError:
                pass
            with db.transaction() as s:
                state=s.get(PendingAction,a3.action_id).status
                n=s.scalar(select(func.count()).select_from(Ticket).where(Ticket.creation_key==a3.action_id))
            assert state=='pending' and n==0 and db.engine.pool.checkedout()==0
            report={'status':'ok','executor':'Codex','postgres':'real isolated test database',
                    'confirm':{'requests':8,'action_id':str(a.action_id),'unique_ticket_ids':len({x.ticket_id for x in results}),
                               'ticket_id':str(results[0].ticket_id),'database_count':count},
                    'confirm_reject_races':races,
                    'failure_after_flush':{'action_id':str(a3.action_id),'status_after_rollback':state,'ticket_count':n},
                    'checked_out_after':db.engine.pool.checkedout(), 'test_database_disposition':'only freshly created random test database dropped'}
        finally:
            db.close()
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
