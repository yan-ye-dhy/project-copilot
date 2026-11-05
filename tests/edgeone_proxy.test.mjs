import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';
import onRequest from '../cloud-functions/api/[[default]].js';

const ORIGIN = 'https://quiet-bamboo-garden.trycloudflare.com';
const SECRET = 'a'.repeat(43);
const ENV = { PROJECT_COPILOT_BACKEND_ORIGIN: ORIGIN, PROJECT_COPILOT_PROXY_SECRET: SECRET, PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example' };
const ID = '11111111-2222-4333-8444-555555555555';
const request = (path, init = {}) => {
  const headers = new Headers(init.headers);
  if (init.method && init.method !== 'GET') {
    if (!headers.has('origin')) headers.set('origin', 'https://copilot.example');
    if (init.body && !headers.has('content-type')) headers.set('content-type',
      path === '/api/jobs/upload' ? 'multipart/form-data; boundary=test-boundary' : 'application/json');
  }
  return new Request(`https://copilot.example${path}`, { ...init, headers });
};
const context = (path, init = {}, env = ENV) => ({ request: request(path, init), env });
const json = (data = { ok: true }, init = {}) => Response.json(data, init);
const makeHandler = (fetchImpl, limits = {}) => createProxyHandler({ fetchImpl, ...limits });
const neverFetch = () => { throw new Error('Unexpected network call'); };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test('platform configuration describes CSP, frame policy and no-store without changing cookies', async () => {
  const config = JSON.parse(await readFile(new URL('../edgeone.json', import.meta.url), 'utf8'));
  assert.equal(config.cloudFunctions.nodejs.maxDuration, 120);
  const rule = (path) => Object.fromEntries(config.headers.find((item) => item.source === path).headers.map((item) => [item.key.toLowerCase(), item.value]));
  const global = rule('/*');
  assert.equal(global['content-security-policy'], "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  assert.equal(global['x-content-type-options'], 'nosniff');
  assert.equal(global['referrer-policy'], 'no-referrer');
  assert.equal(global['x-frame-options'], 'DENY');
  assert.equal(rule('/')['cache-control'], 'no-store');
  assert.equal(rule('/api/*')['cache-control'], 'no-store');
  for (const item of config.headers) assert(!item.headers.some(({ key }) => key.toLowerCase() === 'set-cookie'));
});

test('production entry fails closed without server configuration', async () => {
  const result = await onRequest(context('/api/health', {}, {}));
  assert.equal(result.status, 503);
  assert.equal(result.headers.get('cache-control'), 'no-store');
});

test('configuration rejects missing, weak or malformed secrets and origins', async () => {
  const handler = makeHandler(neverFetch);
  const invalid = [
    {}, { ...ENV, PROJECT_COPILOT_PROXY_SECRET: '' },
    { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: undefined },
    { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example/' },
    { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: 'http://copilot.example' },
    { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: 'https://user:pass@copilot.example' },
    { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example:8443' },
    { ...ENV, PROJECT_COPILOT_PROXY_SECRET: 'short' },
    { ...ENV, PROJECT_COPILOT_PROXY_SECRET: 'a'.repeat(32) + '\n' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'http://quiet.trycloudflare.com' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://localhost' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://127.0.0.1' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://169.254.169.254' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://quiet.trycloudflare.com.evil.example' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://nested.quiet.trycloudflare.com' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://user:pass@quiet.trycloudflare.com' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://quiet.trycloudflare.com:8443' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://quiet.trycloudflare.com/path' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://quiet.trycloudflare.com?x=1' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://quiet.trycloudflare.com#x' },
    { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: ' https://quiet.trycloudflare.com' },
  ];
  for (const env of invalid) assert.equal((await handler(context('/api/health', {}, env))).status, 503);
});

test('GET targets only configured tunnel; query is preserved verbatim', async () => {
  let call;
  const handler = makeHandler(async (url, init) => { call = { url, init }; return json(); });
  const response = await handler(context('/api/tickets?q=%E8%93%9D%E9%B9%A4&limit=5&target=https%3A%2F%2Fevil.example'));
  assert.equal(response.status, 200);
  assert.equal(call.url, `${ORIGIN}/tickets?q=%E8%93%9D%E9%B9%A4&limit=5&target=https%3A%2F%2Fevil.example`);
  assert.equal(call.init.redirect, 'manual');
  assert.deepEqual(await response.json(), { ok: true });
});

test('only explicit routes, UUID paths and HTTP methods are forwarded', async () => {
  const calls = [];
  const handler = makeHandler(async (url, init) => { calls.push([new URL(url).pathname, init.method]); return json(); });
  for (const [method, path] of [
    ['GET', '/api/health'], ['GET', '/api/ui/config'], ['GET', '/api/auth/me'],
    ['POST', '/api/auth/register'], ['POST', '/api/auth/login'], ['POST', '/api/auth/logout'],
    ['GET', '/api/tickets'], ['GET', `/api/tickets/${ID}`],
    ['POST', '/api/agent/contexts'],
    ['GET', '/api/documents'], ['DELETE', `/api/documents/${ID}`],
    ['POST', '/api/actions/preview'], ['GET', `/api/actions/${ID}`],
    ['POST', `/api/actions/${ID}/confirm`], ['POST', `/api/actions/${ID}/reject`],
    ['POST', '/api/jobs'], ['POST', '/api/jobs/upload'], ['GET', `/api/jobs/${ID}`],
  ]) {
    const response = await handler(context(path, { method }));
    assert.equal(response.status, 200, `${method} ${path}`);
    await response.body.cancel();
  }
  const before = calls.length;
  for (const [method, path] of [
    ['POST', '/api/tickets'], ['GET', '/api/docs'], ['GET', '/api/openapi.json'],
    ['GET', '/api/metrics'], ['GET', '/api/collections'], ['GET', '/api/phoenix'],
    ['GET', '/api/agent/ask'], ['POST', '/api/health'], ['PUT', '/api/tickets'],
    ['GET', '/api/tickets/not-a-uuid'], ['GET', '/api/tickets/../docs'],
    ['GET', '/api/actions/%2Fetc'], ['GET', '/api//tickets'], ['GET', '/api/auth/ME'],
  ]) assert.equal((await handler(context(path, { method }))).status, 404, `${method} ${path}`);
  assert.equal(calls.length, before);
});

test('JSON POST passes payload and only allowed headers; internal secret is injected', async () => {
  let call;
  const handler = makeHandler(async (url, init) => { call = { url, init }; return json({ status: 'answered' }, { status: 201 }); });
  const value = JSON.stringify({ question: '你好' });
  const response = await handler(context('/api/jobs', {
    method: 'POST', body: value,
    headers: { 'content-type': 'application/json', 'cookie': 'session=value', 'origin': 'https://copilot.example',
      'x-csrf-token': 'csrf', 'x-copilot-context-id': ID, 'x-copilot-context-token': 'capability',
      'x-project-copilot-proxy-secret': 'forged', 'x-forwarded-for': '127.0.0.1',
      'forwarded': 'for=127.0.0.1', 'x-real-ip': '127.0.0.1', 'authorization': 'Bearer bad',
      'connection': 'x-copilot-context-token, X-Throwaway', 'x-throwaway': 'bad' },
  }));
  assert.equal(response.status, 201);
  assert.equal(new TextDecoder().decode(call.init.body), value);
  assert.equal(call.init.headers.get('content-type'), 'application/json');
  assert.equal(call.init.headers.get('cookie'), 'session=value');
  assert.equal(call.init.headers.get('origin'), 'https://copilot.example');
  assert.equal(call.init.headers.get('x-csrf-token'), 'csrf');
  assert.equal(call.init.headers.get('x-copilot-context-id'), ID);
  assert.equal(call.init.headers.get('x-copilot-context-token'), null);
  assert.equal(call.init.headers.get('x-project-copilot-proxy-secret'), SECRET);
  for (const key of ['authorization', 'forwarded', 'x-forwarded-for', 'x-real-ip', 'x-throwaway', 'connection']) {
    assert.equal(call.init.headers.get(key), null, key);
  }
  assert.equal(call.init.headers.get('accept-encoding'), 'identity');
  assert.equal(call.init.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { status: 'answered' });
});

test('context capability and idempotency header survive when not hop-by-hop', async () => {
  let headers;
  const handler = makeHandler(async (_url, init) => { headers = init.headers; return json(); });
  const response = await handler(context(`/api/actions/${ID}/confirm`, { method: 'POST', body: '{}', headers: {
    'content-type': 'application/json', 'x-copilot-context-id': ID,
    'x-copilot-context-token': 'capability', 'idempotency-key': 'request-1',
  } }));
  assert.equal(headers.get('x-copilot-context-token'), 'capability');
  assert.equal(headers.get('idempotency-key'), 'request-1');
  await response.body.cancel();
});

test('multiple Set-Cookie values with Expires survive separately and remain host-only', async () => {
  const h = new Headers({ 'content-type': 'application/json', 'x-run-id': 'run-1', 'x-trace-id': 'trace-1',
    'x-trace-exported': 'true', 'location': 'https://private.example/secret', 'server': 'private',
    'connection': 'x-hidden', 'x-hidden': 'secret' });
  h.append('set-cookie', 'session=abc; Expires=Wed, 01 Nov 2028 00:00:00 GMT; HttpOnly; Secure; SameSite=Lax; Path=/');
  h.append('set-cookie', 'csrf=def; Secure; SameSite=Lax; Path=/');
  const handler = makeHandler(async () => new Response('{"ok":true}', { status: 200, headers: h }));
  const response = await handler(context('/api/auth/me', { headers: { cookie: 'session=abc' } }));
  assert.deepEqual(response.headers.getSetCookie(), h.getSetCookie());
  assert.equal(response.headers.get('x-run-id'), 'run-1');
  assert.equal(response.headers.get('x-trace-id'), 'trace-1');
  assert.equal(response.headers.get('x-trace-exported'), 'true');
  assert.equal(response.headers.get('location'), null);
  assert.equal(response.headers.get('server'), null);
  assert.equal(response.headers.get('x-hidden'), null);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.deepEqual(await response.json(), { ok: true });
});

test('cookies carrying a Domain attribute fail closed', async () => {
  const handler = makeHandler(async () => new Response('', { headers: { 'set-cookie': 'sid=x; Domain=.trycloudflare.com; Secure' } }));
  const response = await handler(context('/api/auth/login', { method: 'POST' }));
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('set-cookie'), null);
});

test('an upstream runtime without multi-cookie access cannot silently merge Set-Cookie', async () => {
  const headers = new Headers({ 'set-cookie': 'a=1' });
  Object.defineProperty(headers, 'getSetCookie', { value: undefined });
  const handler = makeHandler(async () => ({ status: 200, headers, body: null }));
  assert.equal((await handler(context('/api/auth/me'))).status, 502);
});

test('ordinary non-2xx statuses are preserved without caching', async () => {
  for (const status of [400, 401, 403, 404, 409, 413, 422, 429]) {
    const handler = makeHandler(async () => json({ detail: 'check request' }, { status }));
    const response = await handler(context('/api/auth/me'));
    assert.equal(response.status, status);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal((await response.json()).detail, 'check request');
  }
});

test('upstream 5xx preserves status and trace id while sanitizing error body', async () => {
  const handler = makeHandler(async () => new Response(`credential ${SECRET}`, { status: 503, headers: { 'x-run-id': 'run-2' } }));
  const response = await handler(context('/api/jobs', { method: 'POST', body: '{}' }));
  assert.equal(response.status, 503); assert.equal(response.headers.get('x-run-id'), 'run-2');
  assert(!(await response.text()).includes(SECRET));
});

test('redirect is rejected and Location is not exposed or followed', async () => {
  let calls = 0;
  const handler = makeHandler(async (_url, init) => {
    calls += 1; assert.equal(init.redirect, 'manual');
    return new Response(null, { status: 302, headers: { location: 'https://attacker.invalid/' } });
  });
  const response = await handler(context('/api/health'));
  assert.equal(response.status, 502); assert.equal(response.headers.get('location'), null); assert.equal(calls, 1);
});

test('oversized Content-Length is rejected before making any upstream request', async () => {
  const handler = makeHandler(neverFetch, { maxRequestBytes: 8 });
  const response = await handler(context('/api/jobs/upload', { method: 'POST', body: '123456789', headers: { 'content-length': '9' } }));
  assert.equal(response.status, 413);
});

test('invalid Content-Length is rejected before forwarding', async () => {
  const handler = makeHandler(neverFetch, { maxRequestBytes: 8 });
  for (const size of ['-1', '1, 1', 'abc', '9007199254740992']) {
    const response = await handler(context('/api/jobs', { method: 'POST', body: 'x', headers: { 'content-length': size } }));
    assert.equal(response.status, 400);
  }
});

test('multipart binary body is forwarded intact under limit without losing its boundary', async () => {
  let call;
  const handler = makeHandler(async (_url, init) => { call = init; return json(); });
  const bytes = new Uint8Array([0, 1, 2, 3, 13, 10, 255]);
  const response = await handler(context('/api/jobs/upload', { method: 'POST', body: bytes, headers: {
    'content-type': 'multipart/form-data; boundary=example-boundary',
  } }));
  assert.deepEqual(call.body, bytes);
  assert.equal(call.headers.get('content-type'), 'multipart/form-data; boundary=example-boundary');
  await response.body.cancel();
});

test('a chunked request without Content-Length is limited by actual bytes', async () => {
  let cancelled = false;
  const handler = makeHandler(neverFetch, { maxRequestBytes: 8 });
  const body = new ReadableStream({
    start(c) { c.enqueue(new Uint8Array(5)); c.enqueue(new Uint8Array(4)); },
    cancel() { cancelled = true; },
  });
  const response = await handler(context('/api/jobs/upload', { method: 'POST', body, duplex: 'half' }));
  assert.equal(response.status, 413); assert.equal(cancelled, true);
});

test('a declared short Content-Length cannot bypass actual body limit', async () => {
  const handler = makeHandler(neverFetch, { maxRequestBytes: 8 });
  const body = new ReadableStream({ start(c) { c.enqueue(new Uint8Array(9)); c.close(); } });
  const response = await handler(context('/api/jobs', { method: 'POST', body, duplex: 'half', headers: { 'content-length': '1' } }));
  assert.equal(response.status, 413);
});

test('upstream response over declared size is refused', async () => {
  const handler = makeHandler(async () => new Response('large', { headers: { 'content-length': '9' } }), { maxResponseBytes: 8 });
  assert.equal((await handler(context('/api/health'))).status, 502);
});

test('streamed response forwards chunks without buffering and stops when actual limit is crossed', async () => {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; c.enqueue(new Uint8Array([1, 2, 3])); } });
  const handler = makeHandler(async () => new Response(body, { headers: { 'content-type': 'application/octet-stream' } }), { maxResponseBytes: 5 });
  const response = await handler(context('/api/health'));
  assert.equal(response.status, 200);
  const reader = response.body.getReader();
  assert.deepEqual((await reader.read()).value, new Uint8Array([1, 2, 3]));
  controller.enqueue(new Uint8Array([4, 5, 6]));
  await assert.rejects(reader.read(), /Upstream response interrupted/);
});

test('unexpected gzip response is rejected without forwarding stale encoding or length', async () => {
  const handler = makeHandler(async () => new Response('decoded', { headers: { 'content-encoding': 'gzip', 'content-length': '21' } }));
  const response = await handler(context('/api/health'));
  assert.equal(response.status, 502); assert.equal(response.headers.get('content-encoding'), null);
  assert.equal(response.headers.get('content-length'), null);
});

test('204 response has no body and retains status', async () => {
  const handler = makeHandler(async () => new Response(null, { status: 204 }));
  const response = await handler(context('/api/auth/logout', { method: 'POST' }));
  assert.equal(response.status, 204); assert.equal(response.body, null);
});

test('network failure is sanitized and is never automatically retried', async () => {
  let calls = 0;
  const handler = makeHandler(async () => { calls += 1; throw new Error(`private ${ORIGIN} ${SECRET}`); });
  const response = await handler(context('/api/jobs', { method: 'POST', body: '{}' }));
  const text = await response.text();
  assert.equal(response.status, 502); assert.equal(calls, 1);
  assert(!text.includes(ORIGIN)); assert(!text.includes(SECRET));
});

test('timeout aborts the upstream exactly once and returns a safe 504', async () => {
  let signal; let calls = 0;
  const handler = makeHandler(async (_url, init) => { calls += 1; signal = init.signal; await sleep(50); return json(); }, { timeoutMs: 10 });
  const response = await handler(context('/api/jobs', { method: 'POST', body: '{}' }));
  assert.equal(response.status, 504); assert.equal(calls, 1); assert.equal(signal.aborted, true);
  assert.match((await response.json()).detail, /查询操作状态/);
});

test('timeout remains active during response body streaming', async () => {
  let aborted;
  const handler = makeHandler(async (_url, init) => {
    aborted = init.signal;
    return new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array([1])); } }));
  }, { timeoutMs: 20 });
  const response = await handler(context('/api/health'));
  const reader = response.body.getReader();
  assert.deepEqual((await reader.read()).value, new Uint8Array([1]));
  const waiting = assert.rejects(reader.read(), /Upstream response interrupted/);
  await sleep(40);
  await waiting;
  assert.equal(aborted.aborted, true);
});

test('client abort cancels an in-flight upstream request', async () => {
  const client = new AbortController(); let signal;
  const handler = makeHandler(async (_url, init) => { signal = init.signal; await sleep(50); return json(); });
  const pending = handler(context('/api/jobs', { method: 'POST', body: '{}', signal: client.signal }));
  setTimeout(() => client.abort(), 5);
  const response = await pending;
  assert.equal(response.status, 499); assert.equal(signal.aborted, true);
});

test('downstream response cancellation aborts upstream stream', async () => {
  let upstreamSignal, cancelled = false;
  const handler = makeHandler(async (_url, init) => {
    upstreamSignal = init.signal;
    return new Response(new ReadableStream({ start(c) { c.enqueue(new Uint8Array([1])); }, cancel() { cancelled = true; } }));
  });
  const response = await handler(context('/api/health'));
  const reader = response.body.getReader();
  assert.equal((await reader.read()).done, false);
  await reader.cancel();
  assert.equal(upstreamSignal.aborted, true); assert.equal(cancelled, true);
});

test('injected factory rejects invalid limits', () => {
  for (const options of [{ timeoutMs: 0 }, { maxRequestBytes: -1 }, { maxResponseBytes: Infinity }, { fetchImpl: null }]) {
    assert.throws(() => createProxyHandler(options), TypeError);
  }
});

test('entry header and origin rejection happens before a body reader or upstream call', async () => {
  const handler = makeHandler(neverFetch);
  for (const [headers, status] of [
    [{ origin: 'https://evil.example', 'content-type': 'application/json' }, 403],
    [{ 'content-type': 'application/json' }, 403],
    [{ origin: 'https://copilot.example', 'sec-fetch-site': 'cross-site', 'content-type': 'application/json' }, 403],
    [{ origin: 'https://copilot.example', 'content-encoding': 'gzip', 'content-type': 'application/json' }, 415],
    [{ origin: 'https://copilot.example', 'transfer-encoding': 'chunked', 'content-type': 'application/json' }, 400],
    [{ origin: 'https://copilot.example', upgrade: 'websocket', 'content-type': 'application/json' }, 400],
    [{ origin: 'https://copilot.example', expect: '100-continue', 'content-type': 'application/json' }, 400],
    [{ origin: 'https://copilot.example', 'x-http-method-override': 'DELETE', 'content-type': 'application/json' }, 400],
    [{ origin: 'https://copilot.example', 'content-length': '262145', 'content-type': 'application/json' }, 413],
    [{ origin: 'https://copilot.example', 'content-type': 'text/plain' }, 415],
    [{ origin: 'https://copilot.example', cookie: 'a'.repeat(8193), 'content-type': 'application/json' }, 431],
    [{ origin: 'https://copilot.example', 'x-noise': 'x'.repeat(16384), 'content-type': 'application/json' }, 431],
  ]) {
    let readers = 0;
    const response = await handler({ env: ENV, request: {
      url: 'https://copilot.example/api/jobs', method: 'POST', headers: new Headers(headers),
      signal: new AbortController().signal,
      body: { getReader() { readers += 1; throw new Error('body reader must not be opened'); } },
    } });
    assert.equal(response.status, status, JSON.stringify(Object.keys(headers)));
    assert.equal(readers, 0);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.doesNotMatch(await response.text(), /trycloudflare|aaaaaaaaaaaaaaaa|evil\.example/);
  }
});

test('request URL origin must match the configured site; a preview host is not staging authentication', async () => {
  const handler = makeHandler(neverFetch);
  const response = await handler({ env: ENV, request: new Request('https://unreviewed.edgeone.cool/api/health?eo_token=not-authentication') });
  assert.equal(response.status, 403);
});

test('non-upload JSON is bounded at 256 KiB even when the platform permits larger bodies', async () => {
  const handler = makeHandler(neverFetch);
  const response = await handler(context('/api/jobs', { method: 'POST', body: 'x'.repeat(262145) }));
  assert.equal(response.status, 413);
});

test('upload requires a bounded multipart boundary and does not fall back to Edge Functions', async () => {
  const handler = makeHandler(neverFetch);
  for (const type of ['application/pdf', 'application/json', 'multipart/form-data', `multipart/form-data; boundary=${'x'.repeat(71)}`]) {
    const response = await handler(context('/api/jobs/upload', { method: 'POST', body: 'x', headers: { 'content-type': type } }));
    assert.equal(response.status, 415);
  }
});

test('two independent handlers do not pretend to implement a global rate limiter or burden GET polling', async () => {
  const first = makeHandler(async () => json());
  const second = makeHandler(async () => json());
  const responses = await Promise.all(Array.from({ length: 40 }, (_, index) =>
    (index % 2 ? first : second)(context(`/api/jobs/${ID}`))));
  for (const response of responses) {
    assert.equal(response.status, 200);
    await response.body.cancel();
  }
});


test('full legal Agent history fits even when every Unicode code point is JSON escaped', async () => {
  let call;
  const handler = makeHandler(async (_url, init) => { call = init; return json(); });
  const payload = { kind: 'agent', payload: {
    question: '😀'.repeat(2000),
    history: [
      { role: 'user', content: '😀'.repeat(4000) },
      { role: 'assistant', content: '😀'.repeat(4000) },
      { role: 'user', content: '😀'.repeat(2000) },
    ],
  } };
  const escaped = JSON.stringify(payload).replace(/[\u0080-\uffff]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
  assert(escaped.length > 128 * 1024 && escaped.length < 256 * 1024);
  const response = await handler(context('/api/jobs', { method: 'POST', body: escaped }));
  assert.equal(response.status, 200);
  assert.deepEqual(JSON.parse(new TextDecoder().decode(call.body)), payload);
  await response.body.cancel();
});

test('CLOSED bootstrap with no valid Tunnel destination still rejects invalid requests locally', async () => {
  const handler = makeHandler(neverFetch);
  for (const destination of [undefined, '', 'http://bad.trycloudflare.com', 'https://other.example', 'https://bad.trycloudflare.com/path']) {
    const env = { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: destination };
    let readers = 0;
    const invoke = (url, method, headers, hasBody = false) => handler({ env, request: {
      url: `https://copilot.example${url}`, method, headers: new Headers(headers),
      signal: new AbortController().signal,
      body: hasBody ? { getReader() { readers += 1; throw new Error('must remain unread'); } } : null,
    } });
    assert.equal((await invoke('/api/docs', 'GET', {})).status, 404);
    assert.equal((await invoke('/api/jobs', 'POST', { origin: 'https://evil.example', 'content-type': 'application/json' }, true)).status, 403);
    assert.equal((await invoke('/api/jobs', 'POST', { origin: ENV.PROJECT_COPILOT_SITE_ORIGIN, 'content-type': 'application/json', 'content-length': '262145' }, true)).status, 413);
    for (const response of [
      await invoke('/api/health', 'GET', {}),
      await invoke('/api/jobs', 'POST', { origin: ENV.PROJECT_COPILOT_SITE_ORIGIN, 'content-type': 'application/json' }, true),
    ]) {
      assert.equal(response.status, 503);
      assert.doesNotMatch(await response.text(), /trycloudflare|aaaaaaaaaaaa|other\.example/);
    }
    assert.equal(readers, 0);
  }
});

test('missing site or secret cannot be used as bootstrap request-policy proof', async () => {
  const handler = makeHandler(neverFetch);
  for (const env of [{ PROJECT_COPILOT_SITE_ORIGIN: ENV.PROJECT_COPILOT_SITE_ORIGIN }, { PROJECT_COPILOT_PROXY_SECRET: SECRET }]) {
    assert.equal((await handler(context('/api/docs', {}, env))).status, 503);
  }
});
