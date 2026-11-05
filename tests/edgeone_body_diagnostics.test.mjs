import test from 'node:test';
import assert from 'node:assert/strict';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const sentinel = 'private-password-cookie-token-secret';
const env = {
  PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example',
  PROJECT_COPILOT_BACKEND_ORIGIN: 'https://diagnostic-test.trycloudflare.com',
  PROJECT_COPILOT_PROXY_SECRET: 'synthetic-diagnostic-'.padEnd(48, 'x'),
};

test('response policy failure logs the response stage without weakening the policy', async t => {
  const logs = [];
  t.mock.method(console, 'warn', line => logs.push(JSON.parse(line)));
  const request = { url: 'http://runtime.internal/api/health', method: 'GET', headers: new Headers(), body: null };
  const response = await createProxyHandler({ fetchImpl: async () => new Response('private', {
    headers: { 'content-encoding': 'gzip' },
  }) })({ request, env });
  assert.equal(response.status, 502);
  assert.deepEqual(logs, [{ event: 'pc_proxy_body_v1', stage: 'response', error_type: 'ProxyFailure' }]);
  assert(!(await response.text()).includes('private'));
});

for (const stage of ['read_body', 'fetch_upstream', 'response']) {
  test(`temporary ${stage} diagnostic exposes only fixed stage and safe error type`, async t => {
    const logs = [];
    t.mock.method(console, 'warn', line => logs.push(line));
    const failure = new TypeError(sentinel);
    if (stage === 'response') failure.name = sentinel; // Untrusted names stay generic.
    const request = {
      url: `http://runtime.internal/api/auth/login?eo_token=${sentinel}`,
      method: 'POST',
      headers: new Headers({ origin: env.PROJECT_COPILOT_SITE_ORIGIN,
        cookie: `__Host-pc_session=${sentinel}`, 'content-type': 'application/json' }),
      body: stage === 'read_body' ? { getReader() { throw failure; } } : Buffer.from(`{"password":"${sentinel}"}`),
    };
    let calls = 0;
    const response = await createProxyHandler({ fetchImpl: async () => {
      calls += 1;
      if (stage === 'fetch_upstream') throw failure;
      return { status: 200, headers: { get() { throw failure; } }, body: null };
    } })({ request, env });
    assert.equal(response.status, 502);
    assert.equal(calls, stage === 'read_body' ? 0 : 1);
    assert.deepEqual(logs.map(line => JSON.parse(line)), [{ event: 'pc_proxy_body_v1', stage,
      error_type: stage === 'response' ? 'Error' : 'TypeError' }]);
    assert(!logs.join('').includes(sentinel));
    assert(!logs.join('').includes(env.PROJECT_COPILOT_BACKEND_ORIGIN));
    assert(!logs.join('').includes(env.PROJECT_COPILOT_PROXY_SECRET));
    assert(!(await response.text()).includes(sentinel));
  });
}
