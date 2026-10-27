"""One isolated real Agent proposal/confirmation using PostgreSQL, Hybrid and GPU."""
import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import json
from pathlib import Path

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select

from project_copilot.agent import service as agent_service
from project_copilot.config import load_settings
from project_copilot.db.migrations import upgrade
from project_copilot.db.models import PendingAction, Ticket
from project_copilot.db.session import Database
from project_copilot.main import create_app
from project_copilot.rag import final_retrieval
from scripts.postgres_support import TEST_DATABASE_URL, owned_test_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('需要明确 --live')
    if args.output.exists():
        parser.error('拒绝覆盖现有结果')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts = {'chat': 0, 'embedding': 0}
    receipt = {'status':'running', 'executor':'Codex', 'calls':counts, 'automatic_retries':0,
               'database':'new isolated test database', 'actual_account_cost':None,
               'source_sha256':{str(p):sha256(p.read_bytes()).hexdigest() for p in [
                   Path('src/project_copilot/agent/graph.py'), Path('src/project_copilot/agent/service.py'),
                   Path('src/project_copilot/agent/proposals.py'), Path('src/project_copilot/api/actions.py'),
                   Path('src/project_copilot/services/approvals.py'), Path('src/project_copilot/rag/final_retrieval.py'),
                   Path('migrations/versions/0002_approvals.py')]}}
    def save():
        args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
    original_embedding = final_retrieval.create_embedding_client
    @contextmanager
    def embedding(settings):
        with original_embedding(settings) as actual:
            class MeteredEmbedding:
                model = actual.model
                expected_dimensions = actual.expected_dimensions
                def embed_query(self, value):
                    counts['embedding'] += 1
                    return actual.embed_query(value)
            yield MeteredEmbedding()
    final_retrieval.create_embedding_client = embedding
    original = agent_service.create_agent_model
    @asynccontextmanager
    async def model(settings):
        async with original(settings) as actual:
            class Metered:
                async def ainvoke(self, messages, tools):
                    counts['chat'] += 1
                    return await actual.ainvoke(messages, tools)
            yield Metered()
    agent_service.create_agent_model = model
    try:
        with owned_test_database(TEST_DATABASE_URL) as url:
            db = Database(url)
            try:
                upgrade(db)
                settings = load_settings().model_copy(update={
                    'database_url':SecretStr(url),
                    'qdrant_collection_name':'pc_test_final_live_902ed2ba74124fbe9b1c22982644cbfa',
                    'manifest_dir':Path('data/auto-d24-final/live-accepted/manifests').resolve(),
                    'deepseek_max_tokens':512,
                })
                async def run():
                    app = create_app(settings)
                    async with app.router.lifespan_context(app):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', timeout=300) as client:
                            seed = await client.post('/tickets',json={'title':'蓝鹤既有工单', 'description':'蓝鹤测试环境已有任务'})
                            assert seed.status_code == 201
                            seed_id = seed.json()['id']
                            context = await client.post('/agent/contexts')
                            assert context.status_code == 201
                            cred = context.json()
                            headers = {'x-copilot-context-id':cred['context_id'], 'x-copilot-context-token':cred['context_token']}
                            q = '请先用 search_tickets 查询蓝鹤，再用 search_knowledge 查询蓝鹤协议识别码。最后用 propose_ticket 为蓝鹤问题生成待确认提案，标题蓝鹤验收，描述需要排查蓝鹤协议。不要直接创建工单。'
                            response = await client.post('/agent/ask', headers=headers, json={'question':q})
                            assert response.status_code == 200
                            result=response.json()
                            receipt['agent'] = result
                            save()
                            assert result['status'] == 'pending_confirmation'
                            names = [e['name'] for e in result['events'] if e['status']=='ok']
                            assert {'search_tickets','search_knowledge','propose_ticket'} <= set(names)
                            proposal = next(s['data'] for s in result['sources'] if s['kind']=='proposal')
                            assert proposal['status'] == 'pending' and proposal['ticket_id'] is None
                            with db.transaction() as session:
                                assert session.scalar(select(func.count()).select_from(Ticket)) == 1
                                assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
                            receipt['before_confirmation'] = {'existing_ticket_id':seed_id, 'ticket_count':1, 'proposal_count':1}
                    app2=create_app(settings)
                    async with app2.router.lifespan_context(app2):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2),base_url='http://test') as client:
                            base='/actions/'+proposal['action_id']
                            assert (await client.get(base, headers=headers)).json()['status']=='pending'
                            body={'payload_fingerprint':proposal['payload_fingerprint']}
                            done=await client.post(base+'/confirm',headers=headers,json=body)
                            repeated=await client.post(base+'/confirm',headers=headers,json=body)
                            assert done.status_code == repeated.status_code == 200
                            assert done.json()['status']=='completed' and done.json()['ticket_id']==repeated.json()['ticket_id']
                            actual=await client.get('/tickets/'+done.json()['ticket_id'])
                            assert actual.status_code==200 and actual.json()['title']==proposal['payload']['title']
                            receipt['after_confirmation']={'action':done.json(),'ticket':actual.json(), 'repeat_same_id':True,
                                                           'pending_survived_application_restart':True}
                    app3=create_app(settings)
                    async with app3.router.lifespan_context(app3):
                        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app3),base_url='http://test') as client:
                            assert (await client.get('/tickets/'+receipt['after_confirmation']['ticket']['id'])).status_code==200
                            receipt['ticket_survived_application_restart']=True
                    receipt.update(status='ok', hybrid='shared final retriever; real Qdrant and CUDA model',
                                   embedding_usage='not collected', retained_qdrant_collection=settings.qdrant_collection_name)
                asyncio.run(run())
            finally:
                db.close()
    except Exception as exc:
        receipt.update(status='failed', error_type=type(exc).__name__)
        save()
        print(json.dumps({'status':'error','type':type(exc).__name__,'calls':counts}))
        return 1
    save()
    print(json.dumps({'status':'ok','calls':counts}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
