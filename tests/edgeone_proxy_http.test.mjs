/** Real loopback HTTP tests for the EdgeOne handler. No cloud, model or DB calls.
 * The handler sees a fixed HTTPS origin; an injected fetch maps that origin to
 * an ephemeral localhost server. This tests Node transport, not TLS, Cloudflare
 * or EdgeOne's deployed runtime.
 */
import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

const backendOrigin = 'https://project-copilot-qa.trycloudflare.com';
const testSecret = 'test-only-proxy-secret-'.padEnd(64, 'x');
const env = {
  PROJECT_COPILOT_BACKEND_ORIGIN: backendOrigin,
  PROJECT_COPILOT_PROXY_SECRET: testSecret,
  PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example',
};
let createProxyHandler;
let upstream;
let upstreamBase;
let behavior;
let calls;
const sockets = new Set();

before(async () => {
  ({ createProxyHandler } = await import('../cloud-functions/proxy.mjs'));
  assert.equal(typeof createProxyHandler, 'function');
  upstream = http.createServer((req, res) => {
    calls.push(req.url);
    Promise.resolve(behavior(req, res)).catch(() => { if (!res.headersSent) res.writeHead(500); res.end(); });
  });
  upstream.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => {
    upstream.once('error', reject);
    upstream.listen(0, '127.0.0.1', resolve);
  });
  upstreamBase = `http://127.0.0.1:${upstream.address().port}`;
});

after(async () => {
  for (const socket of sockets) socket.destroy();
  if (upstream?.listening) await new Promise((resolve) => upstream.close(resolve));
});

function localFetch(input, init) {
  const target = new URL(input instanceof Request ? input.url : input);
  assert.equal(target.origin, backendOrigin, 'handler must not target a client-supplied URL');
  const local = new URL(target.pathname + target.search, upstreamBase);
  if (input instanceof Request) return fetch(new Request(local, input), init);
  return fetch(local, init);
}

function invoke(path, init = {}, options = {}, suppliedEnv = env) {
  const headers = new Headers(init.headers);
  if (init.method && init.method !== 'GET' && !headers.has('origin')) headers.set('origin', 'https://copilot.example');
  const request = new Request(`https://copilot.example${path}`, { ...init, headers });
  const handler = createProxyHandler({ fetchImpl: localFetch, timeoutMs: 2000, ...options });
  return handler({ request, env: suppliedEnv });
}

async function bytes(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks);
}

function reset(callback) { calls = []; behavior = callback; }
function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

test('GET preserves path, query, session and CSRF context while dropping spoofed forwarding headers', async () => {
  let seen;
  reset(async (req, res) => {
    seen = { path: req.url, method: req.method, headers: req.headers };
    res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'public, max-age=3600' });
    res.end('{"items":[]}');
  });
  const response = await invoke('/api/tickets?status=open&q=%E8%93%9D%E9%B9%A4', { headers: {
    cookie: 'session=test-session; csrf=test-csrf', origin: 'https://copilot.example',
    'x-csrf-token': 'test-csrf', 'x-forwarded-for': '203.0.113.1',
    'x-project-copilot-proxy-secret': 'attacker-supplied',
  } });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { items: [] });
  assert.equal(seen.method, 'GET');
  assert.equal(seen.path, '/tickets?status=open&q=%E8%93%9D%E9%B9%A4');
  assert.equal(seen.headers.cookie, 'session=test-session; csrf=test-csrf');
  assert.equal(seen.headers.origin, 'https://copilot.example');
  assert.equal(seen.headers['x-csrf-token'], 'test-csrf');
  assert.equal(seen.headers['x-project-copilot-proxy-secret'], testSecret);
  assert.equal(seen.headers['x-forwarded-for'], undefined);
  assert.match(response.headers.get('cache-control') || '', /no-store/i);
});

test('POST JSON and multiple Set-Cookie headers remain distinct', async () => {
  let seen;
  reset(async (req, res) => {
    seen = { method: req.method, body: (await bytes(req)).toString(), headers: req.headers };
    res.writeHead(201, { 'content-type': 'application/json', 'set-cookie': [
      'session=example; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/; Secure; HttpOnly; SameSite=Lax',
      'csrf=example; Path=/; Secure; SameSite=Lax',
    ] });
    res.end('{"status":"ok"}');
  });
  const response = await invoke('/api/auth/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"username":"demo"}' });
  assert.equal(response.status, 201);
  assert.deepEqual(await response.json(), { status: 'ok' });
  assert.equal(seen.method, 'POST');
  assert.equal(seen.body, '{"username":"demo"}');
  assert.equal(seen.headers['content-type'], 'application/json');
  const cookies = response.headers.getSetCookie();
  assert.equal(cookies.length, 2);
  assert.match(cookies[0], /^session=/);
  assert.match(cookies[0], /Expires=Wed, 21 Oct 2037/);
  assert.match(cookies[1], /^csrf=/);
  assert.match(response.headers.get('cache-control') || '', /no-store/i);
});

test('Agent JSON forwards context capability and trace metadata', async () => {
  let seen;
  reset(async (req, res) => {
    seen = { body: (await bytes(req)).toString(), headers: req.headers, path: req.url };
    res.writeHead(200, { 'content-type': 'application/json', 'x-run-id': 'test-run', 'x-trace-id': 'test-trace' });
    res.end('{"status":"answered"}');
  });
  const response = await invoke('/api/jobs', { method: 'POST', headers: {
    'content-type': 'application/json', 'x-copilot-context-id': 'test-id',
    'x-copilot-context-token': 'test-token', 'x-csrf-token': 'test-csrf',
  }, body: '{"question":"hello"}' });
  assert.equal(response.status, 200);
  assert.equal(seen.path, '/jobs');
  assert.equal(seen.body, '{"question":"hello"}');
  assert.equal(seen.headers['x-copilot-context-id'], 'test-id');
  assert.equal(seen.headers['x-copilot-context-token'], 'test-token');
  assert.equal(seen.headers['x-csrf-token'], 'test-csrf');
  assert.equal(response.headers.get('x-run-id'), 'test-run');
  assert.equal(response.headers.get('x-trace-id'), 'test-trace');
});

test('multipart forwards boundary and 4 MiB file bytes without truncation', async () => {
  let seen;
  reset(async (req, res) => {
    seen = { body: await bytes(req), contentType: req.headers['content-type'] };
    res.writeHead(200, { 'content-type': 'application/json' }); res.end('{"status":"indexed"}');
  });
  const form = new FormData();
  form.set('file', new File([Buffer.alloc(4 * 1024 * 1024, 0x61)], 'test.txt', { type: 'text/plain' }));
  const response = await invoke('/api/jobs/upload', { method: 'POST', body: form });
  assert.equal(response.status, 200);
  assert.match(seen.contentType, /^multipart\/form-data; boundary=/i);
  assert.ok(seen.body.length > 4 * 1024 * 1024 && seen.body.length < 6 * 1024 * 1024);
  assert.notEqual(seen.body.indexOf(Buffer.alloc(1024, 0x61)), -1);
  assert.equal(seen.body.filter((byte) => byte === 0x61).length >= 4 * 1024 * 1024, true);
});

test('an oversized body without Content-Length is rejected before contacting upstream', async () => {
  reset((_req, res) => res.end('unexpected'));
  const body = new ReadableStream({ start(controller) {
    controller.enqueue(new Uint8Array(700)); controller.enqueue(new Uint8Array(700)); controller.close();
  } });
  const response = await invoke('/api/jobs', { method: 'POST', body, duplex: 'half', headers: { 'content-type': 'application/json' } }, { maxRequestBytes: 1024 });
  assert.equal(response.status, 413);
  assert.deepEqual(calls, []);
});

test('query parameters cannot choose target and non-API routes are rejected locally', async () => {
  reset((_req, res) => res.end('unexpected'));
  for (const path of ['/api/docs', '/api/health%2F..%2Fdocs', '/api/proxy?url=https://evil.example']) {
    const response = await invoke(path);
    assert.ok([400, 403, 404, 405].includes(response.status), `${path}: ${response.status}`);
  }
  assert.deepEqual(calls, []);
});

test('upstream HTTP status survives and dynamic responses are not cacheable', async () => {
  reset((_req, res) => { res.writeHead(409, { 'content-type': 'application/json', 'cache-control': 'public, max-age=60' }); res.end('{"detail":"conflict"}'); });
  const response = await invoke('/api/actions/22222222-2222-4222-8222-222222222222/confirm', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{"payload_fingerprint":"test"}',
  });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).detail, 'conflict');
  assert.match(response.headers.get('cache-control') || '', /no-store/i);
});

test('upstream redirect is rejected and never followed', async () => {
  reset((_req, res) => { res.writeHead(302, { location: `${upstreamBase}/secret-capture` }); res.end(); });
  const response = await invoke('/api/health');
  assert.equal(response.status, 502);
  assert.equal(calls.length, 1);
  assert.equal(response.headers.get('location'), null);
});

test('declared oversized response fails closed before sending a successful response', async () => {
  reset((_req, res) => { res.writeHead(200, { 'content-length': '100', 'content-type': 'application/json' }); res.end('x'.repeat(100)); });
  const response = await invoke('/api/health', {}, { maxResponseBytes: 32 });
  assert.equal(response.status, 502);
  assert.doesNotMatch(await response.text(), /x{20}/);
});

test('stream response forwards its first bytes before upstream completes', async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  reset(async (_req, res) => {
    res.writeHead(200, { 'content-type': 'application/octet-stream' });
    res.write('first-part');
    await gate;
    res.end('second-part');
  });
  try {
    const response = await invoke('/api/health');
    assert.equal(response.status, 200);
    const reader = response.body.getReader();
    const first = await Promise.race([reader.read(), delay(800).then(() => { throw new Error('response was buffered until completion'); })]);
    assert.equal(new TextDecoder().decode(first.value), 'first-part');
    release();
    const remainder = [];
    for (;;) { const next = await reader.read(); if (next.done) break; remainder.push(next.value); }
    assert.equal(Buffer.concat(remainder.map((chunk) => Buffer.from(chunk))).toString(), 'second-part');
  } finally { release(); }
});

test('client abort closes the real mock upstream connection', async () => {
  let seen;
  const requestSeen = new Promise((resolve) => { seen = resolve; });
  let close;
  const closed = new Promise((resolve) => { close = resolve; });
  reset((_req, res) => { res.on('close', close); seen(); });
  const controller = new AbortController();
  const pending = invoke('/api/health', { signal: controller.signal });
  await requestSeen;
  controller.abort();
  await Promise.race([closed, delay(1500).then(() => { throw new Error('upstream connection remained open after abort'); })]);
  await pending.catch(() => {});
});

test('cancelling a response reader closes the real mock upstream connection', async () => {
  let close;
  const closed = new Promise((resolve) => { close = resolve; });
  reset((_req, res) => {
    res.on('close', close);
    res.writeHead(200, { 'content-type': 'application/octet-stream' });
    res.write('first-part');
  });
  const response = await invoke('/api/health');
  const reader = response.body.getReader();
  assert.equal(new TextDecoder().decode((await reader.read()).value), 'first-part');
  await reader.cancel();
  await Promise.race([closed, delay(1500).then(() => { throw new Error('upstream connection remained open after response cancellation'); })]);
});

test('upstream timeout produces a sanitized gateway error without automatic retry', async () => {
  reset((_req, _res) => {});
  const response = await invoke('/api/health', {}, { timeoutMs: 80 });
  assert.ok([502, 504].includes(response.status));
  const body = await response.text();
  assert.doesNotMatch(body, /trycloudflare|test-only-proxy-secret|127\.0\.0\.1/i);
  assert.equal(calls.length, 1);
  assert.match(response.headers.get('cache-control') || '', /no-store/i);
});

test('missing or malformed backend configuration never reaches upstream', async () => {
  reset((_req, res) => res.end('unexpected'));
  for (const suppliedEnv of [ {}, { ...env, PROJECT_COPILOT_PROXY_SECRET: '' }, { ...env, PROJECT_COPILOT_BACKEND_ORIGIN: 'http://example.com' } ]) {
    const response = await invoke('/api/health', {}, {}, suppliedEnv);
    assert.ok(response.status >= 500);
    assert.doesNotMatch(await response.text(), /test-only-proxy-secret|trycloudflare/i);
  }
  assert.deepEqual(calls, []);
});
