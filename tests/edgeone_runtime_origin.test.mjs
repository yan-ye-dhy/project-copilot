/** The runtime Origin is from user-provided cloud logs. Requests remain synthetic;
 * the loopback case tests real Node HTTP, not Tencent's routing or authentication.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const SITE = 'https://project-copilot-2v2ym9bf.edgeone.cool';
const RUNTIME = 'http://pages-pro-41-af5f.pages-scf-gz-pro.qcloudteo.com';
const ENV = { PROJECT_COPILOT_SITE_ORIGIN: SITE, PROJECT_COPILOT_PROXY_SECRET: 'synthetic-runtime-origin-secret-12345' };
const ID = '11111111-2222-4333-8444-555555555555';

function bootstrap() {
  let fetchCalls = 0;
  let readers = 0;
  const handler = createProxyHandler({ fetchImpl() {
    fetchCalls += 1;
    assert.fail('an unconfigured Tunnel must never be contacted');
  } });
  const invoke = (path = '/api/health', { base = RUNTIME, method = 'GET', headers = {}, env = ENV, body = false } = {}) => handler({ env, request: {
    url: `${base}${path}`, method, headers: new Headers(headers), signal: new AbortController().signal,
    body: body ? { getReader() { readers += 1; assert.fail('bootstrap request body must remain unread'); } } : null,
  } });
  return { handler, invoke, assertIdle() { assert.equal(fetchCalls, 0); assert.equal(readers, 0); } };
}

test('observed internal runtime accepts browser-like health and auth/me GET policy then closes503', async () => {
  const probe = bootstrap();
  for (const path of ['/api/health', '/api/auth/me']) {
    const response = await probe.invoke(path, { headers: { 'sec-fetch-site': 'same-origin' } });
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { detail: '服务暂时不可用。' });
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
  probe.assertIdle();
});

test('direct exact HTTPS site still works without trusting Host or forwarding hints', async () => {
  const probe = bootstrap();
  for (const headers of [{}, { host: 'untrusted.example', 'x-forwarded-host': 'untrusted.example', forwarded: 'host=untrusted.example' }]) {
    assert.equal((await probe.invoke('/api/health', { base: SITE, headers })).status, 503);
  }
  probe.assertIdle();
});

test('only the reviewed site/runtime pair enables internal HTTP compatibility', async () => {
  const probe = bootstrap();
  for (const site of ['https://another.edgeone.cool', 'https://copilot.example']) {
    const env = { ...ENV, PROJECT_COPILOT_SITE_ORIGIN: site };
    assert.equal((await probe.invoke('/api/health', { env })).status, 403);
    assert.equal((await probe.invoke('/api/health', { env, base: site })).status, 503);
  }
  probe.assertIdle();
});

test('internal URL supplies only a route, never the upstream destination or browser Host identity', async () => {
  const destination = 'https://reviewed-synthetic.trycloudflare.com';
  let calls = 0;
  const handler = createProxyHandler({ fetchImpl(url, init) {
    calls += 1;
    assert.equal(url, `${destination}/tickets?q=synthetic`);
    assert.equal(init.headers.get('x-project-copilot-proxy-secret'), ENV.PROJECT_COPILOT_PROXY_SECRET);
    assert.equal(init.headers.get('host'), null);
    assert.equal(init.headers.get('x-forwarded-host'), null);
    assert.equal(init.headers.get('forwarded'), null);
    assert.equal(init.redirect, 'manual');
    return Response.json({ synthetic: true });
  } });
  const response = await handler({ env: { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: destination },
    request: new Request(`${RUNTIME}/api/tickets?q=synthetic`, { headers: {
      host: 'untrusted.example', 'x-forwarded-host': 'untrusted.example', forwarded: 'host=untrusted.example',
      origin: SITE, 'sec-fetch-site': 'same-origin',
    } }),
  });
  assert.deepEqual(await response.json(), { synthetic: true });
  assert.equal(calls, 1); // In-memory fetch replacement; no actual Tunnel/network.
});

test('arbitrary cloud suffixes, deployment URLs, schemes, ports and userinfo remain untrusted', async () => {
  const probe = bootstrap();
  for (const base of [
    'http://another.pages-scf-gz-pro.qcloudteo.com',
    'http://pages-pro-41-af5f.pages-scf-gz-pro.qcloudteo.com.evil.example',
    'http://pages-pro-42-af5f.pages-scf-gz-pro.qcloudteo.com',
    'https://pages-pro-41-af5f.pages-scf-gz-pro.qcloudteo.com',
    `${RUNTIME}:8080`,
    'http://user:pass@pages-pro-41-af5f.pages-scf-gz-pro.qcloudteo.com',
    'https://user:pass@project-copilot-2v2ym9bf.edgeone.cool',
    'https://deployment-synthetic.edgeone.cool',
    'http://127.0.0.1:8002',
  ]) {
    const response = await probe.invoke('/api/health', { base, headers: {
      host: new URL(SITE).host, 'x-forwarded-host': new URL(SITE).host, origin: SITE, 'sec-fetch-site': 'same-origin',
    } });
    assert.equal(response.status, 403, base);
  }
  probe.assertIdle();
});

test('runtime transport does not grant cross-site reads or writes through forged Host/XFH', async () => {
  const probe = bootstrap();
  for (const [method, path] of [['GET', '/api/health'], ['POST', '/api/jobs'], ['DELETE', `/api/documents/${ID}`]]) {
    for (const headers of [
      { origin: 'https://evil.example', 'sec-fetch-site': 'same-origin' },
      { origin: SITE, 'sec-fetch-site': 'cross-site' },
      { origin: SITE, 'sec-fetch-site': 'same-site' },
    ]) {
      assert.equal((await probe.invoke(path, { method, body: method !== 'GET', headers: {
        host: new URL(SITE).host, 'x-forwarded-host': new URL(SITE).host, 'content-type': 'application/json', ...headers,
      } })).status, 403);
    }
  }
  probe.assertIdle();
});

test('write Origin remains mandatory, fixed to public SITE rather than internal runtime', async () => {
  const probe = bootstrap();
  for (const method of ['POST', 'DELETE']) {
    const path = method === 'POST' ? '/api/jobs' : `/api/documents/${ID}`;
    for (const origin of [undefined, '', RUNTIME, 'null']) {
      const headers = { 'content-type': 'application/json', 'sec-fetch-site': 'same-origin' };
      if (origin !== undefined) headers.origin = origin;
      assert.equal((await probe.invoke(path, { method, headers, body: true })).status, 403);
    }
    assert.equal((await probe.invoke(path, { method, headers: { origin: SITE, 'content-type': 'application/json' }, body: true })).status, 503);
  }
  probe.assertIdle();
});

test('internal transport preserves methods, paths and early Header/body rejection', async () => {
  const probe = bootstrap();
  for (const [path, options, expected] of [
    ['/api/jobs', { method: 'PATCH' }, 404],
    ['/api/health', { method: 'POST' }, 404],
    ['/api/docs', {}, 404],
    ['/api/%68ealth', {}, 404],
    ['/api//health', {}, 404],
    ['/api/health', { headers: { upgrade: 'websocket' } }, 400],
    ['/api/health', { headers: { 'x-noise': 'x'.repeat(16384) } }, 431],
    ['/api/jobs', { method: 'POST', headers: { origin: SITE, 'content-type': 'application/json', 'content-length': '262145' }, body: true }, 413],
    ['/api/jobs/upload', { method: 'POST', headers: { origin: SITE, 'content-type': 'multipart/form-data; boundary=synthetic', 'content-length': '4259841' }, body: true }, 413],
    ['/api/jobs', { method: 'POST', headers: { origin: SITE, 'content-type': 'application/json', 'content-encoding': 'gzip' }, body: true }, 415],
    ['/api/health', { body: true }, 400],
  ]) assert.equal((await probe.invoke(path, options)).status, expected, `${options.method || 'GET'} ${path}`);
  probe.assertIdle();
});

test('CLOSED bootstrap errors reveal no credentials, query strings or internal origin', async () => {
  const probe = bootstrap();
  for (const [path, options, expected] of [
    ['/api/health?eo_token=private-preview-token&q=private-query', {}, 503],
    ['/api/jobs', { method: 'POST', body: true, headers: { origin: 'https://private-origin.example', 'content-type': 'application/json', cookie: 'private-cookie' } }, 403],
  ]) {
    const response = await probe.invoke(path, options);
    assert.equal(response.status, expected);
    const body = await response.text();
    for (const marker of [RUNTIME, ENV.PROJECT_COPILOT_PROXY_SECRET, 'private-preview-token', 'private-query', 'private-cookie', 'private-origin']) {
      assert(!body.includes(marker), marker);
    }
  }
  probe.assertIdle();
});

test('temporary origin diagnostics no longer emit runtime or site information', async () => {
  const captured = [];
  const originals = new Map(['info', 'log', 'warn', 'error'].map(name => [name, console[name]]));
  const probe = bootstrap();
  try {
    for (const name of originals.keys()) console[name] = (...values) => captured.push(values);
    assert.equal((await probe.invoke('/api/health')).status, 503);
    assert.equal((await probe.invoke('/api/auth/me')).status, 503);
    assert.equal((await probe.invoke('/api/health', { base: 'https://untrusted.example' })).status, 403);
  } finally {
    for (const [name, original] of originals) console[name] = original;
  }
  assert.deepEqual(captured, []);
  probe.assertIdle();
});

test('real Node loopback reproduces the internal runtime mapping with zero upstream calls', async () => {
  const probe = bootstrap();
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    const request = {
      url: `${RUNTIME}${req.url}`, method: req.method, headers: new Headers(req.headers), signal: new AbortController().signal,
      body: req.method === 'GET' ? null : { getReader() { assert.fail('bootstrap body read'); } },
    };
    probe.handler({ env: ENV, request }).then(async response => {
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(Buffer.from(await response.arrayBuffer()));
    }).catch(error => { res.writeHead(500); res.end('controlled test failed'); });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const local = `http://127.0.0.1:${server.address().port}`;
  try {
    for (const [path, init, expected] of [
      ['/api/health', { headers: { 'sec-fetch-site': 'same-origin' } }, 503],
      ['/api/auth/me', {}, 503],
      ['/api/jobs', { method: 'PATCH' }, 404],
      ['/api/jobs', { method: 'POST', headers: { origin: 'https://evil.example', 'content-type': 'application/json' }, body: '{}' }, 403],
      ['/api/docs', {}, 404],
      ['/api/health', { headers: { upgrade: 'websocket' } }, 400],
      ['/api/jobs', { method: 'POST', headers: { origin: SITE, 'content-type': 'application/json', 'content-length': '262145' }, body: '{}' }, 413],
    ]) {
      // Native fetch rejects Upgrade locally; use a raw Node HTTP client below.
      const result = await new Promise((resolve, reject) => {
        const call = http.request(`${local}${path}`, { method: init.method || 'GET', headers: { ...init.headers, connection: 'close' } }, response => {
          const parts = [];
          response.on('data', part => parts.push(part));
          response.on('end', () => resolve({ status: response.statusCode, body: Buffer.concat(parts).toString() }));
        });
        call.on('error', reject); call.end(init.body);
      });
      assert.equal(result.status, expected);
      assert(!result.body.includes(RUNTIME));
    }
    probe.assertIdle();
  } finally {
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
  }
});
