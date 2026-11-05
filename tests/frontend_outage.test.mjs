/** Read retry budgets never authorize re-execution; no real service/model calls. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, apiRequest, readJobEvents, resumePublicJob, runPublicJob } from '../src/project_copilot/web/assets/api.js';

const ID = '11111111-1111-4111-8111-111111111111';
const response = (status, headers = {}, detail = 'untrusted upstream detail') => new Response(JSON.stringify({ detail }), {
  status, headers: { 'Content-Type': 'application/json', ...headers },
});

for (const status of [429, 502, 503, 504]) test(`POST ${status} is a single attempt with an honest rejection/uncertainty hint`, async () => {
  let calls = 0;
  await assert.rejects(runPublicJob('rag', { question: 'q' }, {
    request: (path, options) => apiRequest(path, { ...options, fetchImpl: async () => {
      calls++; return response(status, { 'Retry-After': '2', 'X-Run-ID': 'safe-request-id' });
    } }),
  }), (error) => {
    assert.equal(error.status, status); assert.equal(error.meta.phase, 'submission');
    assert.equal(error.meta.outcome, status === 429 ? 'rejected' : 'uncertain');
    assert.equal(error.meta.retryAfterMs, 2000); assert.equal(error.meta.runId, 'safe-request-id');
    assert.match(error.message, status === 429 ? /拒绝/ : /尚未核实/);
    assert.match(error.message, /没有自动重试/); assert.doesNotMatch(error.message, /untrusted|已取消|创建失败/);
    return true;
  });
  assert.equal(calls, 1);
});

test('499 or network loss after POST does not claim rejection or repeat the write', async () => {
  for (const status of [0, 499]) {
    let calls = 0;
    await assert.rejects(runPublicJob('agent', {}, { request: async () => {
      calls++;
      throw new ApiError('提交响应未确认。', { status, kind: status ? 'http' : 'network' });
    } }), (error) => error.meta.outcome === 'uncertain' && error.meta.phase === 'submission');
    assert.equal(calls, 1);
  }
});

test('known task GET429 honors Retry-After and then reads the same result without another POST', async () => {
  let posts = 0, gets = 0; const waits = [], events = [];
  const result = await runPublicJob('rag', {}, { random: () => 0, useEvents: false,
    delay: async (ms) => waits.push(ms), onEvent: (event) => events.push(event),
    request: async (path, options = {}) => {
      if (options.method === 'POST') { posts++; return { data: { job_id: ID } }; }
      assert.equal(path, `/jobs/${ID}`); gets++;
      if (gets === 1) throw new ApiError('limited', { status: 429, meta: { retryAfterMs: 5000 } });
      return { data: { status: 'succeeded', result: { answer: 'verified' } } };
    } });
  assert.equal(posts, 1); assert.equal(gets, 2); assert.deepEqual(waits, [5000]);
  assert.match(events[0].data.label, /状态查询暂受限/); assert.equal(result.data.answer, 'verified');
});

test('repeated GET502 stops at bounded read budget and preserves job_id as uncertain', async () => {
  let gets = 0; const waits = [];
  await assert.rejects(resumePublicJob(ID, { random: () => 0, delay: async (ms) => waits.push(ms),
    request: async () => { gets++; throw new ApiError('upstream unavailable', { status: 502 }); },
  }), (error) => {
    assert.equal(error.meta.jobId, ID); assert.equal(error.meta.phase, 'status');
    assert.equal(error.meta.outcome, 'uncertain'); assert.match(error.message, /后台任务可能仍在执行/);
    return true;
  });
  assert.equal(gets, 6); assert.deepEqual(waits, [2000, 4000, 8000, 16000, 30000]);
});

test('short ended SSE connections cannot create an unpaced reconnect loop', async () => {
  let gets = 0, streams = 0; const waits = [];
  const result = await resumePublicJob(ID, { useEvents: true, now: () => 0,
    request: async () => ({ data: ++gets < 4 ? { status: 'running' } : { status: 'succeeded', result: { answer: 'final' } } }),
    eventReader: async () => { streams++; return { after: 0, terminal: false }; },
    delay: async (ms) => waits.push(ms),
  });
  assert.equal(streams, 2); assert.equal(gets, 4); assert.deepEqual(waits, [2000, 2000, 2000]);
  assert.equal(result.data.answer, 'final');
});

test('SSE429 retains Retry-After for GET-only fallback scheduling', async () => {
  await assert.rejects(readJobEvents(ID, { fetchImpl: async () => response(429, { 'Retry-After': '7' }) }),
    (error) => error.status === 429 && error.meta.retryAfterMs === 7000);
});

test('Retry-After longer than wait budget retains job rather than retrying early', async () => {
  let reads = 0, delays = 0;
  await assert.rejects(resumePublicJob(ID, { maxWaitMs: 5000, now: () => 0,
    delay: async () => delays++, request: async () => {
      reads++; throw new ApiError('limited', { status: 429, meta: { retryAfterMs: 60000 } });
    } }), (error) => error.meta.jobId === ID && error.meta.outcome === 'uncertain');
  assert.equal(reads, 1); assert.equal(delays, 0);
});

test('stopping during GET backoff exits without refund, state mutation or a new submission', async () => {
  const signal = new AbortController(); let reads = 0;
  await assert.rejects(resumePublicJob(ID, { signal: signal.signal,
    request: async (path, options) => { assert.equal(path, `/jobs/${ID}`); assert.equal(options.method, undefined);
      reads++; throw new ApiError('limited', { status: 429 }); },
    delay: async () => signal.abort(),
  }), (error) => error.kind === 'aborted');
  assert.equal(reads, 1);
});

test('auth and ownership rejections never enter the transient GET retry path', async () => {
  for (const status of [401, 403, 404]) {
    let reads = 0;
    await assert.rejects(resumePublicJob(ID, { request: async () => {
      reads++; throw new ApiError('rejected', { status });
    }, delay: async () => assert.fail('must not retry') }), (error) => error.status === status && error.meta.jobId === ID);
    assert.equal(reads, 1);
  }
});
