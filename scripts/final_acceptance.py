"""Explicit small live final demo; subsequent persistence checks never call models."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from urllib.parse import quote

import httpx
from sqlalchemy import create_engine, text


DOCUMENT = '''# 蓝鹤研发部署排障
蓝鹤服务切换环境后出现数据库连接失败时，应先检查运行进程的数据库连接配置，再检查PostgreSQL容器就绪、服务地址和端口。
数据库Healthy只能说明服务就绪，不能证明应用连接配置正确。问题排查参考标识为 PC-FINAL-4827。
已有工单反映实际处理状态；本知识文档提供排查建议，不授予任何创建或确认权限。新工单必须先生成提案，再通过独立确认接口执行。
'''


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))


def database_snapshot(url):
    engine = create_engine(url, hide_parameters=True, connect_args={'connect_timeout':5})
    try:
        with engine.connect() as connection:
            connection.execute(text('SET TRANSACTION READ ONLY'))
            rows = connection.execute(text('SELECT id,title,description,status,created_at,updated_at,creation_key FROM tickets ORDER BY id')).mappings()
            return {str(row['id']):sha256(canonical(dict(row)).encode()).hexdigest() for row in rows}
    finally:
        engine.dispose()


def qdrant_snapshot(client, collection):
    response=client.get('/collections/'+collection)
    if response.status_code==404:
        return {'exists':False}
    response.raise_for_status()
    points=[];offset=None
    while True:
        body={'limit':128,'with_payload':True,'with_vector':True}
        if offset is not None:body['offset']=offset
        result=client.post('/collections/'+collection+'/points/scroll',json=body)
        result.raise_for_status();page=result.json()['result'];points.extend(page['points'])
        offset=page.get('next_page_offset')
        if offset is None:break
        if len(points)>10000:raise ValueError('snapshot budget exceeded')
    points.sort(key=lambda p:str(p['id']))
    return {'exists':True,'point_count':len(points),'points_with_vectors_sha256':sha256(canonical(points).encode()).hexdigest()}


class Recorder:
    def __init__(self, folder, *, base_url, qdrant_url):
        self.folder=folder
        self.report_path=folder/'report.json'
        self.report=json.loads(self.report_path.read_text()) if self.report_path.exists() else {
            'status':'running','executor':'Codex automatic final acceptance','started_at':datetime.now(timezone.utc).isoformat(),
            'base_url':base_url,'qdrant_url':qdrant_url,'automatic_retries':0,'requests':[],
            'model_fee_actual_account_bill':None,'embedding_token_usage':None}
        self.client=httpx.Client(base_url=base_url,trust_env=False,timeout=310)
        self.qdrant=httpx.Client(base_url=qdrant_url,trust_env=False,timeout=20)
        self.credentials=None

    def save(self):
        serialized=json.dumps(self.report,ensure_ascii=False,indent=2)+'\n'
        if self.credentials and self.credentials['context_token'] in serialized:
            raise ValueError('credential leaked into report')
        self.report_path.write_text(serialized)

    def request(self, label, method, path, expected=200, *, private=False, **kwargs):
        started=time.monotonic()
        response=self.client.request(method,path,**kwargs)
        body=response.json()
        if private:
            self.credentials=body
            (self.folder/'credentials.json').write_text(json.dumps(body)+'\n')
            public={k:v for k,v in body.items() if k!='context_token'}
        else:public=body
        self.report['requests'].append({'label':label,'method':method,'path':path,'http_status':response.status_code,
              'elapsed_ms':(time.monotonic()-started)*1000,'run_id':response.headers.get('x-run-id'),
              'trace_id':response.headers.get('x-trace-id'),'trace_exported':response.headers.get('x-trace-exported'),
              'response':public})
        self.save()
        if response.status_code!=expected:raise RuntimeError(label+' unexpected HTTP status')
        if response.headers.get('x-trace-exported')!='true':raise RuntimeError(label+' trace export not acknowledged')
        return body

    def close(self):
        self.client.close();self.qdrant.close()


def run_demo(rec, database_url):
    rec.report['database_before']=database_snapshot(database_url)
    rec.report['existing_qdrant_before']=qdrant_snapshot(rec.qdrant,'pc_knowledge_dense_v0');rec.save()
    rec.request('health','GET','/health')
    doc=rec.request('upload_chinese','POST','/documents/upload',files={'file':('final-chinese.md',DOCUMENT.encode(),'text/markdown')})
    assert doc['mode']=='HYBRID_RERANK' and doc['chunk_count']>=1
    rec.report['document']=doc['document'];rec.save()
    rag=rec.request('rag_answer','POST','/rag/ask',json={'question':'蓝鹤服务切换环境后连接失败，应先检查什么？排查参考标识是什么？'})
    assert rag['status']=='answered' and 'PC-FINAL-4827' in rag['answer']
    assert any(c['metadata']['document_id']==doc['document']['document_id'] and 'PC-FINAL-4827' in c['text'] for c in rag['citations'])
    seed=rec.request('create_existing_ticket','POST','/tickets',expected=201,json={'title':'终验蓝鹤配置排查','description':'研发环境切换后连接失败，目前等待检查数据库连接配置。'})
    rec.report['seed_ticket']=seed;rec.save()
    detail=rec.request('read_existing_ticket','GET','/tickets/'+seed['id']);assert detail==seed
    listing=rec.request('search_existing_tickets','GET','/tickets',params={'q':'终验蓝鹤','status':'open'});assert any(item['id']==seed['id'] for item in listing['items'])
    question='请先用search_tickets将q设为终验蓝鹤查询真实工单，再用search_knowledge检索蓝鹤服务连接失败的排查建议，综合说明现状与下一步。请引用两类真实来源，不创建提案。'
    combined=rec.request('agent_combined','POST','/agent/ask',json={'question':question})
    assert combined['status']=='answered' and {'ticket','knowledge'}<={s['kind'] for s in combined['sources']}
    assert {'search_tickets','search_knowledge'}<={e['name'] for e in combined['events'] if e['status']=='ok'}
    creds=rec.request('create_context','POST','/agent/contexts',expected=201,private=True)
    headers={'X-Copilot-Context-ID':creds['context_id'],'X-Copilot-Context-Token':creds['context_token']}
    proposal=rec.request('agent_proposal','POST','/agent/ask',headers=headers,json={
        'question':'根据刚才分析，请使用propose_ticket生成待确认工单，标题为终验蓝鹤后续跟进，描述概括数据库连接配置排查建议。只生成提案，等待我明确确认。',
        'history':[{'role':'user','content':question},{'role':'assistant','content':combined['answer']}]})
    assert proposal['status']=='pending_confirmation'
    pending=next(s['data'] for s in proposal['sources'] if s['kind']=='proposal')
    assert pending['status']=='pending' and pending['ticket_id'] is None
    rec.report['pending_action']=pending;rec.save()
    before_confirm=database_snapshot(database_url)
    assert len(before_confirm)==len(rec.report['database_before'])+1
    preview=rec.request('read_preview','GET','/actions/'+pending['action_id'],headers=headers);assert preview['payload']==pending['payload']
    bad=rec.request('failed_confirmation','POST','/actions/'+pending['action_id']+'/confirm',expected=409,headers=headers,json={'payload_fingerprint':'0'*64})
    assert 'detail' in bad
    assert database_snapshot(database_url)==before_confirm
    confirmed=rec.request('confirm_proposal','POST','/actions/'+pending['action_id']+'/confirm',headers=headers,json={'payload_fingerprint':pending['payload_fingerprint']})
    repeated=rec.request('repeat_confirmation','POST','/actions/'+pending['action_id']+'/confirm',headers=headers,json={'payload_fingerprint':pending['payload_fingerprint']})
    assert confirmed['status']=='completed' and confirmed['ticket_id']==repeated['ticket_id']
    ticket=rec.request('read_created_ticket','GET','/tickets/'+confirmed['ticket_id'])
    assert ticket['title']==pending['payload']['title'] and ticket['description']==pending['payload']['description']
    after=database_snapshot(database_url);assert len(after)==len(before_confirm)+1
    assert all(after.get(k)==v for k,v in rec.report['database_before'].items())
    collection=os.environ['PROJECT_COPILOT_VERIFY_COLLECTION']
    rec.report.update(created_ticket=ticket,database_before_recreation=after,
        qdrant_before_recreation=qdrant_snapshot(rec.qdrant,collection),collection=collection,
        existing_qdrant_before_recreation=qdrant_snapshot(rec.qdrant,'pc_knowledge_dense_v0'),status='awaiting_container_recreation')
    rec.save()


def persistence(rec,database_url):
    assert rec.report['status']=='awaiting_container_recreation'
    rec.request('health_after_recreation','GET','/health')
    for key in ('seed_ticket','created_ticket'):
        expected=rec.report[key];got=rec.request(key+'_after_recreation','GET','/tickets/'+expected['id']);assert got==expected
    assert database_snapshot(database_url)==rec.report['database_before_recreation']
    assert qdrant_snapshot(rec.qdrant,rec.report['collection'])==rec.report['qdrant_before_recreation']
    assert qdrant_snapshot(rec.qdrant,'pc_knowledge_dense_v0')==rec.report['existing_qdrant_before_recreation']==rec.report['existing_qdrant_before']
    rec.report.update(database_persistent_after_recreation=True,qdrant_persistent_after_recreation=True,
                      existing_data_preserved=True,status='business_and_persistence_verified')
    rec.save()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase',choices=['run','persistence'])
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--base-url',default='http://127.0.0.1:8002')
    parser.add_argument('--qdrant-url',default='http://127.0.0.1:6433')
    args=parser.parse_args()
    if args.phase=='run' and not args.live:parser.error('live model calls require --live')
    if args.phase=='run' and args.output_dir.exists():parser.error('refuse to overwrite output directory')
    os.umask(0o077)
    if args.phase=='run':args.output_dir.mkdir(parents=True)
    if not args.output_dir.is_dir():parser.error('existing report required')
    rec=Recorder(args.output_dir,base_url=args.base_url,qdrant_url=args.qdrant_url)
    try:
        database_url=os.environ['PROJECT_COPILOT_VERIFY_DATABASE_URL']
        if args.phase=='run':run_demo(rec,database_url)
        else:persistence(rec,database_url)
    except Exception as exc:
        rec.report.update(status='failed',failed_phase=args.phase,error_type=type(exc).__name__);rec.save()
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'report':str(rec.report_path)}))
        return 1
    finally:rec.close()
    print(json.dumps({'status':rec.report['status'],'requests':len(rec.report['requests']),'report':str(rec.report_path)}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
