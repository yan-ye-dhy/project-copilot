"""Query actual Phoenix spans and summarize explicitly bounded final-demo metrics."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import time
from urllib.parse import quote

import httpx


def attribute(span, key):
    value=span.get('attributes',{})
    if key in value:return value[key]
    for component in key.split('.'):
        if not isinstance(value,dict) or component not in value:return None
        value=value[component]
    return value


def distribution(values):
    values=sorted(values)
    return {'samples':len(values),'median_ms':statistics.median(values) if values else None,
            'p95_nearest_rank_ms':values[math.ceil(.95*len(values))-1] if values else None}


def summarize(report, spans):
    requests=report['requests'];by_label={item['label']:item for item in requests}
    agent_requests=[x for x in requests if x['path']=='/agent/ask']
    events=[event for req in agent_requests for event in req['response'].get('events',[])]
    successes=sum(e['status']=='ok' for e in events)
    failure_types=Counter(e.get('failure_type') or 'unknown' for e in events if e['status']!='ok')
    tasks=[]
    for name in ['agent_combined','agent_proposal']:
        result=by_label.get(name,{}).get('response',{})
        if name=='agent_combined':
            passed=result.get('status')=='answered' and {'ticket','knowledge'}<={s['kind'] for s in result.get('sources',[])}
        else:
            confirm=by_label.get('confirm_proposal',{}).get('response',{})
            repeat=by_label.get('repeat_confirmation',{}).get('response',{})
            passed=(result.get('status')=='pending_confirmation' and confirm.get('status')=='completed'
                    and confirm.get('ticket_id') is not None and confirm.get('ticket_id')==repeat.get('ticket_id'))
        tasks.append({'name':name,'observed':name in by_label,'passed':passed})
    model_usages=[];attempts=0;all_flash=True
    for req in requests:
        data=req['response']
        if req['path']=='/agent/ask':
            attempts+=data.get('model_calls',0)
            for event in data.get('model_events',[]):
                all_flash=all_flash and event.get('model')=='deepseek-flash'
                if event.get('usage') is not None:model_usages.append(event['usage'])
        elif req['path']=='/rag/ask' and data.get('generation'):
            attempts+=1;generation=data['generation'];all_flash=all_flash and generation.get('actual_model')=='deepseek-flash'
            if generation.get('usage') is not None:model_usages.append(generation['usage'])
    # Failed HTTP responses can omit generation metadata even after an attempt.
    # Keep the observed generation/decision spans in the denominator.
    observed_attempts=sum(s['name']=='rag.generate' or
        (s['name']=='agent.model' and attribute(s,'agent.model.step') is not None) for s in spans)
    attempts=max(attempts,observed_attempts)
    totals=({key:sum(u[key] for u in model_usages) for key in ['prompt_tokens','completion_tokens','total_tokens']}
            if model_usages else None)
    cache_complete=bool(model_usages) and all(all(key in u for key in ['prompt_cache_hit_tokens','prompt_cache_miss_tokens']) for u in model_usages)
    peak=None
    if cache_complete and len(model_usages)==attempts and all_flash:
        totals.update({key:sum(u[key] for u in model_usages) for key in ['prompt_cache_hit_tokens','prompt_cache_miss_tokens']})
        peak=(totals['prompt_cache_hit_tokens']*.006+totals['prompt_cache_miss_tokens']*.3+totals['completion_tokens']*1.2)/1e6
    durations=defaultdict(list)
    for span in spans:
        duration=(datetime.fromisoformat(span['end_time'])-datetime.fromisoformat(span['start_time'])).total_seconds()*1000
        durations[span['name']].append(duration)
    embeddings=[s for s in spans if s['name']=='embedding.request']
    embedding_usage=[attribute(s,'usage.prompt_tokens') for s in embeddings]
    embedding_tokens=sum(embedding_usage) if embeddings and all(type(x) is int for x in embedding_usage) else None
    return {'sample_scope':'One synthetic final demonstration, not a benchmark or production SLA',
        'tool_success':{'successful':successes,'attempt_events':len(events),'rate':successes/len(events) if events else None,'failure_types':dict(failure_types)},
        'agent_tasks':{'scope':'Two planned demo scenarios; unobserved scenes are not counted as successes',
                       'tasks':tasks,'successful':sum(x['passed'] for x in tasks),'denominator':len(tasks),'rate':sum(x['passed'] for x in tasks)/len(tasks)},
        'llm':{'reported_attempts':attempts,'calls_with_usage':len(model_usages),'usage':totals,'usage_complete':len(model_usages)==attempts},
        'embedding':{'request_spans':len(embeddings),'calls_with_prompt_usage':sum(type(x) is int for x in embedding_usage),'prompt_tokens':embedding_tokens},
        'http':{'requests':len(requests),'by_status':dict(Counter(str(r['http_status']) for r in requests)),'latency':distribution([r['elapsed_ms'] for r in requests])},
        'stages':{name:distribution(values) for name,values in sorted(durations.items())},
        'timing_boundary':'Stage spans are nested and overlapping; do not sum parent and child durations. API timings include trace-export acknowledgement.',
        'cost_estimate':{'currency':'USD','deepseek_flash_peak':peak,'deepseek_flash_off_peak':peak/2 if peak is not None else None,
                         'embedding_cny':embedding_tokens*.5/1e6 if embedding_tokens is not None else None,
                         'actual_account_bill':None,'price_date':'2026-10-28',
                         'sources':['https://api-docs.deepseek.com/quick_start/pricing/','https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3'],
                         'boundary':'Published-rate estimates based on available usage; not the account invoice. Peak/off-peak shown as a range.'}}


def collect(report, endpoint, project):
    expected={r['trace_id']:r['run_id'] for r in report['requests'] if r.get('trace_id') and r.get('trace_exported')=='true'}
    with httpx.Client(base_url=endpoint,trust_env=False,timeout=20) as client:
        for attempt in range(12):
            spans=[];cursor=None
            for page in range(10):
                params={'limit':1000}
                if cursor:params['cursor']=cursor
                response=client.get('/v1/projects/'+quote(project,safe='')+'/spans',params=params)
                response.raise_for_status();body=response.json();spans.extend(body['data']);cursor=body.get('next_cursor')
                if not cursor:break
            selected=[s for s in spans if s['context']['trace_id'] in expected]
            found={s['context']['trace_id'] for s in selected}
            if found==set(expected):break
            time.sleep(.25)
    if found!=set(expected):raise ValueError('collector is missing acknowledged traces')
    for trace_id,run_id in expected.items():
        group=[s for s in selected if s['context']['trace_id']==trace_id]
        ids={s['context']['span_id'] for s in group}
        assert any(s['parent_id'] is None for s in group)
        assert all(s['parent_id'] is None or s['parent_id'] in ids for s in group)
        assert all(attribute(s,'copilot.run_id')==run_id for s in group)
    by_label={r['label']:r for r in report['requests']}
    success_id=by_label['agent_combined']['trace_id'];failure_id=by_label['failed_confirmation']['trace_id']
    success=[s for s in selected if s['context']['trace_id']==success_id]
    failed=[s for s in selected if s['context']['trace_id']==failure_id]
    assert {'agent.model','agent.tool.execute','rag.dense','rag.bm25','rag.rrf','rag.rerank','ticket.search'}<={s['name'] for s in success}
    assert any(s['status_code']=='ERROR' for s in failed)
    assert any(s['name']=='db.transaction' and attribute(s,'transaction.state')=='rolled_back' for s in failed)
    return selected,{'project':project,'collector_trace_count':len(expected),'collector_span_count':len(selected),
        'success_trace_id':success_id,'failure_trace_id':failure_id,
        'success_span_names':[s['name'] for s in success],
        'failure_errors':[{'name':s['name'],'error_type':attribute(s,'error.type'),'transaction_state':attribute(s,'transaction.state')} for s in failed if s['status_code']=='ERROR'],
        'parent_child_and_run_id_verified':True,'collector_data_is_real_not_internal_log':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--phoenix-url',default='http://127.0.0.1:6007')
    parser.add_argument('--project',required=True)
    args=parser.parse_args()
    report=json.loads((args.output_dir/'report.json').read_text())
    if report['status']!='business_and_persistence_verified':parser.error('business and persistence acceptance must pass first')
    spans,trace_summary=collect(report,args.phoenix_url,args.project)
    credentials=json.loads((args.output_dir/'credentials.json').read_text())
    serialized=json.dumps(spans,ensure_ascii=False,indent=2)+'\n'
    if credentials['context_token'] in serialized:raise ValueError('credential found in trace')
    (args.output_dir/'phoenix_spans.json').write_text(serialized)
    metrics=summarize(report,spans)
    (args.output_dir/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2)+'\n')
    (args.output_dir/'trace_verification.json').write_text(json.dumps(trace_summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':'verified','traces':trace_summary['collector_trace_count'],'spans':len(spans),'tool_rate':metrics['tool_success']['rate'],'agent_task_rate':metrics['agent_tasks']['rate']}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
