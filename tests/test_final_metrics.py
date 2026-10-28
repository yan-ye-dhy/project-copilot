"""Metrics preserve failure denominators and unknown billing data."""
from scripts.final_metrics import distribution, summarize


def request(label, response):
    return {'label':label,'path':'/agent/ask','http_status':200,'elapsed_ms':10.0,'response':response}


def test_failed_or_unobserved_tasks_remain_in_final_scenario_denominator():
    report={'requests':[request('agent_combined',{'status':'failed','model_calls':1,'model_events':[
        {'model':'deepseek-flash','status':'error','usage':None}],
        'events':[{'name':'search_knowledge','status':'error','failure_type':'tool_timeout'}]})]}
    result=summarize(report,[])
    assert result['agent_tasks']['denominator']==2 and result['agent_tasks']['rate']==0
    assert result['tool_success']['rate']==0 and result['tool_success']['failure_types']=={'tool_timeout':1}
    assert result['llm']['reported_attempts']==1 and result['llm']['calls_with_usage']==0
    assert result['llm']['usage_complete'] is False
    assert result['llm']['usage'] is None
    assert result['cost_estimate']['deepseek_flash_peak'] is None


def test_usage_and_cost_use_only_reported_tokens_with_cache_coverage():
    usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
           'prompt_cache_hit_tokens':40,'prompt_cache_miss_tokens':60}
    result=summarize({'requests':[request('agent_combined',{'status':'answered','sources':[{'kind':'ticket'},{'kind':'knowledge'}],
        'events':[{'status':'ok'}],'model_calls':1,'model_events':[{'model':'deepseek-flash','usage':usage}]})]},[])
    assert result['llm']['usage']==usage
    assert result['cost_estimate']['deepseek_flash_peak']==(40*.006+60*.3+20*1.2)/1e6
    assert result['embedding']['prompt_tokens'] is None
    assert result['cost_estimate']['actual_account_bill'] is None


def test_missing_cache_usage_does_not_invent_zero_cost():
    usage={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
    result=summarize({'requests':[request('agent_combined',{'status':'failed','model_calls':1,
                 'model_events':[{'model':'deepseek-flash','usage':usage}],'events':[]})]},[])
    assert result['llm']['usage_complete'] is True
    assert result['cost_estimate']['deepseek_flash_peak'] is None
    assert result['tool_success']['rate'] is None


def test_small_sample_p95_is_explicit_nearest_rank():
    assert distribution([1,4,2])=={'samples':3,'median_ms':2,'p95_nearest_rank_ms':4}


def test_failed_rag_generation_span_preserves_unreported_attempt():
    report={'requests':[{'label':'rag_answer','path':'/rag/ask','http_status':502,'elapsed_ms':12,
                        'response':{'detail':'upstream failure'}}]}
    spans=[{'name':'rag.generate','start_time':'2026-10-28T11:00:00+00:00','end_time':'2026-10-28T11:00:01+00:00','attributes':{}}]
    result=summarize(report,spans)
    assert result['llm']['reported_attempts']==1
    assert result['llm']['calls_with_usage']==0
    assert result['llm']['usage_complete'] is False
