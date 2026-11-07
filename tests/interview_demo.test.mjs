import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { request as httpRequest } from 'node:http';
import { startDemo } from '../scripts/interview_demo.mjs';
import { CSRF } from '../scripts/interview-demo/fixtures.mjs';

async function fixture(t) {
  const demo = await startDemo({ port: 0, durationMs: 10 });
  t.after(() => demo.close());
  const request = async (path, { method = 'GET', body, headers = {} } = {}) => {
    const response = await fetch(demo.url + path, { method, headers: { ...(method === 'POST' ? { Origin: demo.url, 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF } : {}), ...headers }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    return { response, data: await response.json() };
  };
  return { demo, request, control: (path, body) => request(`/__demo/${path}`, { method: 'POST', body, headers: { 'X-Interview-Demo': 'local-only' } }) };
}

test('isolated build, explicit simulation, exact static assets, strict host/origin and no proxy routes', async t => {
  const { demo, request } = await fixture(t);
  const html = await (await fetch(demo.url)).text();
  assert.match(html, /本地模拟 · 零模型/);
  const app = await (await fetch(`${demo.url}/ui-assets/app.js`)).text();
  assert.equal(app, await readFile(new URL('../src/project_copilot/web/assets/app.js', import.meta.url), 'utf8'));
  assert.match(await (await fetch(`${demo.url}/ui-assets/api.js`)).text(), /const API_PREFIX = "\/api"/);
  assert.ok(demo.scratch.startsWith('/tmp/') || demo.scratch.includes('pc-interview-demo-'));
  for (const route of ['/api/proxy?url=https://example.com', '/.env', '/__demo/../../package.json', '/api/agent/ask']) assert.equal((await fetch(demo.url + route)).status, 404);
  assert.equal((await request('/api/jobs', { method: 'POST', body: {}, headers: { Origin: 'https://evil.invalid' } })).response.status, 403);
  assert.equal((await request('/__demo/reset', { method: 'POST', body: {} })).response.status, 403);
  const badHost = await new Promise((resolve, reject) => { const req = httpRequest(demo.url, { headers: { Host: 'evil.invalid' } }, res => { res.resume(); resolve(res.statusCode); }); req.on('error', reject); req.end(); });
  assert.equal(badHost, 403);
});

test('knowledge, proposal requires separate valid context and fingerprint, repeat confirm is idempotent', async t => {
  const { request, control } = await fixture(t);
  const submit = await request('/api/jobs', { method: 'POST', body: { kind: 'rag', payload: { question: 'fixture question' } } });
  assert.equal(submit.response.status, 202);
  await new Promise(resolve => setTimeout(resolve, 20));
  const knowledge = await request(`/api/jobs/${submit.data.job_id}`);
  assert.equal(knowledge.data.result.citations.length, 1);
  assert.match(knowledge.data.result.answer, /模拟/);
  await control('scenario', { scenario: 'proposal' });
  const context = (await request('/api/agent/contexts', { method: 'POST' })).data;
  const headers = { 'X-Copilot-Context-ID': context.context_id, 'X-Copilot-Context-Token': context.context_token };
  const job = (await request('/api/jobs', { method: 'POST', headers, body: { kind: 'agent', payload: { question: '确认' } } })).data;
  await new Promise(resolve => setTimeout(resolve, 20));
  const result = (await request(`/api/jobs/${job.job_id}`)).data.result;
  const action = result.sources.find(s => s.kind === 'proposal').data;
  assert.equal((await request('/__demo/state')).data.ticket_count, 1);
  const url = `/api/actions/${action.action_id}/confirm`;
  assert.equal((await request(url, { method: 'POST', body: { payload_fingerprint: action.payload_fingerprint } })).response.status, 403);
  assert.equal((await request(url, { method: 'POST', headers, body: { payload_fingerprint: '0'.repeat(64) } })).response.status, 409);
  const confirmed = (await request(url, { method: 'POST', headers, body: { payload_fingerprint: action.payload_fingerprint } })).data;
  assert.equal(confirmed.status, 'completed');
  assert.equal((await request(url, { method: 'POST', headers, body: { payload_fingerprint: action.payload_fingerprint } })).data.ticket_id, confirmed.ticket_id);
  assert.equal((await request('/__demo/state')).data.confirmations, 1);
  assert.equal((await request('/__demo/state')).data.ticket_count, 2);
});

test('failed job stays failed through read-only recovery; reset affects only synthetic records', async t => {
  const { demo, request, control } = await fixture(t);
  await control('scenario', { scenario: 'failure' });
  const job = (await request('/api/jobs', { method: 'POST', body: { kind: 'rag', payload: { question: 'fixture' } } })).data;
  const sse = await (await fetch(`${demo.url}/api/jobs/${job.job_id}/events`)).text();
  assert.match(sse, /模拟 SSE/); assert.match(sse, /event: terminal/);
  for (let n = 0; n < 3; n++) {
    const read = await request(`/api/jobs/${job.job_id}`);
    assert.equal(read.data.status, 'failed'); assert.equal(read.data.failure_type, 'tool_budget');
  }
  assert.equal((await request('/__demo/state')).data.submissions, 1);
  await control('reset', {});
  assert.equal((await request(`/api/jobs/${job.job_id}`)).response.status, 404);
  const state = (await request('/__demo/state')).data;
  assert.equal(state.submissions, 0); assert.equal(state.ticket_count, 1);
  await access(demo.scratch);
});
