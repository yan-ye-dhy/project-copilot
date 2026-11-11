import test from 'node:test';
import assert from 'node:assert/strict';
import { jobExecution, readJobEvents, resumePublicJob } from '../src/project_copilot/web/assets/api.js';
import { jobObservation, jobStateLabel, canHideJob } from '../src/project_copilot/web/assets/task-center.js';

const ID = '11111111-1111-4111-8111-111111111111';
const execution = (attempt = 1, phase = 'running', extra = {}) => ({ attempt_no: attempt,
  event_floor: attempt === 1 ? 0 : 2, recovery_status: phase, blocked_reason: null,
  can_resume_waiting: !['completed', 'blocked'].includes(phase), ...extra });
const frame = (id, event, data) => `${id === null ? '' : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const sse = (...frames) => new Response(frames.join(''), { headers: { 'Content-Type': 'text/event-stream' } });

test('execution metadata accepts only safe phases, reasons and attempt identities', () => {
  const value = jobExecution({ ...execution(), execution_token: 'SECRET', lease_inode: 123, prompt: 'PRIVATE' });
  assert.deepEqual(value, execution());
  assert.ok(Object.isFrozen(value));
  for (const extra of [{ attempt_no: 3 }, { event_floor: -1 }, { recovery_status: 'PRIVATE' },
    { blocked_reason: 'PRIVATE' }, { can_resume_waiting: 'true' }]) assert.equal(jobExecution(execution(1, 'running', extra)), null);
});

test('old attempt terminal frames cannot stop the current stream before its delta or canonical GET', async () => {
  const current = execution(2, 'recovering'), seen = [];
  const stream = await readJobEvents(ID, { after: 2, execution: current, onEvent: (event) => seen.push(event),
    fetchImpl: async (_url, options) => {
      assert.equal(options.method, undefined);
      return sse(frame(2, 'terminal', { status: 'unknown', execution: execution() }),
        frame(null, 'snapshot', { status: 'succeeded', result: { answer: 'UNVALIDATED' } }),
        frame(2, 'terminal', { status: 'succeeded', execution: current }),
        frame(3, 'delta', { text: 'current draft', execution: current }));
    } });
  assert.equal(stream.terminal, false);
  assert.equal(stream.after, 3);
  assert.deepEqual(seen.map((event) => event.event), ['delta']);
  assert.doesNotMatch(JSON.stringify(seen), /UNVALIDATED|unknown/);
});

test('a live attempt change resets its cursor before new deltas and rejects late prior-attempt text', async () => {
  const seen = [];
  await readJobEvents(ID, { execution: execution(), onEvent: (event) => seen.push(event), fetchImpl: async () => sse(
    frame(1, 'delta', { text: 'old draft', execution: execution() }),
    frame(2, 'reset', { cursor_reset: true, execution: execution(2, 'recovering') }),
    frame(3, 'delta', { text: 'new draft', execution: execution(2, 'recovering') }),
    frame(4, 'delta', { text: 'late stale draft', execution: execution() })) });
  assert.deepEqual(seen.map((event) => event.event), ['delta', 'reset', 'delta']);
  assert.equal(seen[1].id, 2);
  assert.doesNotMatch(JSON.stringify(seen), /late stale/);
});

test('recovery polling and SSE return only the canonical GET answer and never issue POST', async () => {
  let reads = 0, streams = 0; const seen = [], paths = [];
  const result = await resumePublicJob(ID, { maxPolls: 4, useEvents: true, delay: async () => {},
    onEvent: (event) => seen.push(event), request: async (path, options = {}) => {
      assert.equal(options.method, undefined); paths.push(path); reads++;
      const status = reads === 1 ? 'unknown' : reads === 4 ? 'succeeded' : 'running';
      return { data: { status, execution: reads === 1 ? execution(1, 'awaiting_recovery')
        : execution(2, status === 'succeeded' ? 'completed' : 'recovering'),
        result: status === 'succeeded' ? { status: 'answered', answer: 'verified GET', sources: [] } : null } };
    }, eventReader: async (id, options) => {
      streams++;
      return readJobEvents(id, { ...options, fetchImpl: async () => sse(
        frame(2, 'terminal', { status: 'succeeded', result: { answer: 'UNVALIDATED_PROPOSAL' }, execution: execution() }),
        frame(3, 'delta', { text: 'current draft', execution: execution(2, 'recovering') })) });
    } });
  assert.equal(reads, 4); assert.equal(streams, 2);
  assert.ok(paths.every((path) => path === `/jobs/${ID}`));
  assert.equal(result.data.answer, 'verified GET');
  assert.deepEqual(result.data.sources, []);
  assert.ok(seen.some((event) => event.event === 'reset' && event.id === 2));
  assert.doesNotMatch(JSON.stringify(seen), /UNVALIDATED_PROPOSAL/);
});

test('blocked recovery stops waiting with a safe status and retains the original identifier', async () => {
  let reads = 0;
  await assert.rejects(resumePublicJob(ID, { request: async (_path, options = {}) => {
    assert.equal(options.method, undefined); reads++;
    return { data: { status: 'unknown', execution: execution(1, 'blocked', { blocked_reason: 'provider_uncertain' }) } };
  } }), (error) => error.meta.jobId === ID && error.meta.recoveryStatus === 'blocked' && /恢复已暂停/.test(error.message));
  assert.equal(reads, 1);
});

test('stop and resume during recovery perform reads only and do not create another task', async () => {
  const controller = new AbortController(); let reads = 0;
  await assert.rejects(resumePublicJob(ID, { signal: controller.signal, request: async (_path, options = {}) => {
    assert.equal(options.method, undefined); reads++;
    return { data: { status: 'unknown', execution: execution(1, 'awaiting_recovery') } };
  }, delay: async () => controller.abort() }), /停止等待/);
  const result = await resumePublicJob(ID, { request: async (_path, options = {}) => {
    assert.equal(options.method, undefined); reads++;
    return { data: { status: 'succeeded', execution: execution(2, 'completed'), result: { answer: 'same original task' } } };
  } });
  assert.equal(reads, 2); assert.equal(result.data.answer, 'same original task');
});

test('task center distinguishes dispatch, recovery and blocked status without persisting progress', () => {
  for (const [phase, label] of [['waiting_dispatch', '等待分发'], ['recovering', '正在恢复处理'], ['blocked', '恢复已暂停，请核实原任务']]) {
    const observation = jobObservation({ job_id: ID, status: phase === 'blocked' ? 'unknown' : 'running', execution: execution(2, phase) }, ID);
    assert.equal(jobStateLabel(observation), label);
    assert.doesNotMatch(JSON.stringify(observation), /token|inode|prompt/);
  }
  assert.equal(canHideJob(jobObservation({ status: 'unknown', execution: execution(1, 'awaiting_recovery') })), false);
  assert.equal(canHideJob(jobObservation({ status: 'unknown', execution: execution(1, 'blocked') })), true);
});
