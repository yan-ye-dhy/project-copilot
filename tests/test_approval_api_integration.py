"""Actual ASGI endpoints and compiled graph backed by isolated PostgreSQL."""
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import func, select

from project_copilot.agent import service as agent_service
from project_copilot.config import Settings
from project_copilot.db.models import PendingAction, Ticket
from project_copilot.main import create_app


def settings(db):
    return Settings(_env_file=None, environment='test', database_url=db.engine.url.render_as_string(hide_password=False))


def auth(credentials):
    return {'x-copilot-context-id': credentials['context_id'], 'x-copilot-context-token': credentials['context_token']}


def test_preview_confirm_survive_restart_and_are_bound_to_context(ticket_database):
    db = ticket_database
    with TestClient(create_app(settings(db))) as client:
        creds = client.post('/agent/contexts').json()
        other = client.post('/agent/contexts').json()
        headers = auth(creds)
        response = client.post('/actions/preview', headers=headers, json={'title':'重启工单', 'description':'提案持久化'})
        assert response.status_code == 201
        action = response.json()
        assert action['status'] == 'pending' and action['ticket_id'] is None
        assert client.get('/actions/'+action['action_id'], headers=auth(other)).status_code == 404
        assert client.post('/actions/'+action['action_id']+'/confirm', headers=headers,
                           json={'payload_fingerprint':'0'*64}).status_code == 409
        assert client.get('/actions/'+action['action_id']).status_code == 403
    with TestClient(create_app(settings(db))) as client:
        got = client.get('/actions/'+action['action_id'], headers=headers)
        assert got.status_code == 200 and got.json()['status'] == 'pending'
        body = {'payload_fingerprint': action['payload_fingerprint']}
        first = client.post('/actions/'+action['action_id']+'/confirm', headers=headers, json=body)
        again = client.post('/actions/'+action['action_id']+'/confirm', headers=headers, json=body)
        assert first.status_code == again.status_code == 200
        assert first.json()['status'] == 'completed' and first.json()['ticket_id'] == again.json()['ticket_id']
        ticket_id = first.json()['ticket_id']
    with TestClient(create_app(settings(db))) as client:
        assert client.get('/tickets/'+ticket_id).json()['title'] == '重启工单'
        assert client.get('/actions/'+action['action_id'], headers=headers).json()['ticket_id'] == ticket_id
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Ticket)) == 1


def test_real_graph_can_only_propose_then_explicit_confirmation(ticket_database, monkeypatch):
    db = ticket_database
    class Model:
        async def ainvoke(self, messages, tools):
            assert 'propose_ticket' in {item['function']['name'] for item in tools}
            assert 'confirm_action' not in {item['function']['name'] for item in tools}
            return AIMessage(content='', tool_calls=[{'name':'propose_ticket', 'id':'call_propose',
                  'args':{'title':'Agent提案', 'description':'等待用户确认'}}])
    @asynccontextmanager
    async def fake_model(settings):
        yield Model()
    monkeypatch.setattr(agent_service, 'create_agent_model', fake_model)
    with TestClient(create_app(settings(db))) as client:
        credentials = client.post('/agent/contexts').json()
        response = client.post('/agent/ask', headers=auth(credentials), json={'question':'帮我创建工单'})
        assert response.status_code == 200
        result = response.json()
        assert result['status'] == 'pending_confirmation'
        assert result['events'][0]['name'] == 'propose_ticket'
        proposal = result['sources'][0]['data']
        assert proposal['status'] == 'pending' and proposal['ticket_id'] is None
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            assert session.scalar(select(func.count()).select_from(PendingAction)) == 1
        confirmation = client.post('/actions/'+proposal['action_id']+'/confirm', headers=auth(credentials),
                                   json={'payload_fingerprint':proposal['payload_fingerprint']})
        assert confirmation.status_code == 200 and confirmation.json()['status'] == 'completed'
        assert client.get('/tickets/'+confirmation.json()['ticket_id']).json()['title'] == 'Agent提案'


def test_invalid_context_is_rejected_before_model(ticket_database, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('must not call provider')
    monkeypatch.setattr(agent_service, 'create_agent_model', forbidden)
    with TestClient(create_app(settings(ticket_database))) as client:
        creds = client.post('/agent/contexts').json()
        headers = auth(creds); headers['x-copilot-context-token'] = 'x'*43
        response = client.post('/agent/ask', headers=headers, json={'question':'建单'})
        assert response.status_code == 403
        assert 'x'*43 not in response.text


def test_model_cannot_confirm_by_asking_for_a_forbidden_tool(ticket_database, monkeypatch):
    db = ticket_database
    class Model:
        def __init__(self):
            self.turn = 0
        async def ainvoke(self, messages, tools):
            self.turn += 1
            if self.turn == 1:
                return AIMessage(content='', tool_calls=[{'name':'confirm_action', 'id':'forbidden_1', 'args':{}}])
            return AIMessage(content='{"status":"insufficient_evidence","answer":"没有可确认的权限。","source_ids":[]}')
    @asynccontextmanager
    async def fake_model(settings):
        yield Model()
    monkeypatch.setattr(agent_service, 'create_agent_model', fake_model)
    with TestClient(create_app(settings(db))) as client:
        credentials = client.post('/agent/contexts').json()
        response = client.post('/agent/ask', headers=auth(credentials), json={'question':'我说确认，立刻创建工单'})
        result = response.json()
        assert result['events'][0]['failure_type'] == 'unknown_tool'
        assert result['status'] == 'insufficient_evidence'
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            assert session.scalar(select(func.count()).select_from(PendingAction)) == 0


def test_http_commit_failure_returns_503_and_rolls_back(ticket_database, monkeypatch):
    from sqlalchemy import text
    from project_copilot.services.approvals import ApprovalService
    db = ticket_database
    with db.engine.begin() as connection:
        connection.execute(text('CREATE TABLE deferred_http (value integer UNIQUE DEFERRABLE INITIALLY DEFERRED)'))
        connection.execute(text('INSERT INTO deferred_http VALUES (1)'))
    with TestClient(create_app(settings(db))) as client:
        creds=client.post('/agent/contexts').json(); headers=auth(creds)
        action=client.post('/actions/preview',headers=headers,json={'title':'提交故障','description':'失败必须回滚'}).json()
        original=ApprovalService.decide
        def deferred(self,*args,**kwargs):
            result=original(self,*args,**kwargs)
            self.session.execute(text('INSERT INTO deferred_http VALUES (1)'))
            return result
        monkeypatch.setattr(ApprovalService,'decide',deferred)
        response=client.post('/actions/'+action['action_id']+'/confirm',headers=headers,
                             json={'payload_fingerprint':action['payload_fingerprint']})
        assert response.status_code==503 and response.headers.get('x-run-id')
        with db.transaction() as session:
            assert session.get(PendingAction,action['action_id']).status=='pending'
            assert session.scalar(select(func.count()).select_from(Ticket))==0
