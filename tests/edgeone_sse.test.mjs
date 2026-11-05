/** Controlled HTTP SSE transport; this does not claim deployed EdgeOne/Tunnel validation. */
import test, { after, before } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const env = {
  PROJECT_COPILOT_SITE_ORIGIN: 'https://copilot.example',
  PROJECT_COPILOT_BACKEND_ORIGIN: 'https://controlled-demo.trycloudflare.com',
  PROJECT_COPILOT_PROXY_SECRET: 'synthetic-proxy-secret-'.padEnd(64, 'x'),
};
const job = '00000000-0000-4000-8000-000000000001';
const path = `/api/jobs/${job}/events`;
let server, base, behavior;
const sockets = new Set();
before(async () => {
  server = http.createServer((req, res) => {
    Promise.resolve(behavior(req, res)).catch(() => res.destroy());
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  for (const socket of sockets) socket.destroy();
  await new Promise(resolve => server.close(resolve));
});
function request(suffix = '', extra = {}) {
  return new Request(`https://copilot.example${path}${suffix}`, {
    headers: { 'sec-fetch-site': 'same-origin', accept: 'text/event-stream', ...extra.headers },
    ...Object.fromEntries(Object.entries(extra).filter(([key]) => key !== 'headers')),
  });
}
function loopback(options = {}) {
  return createProxyHandler({ fetchImpl: (url, init) => {
    const target = new URL(url);
    assert.equal(target.origin, env.PROJECT_COPILOT_BACKEND_ORIGIN);
    return fetch(`${base}${target.pathname}${target.search}`, init);
  }, ...options });
}
const decode = value => new TextDecoder().decode(value);

test('SSE forwards first HTTP event before the later event is produced, retaining replay cursor and cookie', async () => {
  let release, received;
  const next = new Promise(resolve => { release = resolve; });
  behavior = async (req, res) => {
    received = { path: req.url, headers: req.headers, method: req.method };
    res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' });
    res.write('id: 5\nevent: progress\ndata: {"stage":"search_knowledge"}\n\n');
    await next;
    res.end('id: 6\nevent: terminal\ndata: {"status":"succeeded"}\n\n');
  };
  const response = await loopback()({ request: request('?after=4', { headers: {
    'last-event-id': '4', cookie: '__Host-pc_session=synthetic',
    'x-project-copilot-proxy-secret': 'spoofed',
  } }), env });
  try {
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('cache-control'), 'no-store, no-transform');
    assert.equal(response.headers.get('x-accel-buffering'), 'no');
    const reader = response.body.getReader();
    const first = decode((await reader.read()).value);
    assert.match(first, /id: 5/);
    assert.doesNotMatch(first, /id: 6/);
    assert.equal(received.path, `/jobs/${job}/events?after=4`);
    assert.equal(received.headers['last-event-id'], '4');
    assert.equal(received.headers.cookie, '__Host-pc_session=synthetic');
    assert.equal(received.headers['x-project-copilot-proxy-secret'], env.PROJECT_COPILOT_PROXY_SECRET);
    assert.equal(received.method, 'GET');
    release();
    assert.match(decode((await reader.read()).value), /id: 6/);
    assert.equal((await reader.read()).done, true);
  } finally { release(); }
});

test('SSE heartbeat is streamed without inventing application progress', async () => {
  behavior = (_req, res) => { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.end(': heartbeat\n\n'); };
  const result = await loopback()({ request: request(), env });
  assert.equal(await result.text(), ': heartbeat\n\n');
});

test('event routes remain GET-only and cursor query is narrowly bounded', async () => {
  let calls = 0;
  const proxy = createProxyHandler({ fetchImpl: () => { calls++; throw Error('not reached'); } });
  for (const suffix of ['?after=-1', '?after=01', '?after=2147483648', '?after=1&after=2', '?q=x', '?after=x', '?%61fter=1']) {
    assert.equal((await proxy({ request: request(suffix), env })).status, 404, suffix);
  }
  assert.equal((await proxy({ request: request('', { method: 'POST', headers: { origin: env.PROJECT_COPILOT_SITE_ORIGIN } }), env })).status, 404);
  for (const id of ['-1', '00', '2147483648', 'arbitrary']) {
    assert.equal((await proxy({ request: request('', { headers: { 'last-event-id': id } }), env })).status, 400);
  }
  assert.equal(calls, 0);
});

test('event connection retains Origin and Fetch Metadata checks before upstream access', async () => {
  let calls = 0;
  const proxy = createProxyHandler({ fetchImpl: () => { calls++; throw Error('not reached'); } });
  for (const headers of [{ origin: 'https://untrusted.example' }, { 'sec-fetch-site': 'cross-site' }]) {
    assert.equal((await proxy({ request: request('', { headers }), env })).status, 403);
  }
  assert.equal(calls, 0);
  assert.equal((await proxy({ request: request(), env: { ...env, PROJECT_COPILOT_BACKEND_ORIGIN: 'http://localhost' } })).status, 503);
  assert.equal(calls, 0);
});

test('non-SSE response on a successful event endpoint fails closed, error JSON remains JSON', async () => {
  behavior = (_req, res) => { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{}'); };
  assert.equal((await loopback()({ request: request(), env })).status, 502);
  behavior = (_req, res) => { res.writeHead(403, { 'Content-Type': 'application/json' }); res.end('{"detail":"denied"}'); };
  const result = await loopback()({ request: request(), env });
  assert.equal(result.status, 403);
  assert.equal(result.headers.get('content-type'), 'application/json');
  assert.deepEqual(await result.json(), { detail: 'denied' });
});

test('stream deadline closes a blocked read without replay or invented terminal success', async () => {
  let calls = 0;
  behavior = (_req, res) => { calls++; res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': connected\n\n'); };
  const result = await loopback({ eventTimeoutMs: 150 })({ request: request(), env });
  const reader = result.body.getReader();
  assert.equal(decode((await reader.read()).value), ': connected\n\n');
  await assert.rejects(reader.read(), /interrupted/);
  assert.equal(calls, 1);
});

test('response cancellation ends only the read-only tail, never retries or creates a job', async () => {
  let calls = 0, closed;
  const ended = new Promise(resolve => { closed = resolve; });
  behavior = (req, res) => {
    calls++; assert.equal(req.method, 'GET');
    req.on('close', closed);
    res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': connected\n\n');
  };
  const result = await loopback()({ request: request(), env });
  const reader = result.body.getReader();
  await reader.read();
  await reader.cancel();
  await ended;
  assert.equal(calls, 1);
});

test('client AbortSignal interrupts SSE while a missing runtime signal remains supported', async () => {
  const controller = new AbortController();
  behavior = (_req, res) => { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': connected\n\n'); };
  const result = await loopback()({ request: request('', { signal: controller.signal }), env });
  const reader = result.body.getReader();
  await reader.read();
  controller.abort();
  await assert.rejects(reader.read(), /interrupted/);
  behavior = (_req, res) => { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.end(': complete\n\n'); };
  const missing = { url: `http://runtime.internal${path}`, method: 'GET', headers: new Headers({ accept: 'text/event-stream' }) };
  assert.equal(await (await loopback()({ request: missing, env })).text(), ': complete\n\n');
});

test('SSE retains response byte limits and does not leak upstream bodies on failure', async () => {
  behavior = (_req, res) => { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.end('x'.repeat(100)); };
  const result = await loopback({ maxResponseBytes: 32 })({ request: request(), env });
  if (result.status === 200) await assert.rejects(result.text(), /interrupted/);
  else assert.equal(result.status, 502);
});

test('preview query credentials are removed without treating them as event authorization', async () => {
  let seen;
  behavior = (req, res) => { seen = req.url; res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.end(': done\n\n'); };
  const result = await loopback()({ request: request('?after=2&eo_token=synthetic&eo_time=3'), env });
  await result.text();
  assert.equal(seen, `/jobs/${job}/events?after=2`);
});
