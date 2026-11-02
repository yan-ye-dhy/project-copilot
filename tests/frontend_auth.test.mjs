import test, { afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Exercise the exact public-build source without filesystem writes or a server.
const source = await readFile(new URL('../src/project_copilot/web/assets/api.js', import.meta.url), 'utf8');
const marker = 'const API_PREFIX = ""; // EDGEONE_API_PREFIX';
assert.equal(source.split(marker).length, 2);
const built = source.replace(marker, 'const API_PREFIX = "/api"; // EDGEONE_API_PREFIX');
const client = await import(`data:text/javascript;base64,${Buffer.from(built).toString('base64')}`);
const token = 'a'.repeat(64);
const json = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { client.setCsrfToken(null); client.onAuthenticationFailure(null); });

test('public build sends login and registration to same-origin proxy without CSRF', async () => {
  assert.equal(client.PUBLIC_BUILD, true);
  const calls = [];
  for (const path of ['/auth/login', '/auth/register']) {
    await client.apiRequest(path, { method: 'POST', body: { username: 'demo', password: 'test-only-password' },
      fetchImpl: async (url, options) => { calls.push({ url, options }); return json({ status: 'ok' }); } });
  }
  assert.deepEqual(calls.map((call) => call.url), ['/api/auth/login', '/api/auth/register']);
  for (const { options } of calls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['X-CSRF-Token'], undefined);
  }
});

test('unauthenticated mutations fail before network while reads remain allowed', async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; return json({ status: 'ok' }); };
  await assert.rejects(client.apiRequest('/tickets', { method: 'POST', body: {}, fetchImpl }), (error) => error.status === 401);
  assert.equal(calls, 0);
  await client.apiRequest('/tickets', { fetchImpl });
  assert.equal(calls, 1);
});

test('mutations use memory CSRF and override any caller-provided variant', async () => {
  client.setCsrfToken(token);
  const seen = [];
  for (const [path, method] of [['/agent/ask', 'POST'], ['/documents/upload', 'POST'], ['/actions/id', 'DELETE'], ['/auth/logout', 'POST']]) {
    await client.apiRequest(path, { method, headers: { 'x-csrf-token': 'untrusted' },
      fetchImpl: async (_url, options) => { seen.push(options.headers); return json({ status: 'ok' }); } });
  }
  for (const headers of seen) {
    assert.equal(headers['X-CSRF-Token'], token);
    assert.equal(headers['x-csrf-token'], undefined);
  }
  await client.apiRequest('/tickets', { fetchImpl: async (_url, options) => {
    assert.equal(options.headers['X-CSRF-Token'], undefined); return json({ items: [] });
  } });
});

test('CSRF validation accepts only fixed lowercase hex and clearing prevents reuse', async () => {
  for (const invalid of ['', 'a'.repeat(63), 'A'.repeat(64), 'x'.repeat(64), 123, undefined]) {
    assert.throws(() => client.setCsrfToken(invalid), TypeError);
  }
  client.setCsrfToken(token); client.setCsrfToken(null);
  await assert.rejects(client.apiRequest('/agent/contexts', { method: 'POST', fetchImpl: async () => assert.fail('must not call network') }),
    (error) => error.status === 401);
});

test('expired session signals once without automatic retry', async () => {
  client.setCsrfToken(token);
  let attempts = 0; let signals = 0;
  client.onAuthenticationFailure(() => { signals += 1; client.setCsrfToken(null); });
  await assert.rejects(client.apiRequest('/agent/ask', { method: 'POST', body: { question: 'q' },
    fetchImpl: async () => { attempts += 1; return json({ detail: 'expired' }, 401); } }), (error) => error.status === 401);
  assert.equal(attempts, 1); assert.equal(signals, 1);
  await assert.rejects(client.apiRequest('/agent/ask', { method: 'POST', body: {},
    fetchImpl: async () => assert.fail('must not retry') }), (error) => error.status === 401);
});

test('a late 401 from an older session cannot invalidate a newer login', async () => {
  client.setCsrfToken(token);
  let finish;
  let signals = 0;
  client.onAuthenticationFailure(() => { signals += 1; });
  const pending = client.apiRequest('/tickets', {
    fetchImpl: () => new Promise((resolve) => { finish = resolve; }),
  });
  client.setCsrfToken(null);
  client.setCsrfToken('b'.repeat(64));
  finish(json({ detail: 'expired' }, 401));
  await assert.rejects(pending, (error) => error.status === 401);
  assert.equal(signals, 0);
  await client.apiRequest('/agent/ask', { method: 'POST', body: {}, fetchImpl: async (_url, options) => {
    assert.equal(options.headers['X-CSRF-Token'], 'b'.repeat(64));
    return json({ status: 'ok' });
  } });
});

test('login and session-check 401 do not invalidate unrelated authentication state', async () => {
  let signals = 0;
  client.onAuthenticationFailure(() => { signals += 1; });
  for (const [path, method] of [['/auth/me', 'GET'], ['/auth/login', 'POST']]) {
    await assert.rejects(client.apiRequest(path, { method, fetchImpl: async () => json({ detail: 'invalid' }, 401) }),
      (error) => error.status === 401);
  }
  assert.equal(signals, 0);
});

test('a non-JSON 401 still invalidates the active session once', async () => {
  client.setCsrfToken(token);
  let signals = 0;
  client.onAuthenticationFailure(() => { signals += 1; client.setCsrfToken(null); });
  await assert.rejects(client.apiRequest('/tickets', {
    fetchImpl: async () => new Response('not-json', { status: 401 }),
  }), (error) => error.kind === 'response' && error.status === 401);
  assert.equal(signals, 1);
  await assert.rejects(client.apiRequest('/agent/ask', { method: 'POST', body: {},
    fetchImpl: async () => assert.fail('must not send after invalidation') }), (error) => error.status === 401);
});

test('network and 503 failures do not cause an automatic retry or expose internals', async () => {
  let attempts = 0; let signals = 0;
  client.onAuthenticationFailure(() => { signals += 1; });
  await assert.rejects(client.apiRequest('/auth/me', { fetchImpl: async () => { attempts += 1; throw Error('private internals'); } }),
    (error) => error.kind === 'network' && !error.message.includes('private internals'));
  assert.equal(attempts, 1); assert.equal(signals, 0);
});

test('credentials never consult browser storage', async () => {
  let accesses = 0;
  for (const key of ['localStorage', 'sessionStorage']) Object.defineProperty(globalThis, key, {
    configurable: true, get() { accesses += 1; throw Error('storage must not be used'); },
  });
  try {
    client.setCsrfToken(token);
    await client.apiRequest('/auth/logout', { method: 'POST', fetchImpl: async () => json({ status: 'ok' }) });
    client.setCsrfToken(null);
    assert.equal(accesses, 0);
  } finally { delete globalThis.localStorage; delete globalThis.sessionStorage; }
});
