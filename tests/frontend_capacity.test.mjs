import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ApiError, apiRequest, resumePublicJob, runPublicJob, readJobEvents,
  PERSONAL_JOB_WAIT_MS, personalDemoProfile, personalJobWaitOptions,
} from '../src/project_copilot/web/assets/api.js';

const ID = '11111111-1111-4111-8111-111111111111';
const profile = (overrides = {}) => ({
  expires_at: '2026-11-06T12:00:00Z', remaining_seconds: 86399, worker_enabled: true,
  job_limit: 500, jobs_used: 37, user_daily_limit: 1000, global_daily_limit: 2000,
  concurrent_limit: 3, occupied_slots: 2, ...overrides,
});

test('personal authorization display uses only approved fields and authoritative remaining time', () => {
  const value = personalDemoProfile(profile({ api_key: 'must-not-reach-display', cookie: 'private' }), 1000);
  assert.equal(value.job_limit, 500);
  assert.equal(value.jobs_used, 37);
  assert.equal(value.occupied_slots, 2);
  assert.equal(value.deadline_ms, 1000 + 86399 * 1000);
  assert.equal(value.api_key, undefined);
  assert.equal(value.cookie, undefined);
  assert.ok(Object.isFrozen(value));
  assert.deepEqual(personalJobWaitOptions(value), { maxWaitMs: PERSONAL_JOB_WAIT_MS });
  assert.deepEqual(personalJobWaitOptions(null), {});
});

test('invalid authorization display fields do not enable a personal-mode meter', () => {
  for (const value of [null, {}, profile({ remaining_seconds: 86401 }), profile({ job_limit: 501 }),
    profile({ concurrent_limit: 4 }), profile({ jobs_used: -1 }), profile({ expires_at: 'not-a-time' }),
    profile({ worker_enabled: 'yes' }), profile({ occupied_slots: 1.5 })]) {
    assert.equal(personalDemoProfile(value), null);
  }
  assert.equal(personalDemoProfile(profile({ remaining_seconds: 0 }), 100).deadline_ms, 100);
  assert.equal(personalDemoProfile(profile({ expires_at: '2026-11-05T00:00:00Z' }),
    Date.parse('2026-11-05T01:00:00Z')).deadline_ms, Date.parse('2026-11-05T00:00:00Z'));
});

test('three sequential long tasks can be waited for without another POST or long HTTP connection', async () => {
  let clock = 0, posts = 0, polls = 0, streams = 0;
  const request = async (path, options = {}) => {
    if (path === '/jobs') {
      posts += 1;
      assert.equal(options.method, 'POST');
      return { data: { job_id: ID } };
    }
    polls += 1;
    assert.equal(path, `/jobs/${ID}`);
    assert.equal(options.method, undefined);
    assert.ok(options.timeoutMs <= 15000);
    return { data: clock >= 46 * 60 * 1000
      ? { status: 'succeeded', result: { status: 'answered', answer: 'validated result' } }
      : { status: clock < 30 * 60 * 1000 ? 'reserved' : 'running' } };
  };
  const result = await runPublicJob('agent', { question: 'controlled request' }, {
    request, ...personalJobWaitOptions(personalDemoProfile(profile())), now: () => clock,
    delay: async (ms) => { clock += ms; }, useEvents: true,
    eventReader: async (id, options) => {
      assert.equal(id, ID);
      assert.ok(options.timeoutMs <= 27000);
      streams += 1; clock += 25000;
      return { after: streams, terminal: false };
    },
  });
  assert.equal(result.data.answer, 'validated result');
  assert.equal(posts, 1);
  assert.ok(streams > 50 && polls > 50);
  assert.ok(clock > 45 * 60 * 1000 && clock < 50 * 60 * 1000);
});

test('personal polling has enough read budget beyond the previous 240-poll ceiling', async () => {
  let clock = 0, polls = 0;
  const result = await resumePublicJob(ID, {
    ...personalJobWaitOptions(personalDemoProfile(profile())), now: () => clock,
    delay: async (ms) => { clock += ms; },
    request: async (path, options = {}) => {
      polls += 1;
      assert.equal(path, `/jobs/${ID}`);
      assert.equal(options.method, undefined);
      return { data: clock >= 16 * 60 * 1000
        ? { status: 'succeeded', result: { answer: 'done after 16 minutes' } } : { status: 'running' } };
    },
  });
  assert.equal(result.data.answer, 'done after 16 minutes');
  assert.ok(polls > 240);
});

test('ordinary waiting still stops after eight minutes and retains the existing identifier', async () => {
  let clock = 0;
  await assert.rejects(resumePublicJob(ID, {
    now: () => clock, delay: async (ms) => { clock += ms; },
    request: async () => ({ data: { status: 'running' } }),
  }), (error) => error.kind === 'timeout' && error.meta.jobId === ID);
  assert.equal(clock, 8 * 60 * 1000);
});

test('long personal wait can stop immediately and resume the original job without submitting work', async () => {
  const controller = new AbortController();
  let reads = 0;
  await assert.rejects(resumePublicJob(ID, {
    ...personalJobWaitOptions(personalDemoProfile(profile())), signal: controller.signal,
    request: async (path, options = {}) => {
      assert.equal(path, `/jobs/${ID}`); assert.equal(options.method, undefined);
      reads += 1; return { data: { status: 'running' } };
    },
    delay: async () => { controller.abort(); },
  }), (error) => error.kind === 'aborted');
  const resumed = await resumePublicJob(ID, {
    ...personalJobWaitOptions(personalDemoProfile(profile())),
    request: async (path, options = {}) => {
      assert.equal(path, `/jobs/${ID}`); assert.equal(options.method, undefined);
      reads += 1; return { data: { status: 'succeeded', result: { answer: 'same job' } } };
    },
  });
  assert.equal(resumed.data.answer, 'same job');
  assert.equal(reads, 2);
});

for (const type of ['model_budget', 'tool_budget', 'total_timeout', 'repeated_tool_loop', 'grant_ended']) {
  test(`confirmed failed ${type} preserves safe reason and job identifier without resubmission`, async () => {
    await assert.rejects(resumePublicJob(ID, {
      request: async () => ({ data: { status: 'failed', failure_type: type } }),
    }), (error) => error instanceof ApiError && error.meta.jobId === ID &&
      error.meta.jobStatus === 'failed' && error.meta.failureType === type && error.message.includes(type));
  });
}

test('unknown remains uncertain and an unapproved provider diagnostic is never displayed', async () => {
  const diagnostic = 'private-provider-content-secret';
  await assert.rejects(resumePublicJob(ID, {
    request: async () => ({ data: { status: 'unknown', failure_type: diagnostic } }),
  }), (error) => error.meta.jobStatus === 'unknown' && error.meta.jobId === ID &&
    !error.message.includes(diagnostic) && !JSON.stringify(error.meta).includes(diagnostic) && /待核实/.test(error.message));
});

for (const [reason, label] of [
  ['personal_demo_limit', '本次个人授权'], ['user_daily', '你的 UTC 每日'],
  ['global_daily', '全站 UTC 每日'], ['user_concurrency', '个人并发槽'], ['provider_attempts', '供应商调用'],
]) {
  test(`quota reason ${reason} is distinct, sanitized and never retried`, async () => {
    let calls = 0;
    await assert.rejects(apiRequest('/jobs', {
      method: 'POST', body: {}, fetchImpl: async () => {
        calls += 1;
        return new Response(JSON.stringify({ detail: 'private internal diagnostic', reason }), {
          status: 429, headers: { 'Content-Type': 'application/json' },
        });
      },
    }), (error) => error.meta.quotaReason === reason && error.message.includes(label) &&
      !error.message.includes('private internal diagnostic'));
    assert.equal(calls, 1);
  });
}

test('SSE connections cannot opt into an HTTP lifetime longer than the proxy short window', async () => {
  await assert.rejects(readJobEvents(ID, {
    timeoutMs: 900000, fetchImpl: async () => assert.fail('must not connect'),
  }), (error) => error.kind === 'configuration');
});
