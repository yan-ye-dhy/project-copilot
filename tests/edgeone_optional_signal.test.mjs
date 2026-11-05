import test from 'node:test';
import assert from 'node:assert/strict';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const origin = 'https://copilot.example';
const backend = 'https://signal-hotfix-test.trycloudflare.com';
const env = {
  PROJECT_COPILOT_SITE_ORIGIN: origin,
  PROJECT_COPILOT_BACKEND_ORIGIN: backend,
  PROJECT_COPILOT_PROXY_SECRET: 'synthetic-hotfix-secret-'.padEnd(48, 'x'),
};
// A plain runtime request is deliberate: WHATWG Request always supplies signal.
const runtimeRequest = () => ({
  url: 'http://runtime.internal/api/health', method: 'GET',
  headers: new Headers({ 'Sec-Fetch-Site': 'same-origin' }), body: null,
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

for (const kind of ['absent', 'null']) {
  test(`configured GET succeeds and stream cleanup works with ${kind} client signal`, async () => {
    const request = runtimeRequest();
    if (kind === 'null') request.signal = null;
    else assert.equal(Object.hasOwn(request, 'signal'), false);
    let calls = 0;
    const handler = createProxyHandler({ fetchImpl: async (url, init) => {
      calls += 1;
      assert.equal(url, `${backend}/health`);
      assert(init.signal instanceof AbortSignal);
      assert.equal(init.signal.aborted, false);
      return Response.json({ status: 'ok' });
    } });
    const response = await handler({ request, env });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: 'ok' }); // Exercises cleanup.
    assert.equal(calls, 1);
  });
}

test('missing client signal permits bodyless response cleanup', async () => {
  const handler = createProxyHandler({ fetchImpl: async () => new Response(null, { status: 204 }) });
  const response = await handler({ request: runtimeRequest(), env });
  assert.equal(response.status, 204);
  assert.equal(await response.text(), '');
});

test('missing client signal handles upstream failure without throwing or retrying', async () => {
  let calls = 0;
  const handler = createProxyHandler({ fetchImpl: async () => {
    calls += 1;
    throw new Error(`private ${env.PROJECT_COPILOT_PROXY_SECRET}`);
  } });
  const response = await handler({ request: runtimeRequest(), env });
  assert.equal(response.status, 502);
  assert.equal(calls, 1);
  assert(!(await response.text()).includes(env.PROJECT_COPILOT_PROXY_SECRET));
});

test('missing client signal retains internal timeout and upstream abort', async () => {
  let upstreamSignal, calls = 0;
  const handler = createProxyHandler({ timeoutMs: 10, fetchImpl: async (_url, init) => {
    calls += 1;
    upstreamSignal = init.signal;
    await sleep(50);
    return Response.json({ status: 'late' });
  } });
  const response = await handler({ request: runtimeRequest(), env });
  assert.equal(response.status, 504);
  assert.equal(upstreamSignal.aborted, true);
  assert.equal(calls, 1);
  await response.text();
});

test('provided client signal is detached after response consumption', async () => {
  const client = new AbortController();
  let upstreamSignal;
  const handler = createProxyHandler({ fetchImpl: async (_url, init) => {
    upstreamSignal = init.signal;
    return Response.json({ status: 'ok' });
  } });
  const request = { ...runtimeRequest(), signal: client.signal };
  const response = await handler({ request, env });
  assert.deepEqual(await response.json(), { status: 'ok' });
  client.abort();
  assert.equal(upstreamSignal.aborted, false);
});

test('already-aborted client signal returns 499 without starting upstream', async () => {
  const client = new AbortController();
  client.abort();
  const handler = createProxyHandler({ fetchImpl: () => assert.fail('aborted request must not fetch') });
  const response = await handler({ request: { ...runtimeRequest(), signal: client.signal }, env });
  assert.equal(response.status, 499);
  await response.text();
});
