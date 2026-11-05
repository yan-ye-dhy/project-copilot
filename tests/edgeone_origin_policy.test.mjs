/** Runtime URLs are routing input, never browser identity or upstream targets.
 * All data is synthetic. HTTP uses loopback; authentication replies are explicit
 * controlled upstream contracts, not a real PostgreSQL/session integration.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import http from 'node:http';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const SITE = 'https://copilot-synthetic.example';
const SECRET = 'synthetic-origin-policy-secret-123456789';
const DESTINATION = 'https://reviewed-synthetic.trycloudflare.com';
const ENV = { PROJECT_COPILOT_SITE_ORIGIN: SITE, PROJECT_COPILOT_PROXY_SECRET: SECRET };
const ID = '11111111-2222-4333-8444-555555555555';
const runtime = () => `${Math.random() < .5 ? 'http' : 'https'}://node-${randomUUID()}.runtime.invalid:${10000 + Math.floor(Math.random() * 40000)}`;

function closedProbe() {
  let fetches = 0, readers = 0;
  const handler = createProxyHandler({ fetchImpl() {
    fetches += 1;
    assert.fail('no configured backend must mean no network request');
  } });
  function invoke(path = '/api/health', { base = runtime(), url, method = 'GET', headers = {}, body = false, env = ENV } = {}) {
    return handler({ env, request: {
      url: url ?? `${base}${path}`, method, headers: new Headers(headers), signal: new AbortController().signal,
      body: body ? { getReader() { readers += 1; assert.fail('bootstrap must not read a body'); } } : null,
    } });
  }
  return { handler, invoke, idle() { assert.equal(fetches, 0); assert.equal(readers, 0); } };
}

test('random runtime HTTP/HTTPS hosts, deployment names and ports cannot decide browser Origin', async () => {
  const probe = closedProbe();
  for (let i = 0; i < 20; i += 1) {
    for (const path of ['/api/health', '/api/auth/me', `/api/jobs/${ID}`]) {
      const response = await probe.invoke(`${path}?q=iteration-${i}`, { headers: { 'sec-fetch-site': 'same-origin' } });
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { detail: '服务暂时不可用。' });
    }
  }
  for (const base of [SITE, 'http://localhost:3000', 'https://deployment-synthetic.edgeone.cool', 'https://[::1]:8443']) {
    assert.equal((await probe.invoke('/api/health', { base })).status, 503);
  }
  probe.idle();
});

test('GET may omit Origin but every explicit Origin must match the fixed reviewed HTTPS site', async () => {
  const probe = closedProbe();
  for (const path of ['/api/health', '/api/auth/me', '/api/tickets', `/api/jobs/${ID}`]) {
    assert.equal((await probe.invoke(path)).status, 503);
    assert.equal((await probe.invoke(path, { headers: { origin: SITE } })).status, 503);
    for (const origin of ['', 'null', 'http://copilot-synthetic.example', 'https://evil.example', `${SITE}/`]) {
      assert.equal((await probe.invoke(path, { headers: { origin } })).status, 403, origin);
    }
  }
  probe.idle();
});

test('POST and DELETE always require the fixed browser Origin, even for clients without Fetch Metadata', async () => {
  const probe = closedProbe();
  for (const [method, path] of [['POST', '/api/jobs'], ['POST', '/api/auth/login'], ['DELETE', `/api/documents/${ID}`]]) {
    for (const origin of [undefined, '', 'null', 'https://evil.example', runtime()]) {
      const headers = { 'content-type': 'application/json', host: new URL(SITE).host, 'x-forwarded-host': new URL(SITE).host };
      if (origin !== undefined) headers.origin = origin;
      assert.equal((await probe.invoke(path, { method, headers, body: true })).status, 403);
    }
    assert.equal((await probe.invoke(path, { method, body: true, headers: { origin: SITE, 'content-type': 'application/json' } })).status, 503);
  }
  probe.idle();
});

test('Fetch Metadata absence supports curl/older clients but present invalid or cross-site values reject', async () => {
  const probe = closedProbe();
  for (const [method, path] of [['GET', '/api/auth/me'], ['POST', '/api/jobs'], ['DELETE', `/api/documents/${ID}`]]) {
    for (const value of [undefined, 'same-origin', 'none', 'cross-site', 'same-site', '', 'unexpected']) {
      const headers = { origin: SITE, 'content-type': 'application/json' };
      if (value !== undefined) headers['sec-fetch-site'] = value;
      const expected = value === undefined || value === 'same-origin' || value === 'none' ? 503 : 403;
      assert.equal((await probe.invoke(path, { method, body: method !== 'GET', headers })).status, expected);
    }
  }
  probe.idle();
});

test('malformed URL syntax cannot become an allowed normalized route', async () => {
  const probe = closedProbe();
  for (const url of [
    'not-a-url', '/api/health', '//runtime.invalid/api/health', 'file:///api/health',
    'ftp://runtime.invalid/api/health', 'javascript:/api/health', 'https:/runtime.invalid/api/health',
    'https://user:password@runtime.invalid/api/health', 'https://@runtime.invalid/api/health',
    'https://runtime.invalid:65536/api/health', 'https://runtime.invalid/api/health#fragment',
    'https://runtime.invalid/api/health#', ' https://runtime.invalid/api/health',
    'https://runtime.invalid/api/he\nalth', 'https://runtime.invalid\\api\\health',
    'https://runtime.invalid/api/./health', 'https://runtime.invalid/api/../api/health',
    'https://runtime.invalid/api/%2e/health', 'https://runtime.invalid/api/%68ealth',
    'https://runtime.invalid/api//health', 'https://runtime.invalid//api/health',
  ]) assert.equal((await probe.invoke(undefined, { url })).status, 404, url);
  probe.idle();
});

test('query validation rejects malformed percent/UTF8/control input before preview-token removal', async () => {
  const probe = closedProbe();
  for (const search of [
    '?q=%', '?q=%0', '?q=%GG', '?q=%C3%28', '?q=%FF', '?q=%ED%A0%80',
    '?q=%00', '?q=%0d%0aInjected', '?q=%7f', '?%00key=value', '?=value',
    '?q=ok&&status=open', '?q=ok&', '?eo_token=%GG', '?eo_time=%00',
    `?q=${'中'.repeat(500)}`, '?q=\ud800', '?q=\u0081',
    `?q=${'a'.repeat(4096)}`, `?${Array.from({ length: 65 }, (_, i) => `p${i}=x`).join('&')}`,
  ]) assert.equal((await probe.invoke(`/api/health${search}`)).status, 404, search.slice(0, 50));
  probe.idle();
});

test('normal business query bytes/order, empty values, duplicates and flags survive; preview parameters do not', async () => {
  const calls = [];
  const handler = createProxyHandler({ fetchImpl(url) { calls.push(url); return Response.json({ synthetic: true }); } });
  const input = '?q=%E4%B8%AD+%E6%96%87&status=open&eo_token=private-preview&offset=0&%65o_time=123&limit=10&q=second&empty=&flag&%65o_token=duplicate';
  const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: DESTINATION },
    request: new Request(`${runtime()}/api/tickets${input}`) });
  assert.deepEqual(await response.json(), { synthetic: true });
  assert.deepEqual(calls, [`${DESTINATION}/tickets?q=%E4%B8%AD+%E6%96%87&status=open&offset=0&limit=10&q=second&empty=&flag`]);
  assert.equal(new URL(calls[0]).searchParams.get('q'), '中 文');
});

test('preview names are not business authentication and their values never reach upstream or logs', async () => {
  const logs = [];
  const originals = new Map(['log', 'info', 'warn', 'error'].map(name => [name, console[name]]));
  const seen = [];
  const handler = createProxyHandler({ fetchImpl(url, init) {
    seen.push([url, [...init.headers]]);
    assert.equal(new URL(url).search, '?q=eo_token%3Dpublic-text');
    return Response.json({ detail: 'login required' }, { status: 401 });
  } });
  try {
    for (const name of originals.keys()) console[name] = (...values) => logs.push(values);
    const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: DESTINATION }, request: new Request(
      `${runtime()}/api/auth/me?eo_token=PRIVATE_PREVIEW_TOKEN&eo_time=PRIVATE_PREVIEW_TIME&q=eo_token%3Dpublic-text`) });
    assert.equal(response.status, 401);
    await response.text();
  } finally { for (const [name, fn] of originals) console[name] = fn; }
  assert.deepEqual(logs, []);
  assert.doesNotMatch(JSON.stringify(seen), /PRIVATE_PREVIEW_TOKEN|PRIVATE_PREVIEW_TIME/);
});

test('caller URL/Host/XFH never chooses upstream or overrides the server proxy credential', async () => {
  let count = 0;
  const handler = createProxyHandler({ fetchImpl(url, init) {
    count += 1;
    assert.equal(url, `${DESTINATION}/tickets?q=https%3A%2F%2Fevil.example%2F`);
    assert.equal(init.headers.get('x-project-copilot-proxy-secret'), SECRET);
    for (const header of ['host', 'x-forwarded-host', 'forwarded', 'x-forwarded-for', 'authorization']) assert.equal(init.headers.get(header), null);
    assert.equal(init.redirect, 'manual');
    return Response.json({ synthetic: true });
  } });
  const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: DESTINATION }, request: new Request(
    'http://unrelated-internal.invalid:8080/api/tickets?q=https%3A%2F%2Fevil.example%2F', { headers: {
      host: 'evil.example', 'x-forwarded-host': 'evil.example', forwarded: 'host=evil.example',
      'x-forwarded-for': '127.0.0.1', 'x-project-copilot-proxy-secret': 'browser-forgery', authorization: 'Bearer forgery',
    } }) });
  assert.deepEqual(await response.json(), { synthetic: true });
  assert.equal(count, 1);
});

test('browser metadata and preview tokens do not grant sensitive GET access; upstream401/404 are preserved', async () => {
  const handler = createProxyHandler({ fetchImpl(url, init) {
    const cookie = init.headers.get('cookie');
    if (!cookie) return Response.json({ detail: 'synthetic authentication rejected' }, { status: 401 });
    if (cookie !== '__Host-pc_session=synthetic-alice') return Response.json({ detail: 'synthetic owner rejected' }, { status: 404 });
    return Response.json({ user: 'synthetic-alice' });
  } });
  for (const [cookie, expected] of [[undefined, 401], ['__Host-pc_session=synthetic-bob', 404], ['__Host-pc_session=synthetic-alice', 200]]) {
    const headers = { 'sec-fetch-site': 'same-origin', 'x-forwarded-host': new URL(SITE).host };
    if (cookie) headers.cookie = cookie;
    const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: DESTINATION },
      request: new Request(`${runtime()}/api/tickets/${ID}?eo_token=not-an-identity`, { headers }) });
    assert.equal(response.status, expected);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    const body = await response.json();
    if (expected !== 200) assert.equal(body.user, undefined);
  }
});

test('sensitive responses retain distinct host-only cookies but never gain CORS or shared-cache permission', async () => {
  const cookies = [
    '__Host-pc_session=synthetic; Secure; HttpOnly; SameSite=Lax; Path=/',
    'synthetic_csrf=value; Secure; SameSite=Lax; Path=/',
  ];
  const handler = createProxyHandler({ fetchImpl(_url, init) {
    assert.equal(init.headers.get('cookie'), '__Host-pc_session=synthetic');
    assert.equal(init.headers.get('x-csrf-token'), 'synthetic-csrf');
    const headers = new Headers({ 'content-type': 'application/json', 'cache-control': 'public, max-age=600',
      'access-control-allow-origin': '*', 'access-control-allow-credentials': 'true' });
    for (const cookie of cookies) headers.append('set-cookie', cookie);
    return new Response('{"synthetic":"private"}', { headers });
  } });
  const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: DESTINATION }, request: new Request(`${runtime()}/api/auth/me`, {
    headers: { cookie: '__Host-pc_session=synthetic', 'x-csrf-token': 'synthetic-csrf' },
  }) });
  assert.deepEqual(response.headers.getSetCookie(), cookies);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('access-control-allow-origin'), null);
  assert.equal(response.headers.get('access-control-allow-credentials'), null);
  await response.text();
});

test('routing changes preserve early method/Header/JSON/upload limits and zero body reading while CLOSED', async () => {
  const probe = closedProbe();
  for (const [path, options, status] of [
    ['/api/jobs', { method: 'PATCH' }, 404], ['/api/docs', {}, 404],
    ['/api/health', { headers: { upgrade: 'websocket' } }, 400],
    ['/api/health', { headers: { 'x-noise': 'a'.repeat(16384) } }, 431],
    ['/api/jobs', { method: 'POST', body: true, headers: { origin: SITE, 'content-type': 'application/json', 'content-length': '262145' } }, 413],
    ['/api/jobs/upload', { method: 'POST', body: true, headers: { origin: SITE, 'content-type': 'multipart/form-data; boundary=synthetic', 'content-length': '4259841' } }, 413],
    ['/api/jobs', { method: 'POST', body: true, headers: { origin: SITE, 'content-type': 'text/plain' } }, 415],
    ['/api/jobs', { method: 'POST', body: true, headers: { origin: SITE, 'content-type': 'application/json' } }, 503],
    ['/api/health', { body: true }, 400],
  ]) assert.equal((await probe.invoke(path, options)).status, status);
  probe.idle();
});

test('real Node HTTP loopback tolerates changing runtime Origins and enforces CLOSED before upstream/body work', async () => {
  const probe = closedProbe();
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    const request = { url: `${runtime()}${req.url}`, method: req.method, headers: new Headers(req.headers), signal: new AbortController().signal,
      body: req.method === 'GET' ? null : { getReader() { assert.fail('must not read HTTP bootstrap body'); } } };
    probe.handler({ env: ENV, request }).then(async response => {
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    }).catch(() => { res.writeHead(500); res.end('synthetic test failed'); });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  try {
    for (const [path, method, headers, body, expected] of [
      ['/api/health', 'GET', { 'sec-fetch-site': 'same-origin' }, undefined, 503],
      ['/api/auth/me?eo_token=private-preview&eo_time=123', 'GET', {}, undefined, 503],
      ['/api/jobs', 'PATCH', {}, undefined, 404],
      ['/api/jobs', 'POST', { origin: 'https://evil.example', 'content-type': 'application/json' }, '{}', 403],
      ['/api/health', 'GET', { origin: 'https://evil.example' }, undefined, 403],
      ['/api/../api/health', 'GET', {}, undefined, 404],
      ['/api/health?q=%GG', 'GET', {}, undefined, 404],
      ['/api/health', 'GET', { upgrade: 'websocket' }, undefined, 400],
      ['/api/jobs', 'POST', { origin: SITE, 'content-type': 'application/json', 'content-length': '262145' }, '{}', 413],
    ]) {
      const result = await new Promise((resolve, reject) => {
        const call = http.request({ hostname: '127.0.0.1', port: server.address().port, path, method,
          headers: { ...headers, connection: 'close' } }, response => {
          const parts = [];
          response.on('data', part => parts.push(part));
          response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(parts).toString() }));
        });
        call.on('error', reject); call.end(body);
      });
      assert.equal(result.status, expected, `${method} ${path}`);
      assert.doesNotMatch(result.body, /private-preview|reviewed-synthetic|synthetic-origin-policy-secret/);
    }
    probe.idle();
  } finally {
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});
