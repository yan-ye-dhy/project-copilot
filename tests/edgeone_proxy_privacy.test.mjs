/** Real local Node HTTP transport with a CONTROLLED authentication server.
 * This proves proxy preservation/containment, not real FastAPI sessions,
 * database ownership, EdgeOne routing, browser cookie policy or cloud security.
 */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const SITE = 'https://copilot.example';
const BACKEND = 'https://privacy-synthetic.trycloudflare.com';
const PROXY_SECRET = 'synthetic-privacy-proxy-secret-123456789';
const ALICE_TICKET = '11111111-1111-4111-8111-111111111111';
const BOB_TICKET = '22222222-2222-4222-8222-222222222222';
const ACTION = '33333333-3333-4333-8333-333333333333';
const ENV = { PROJECT_COPILOT_SITE_ORIGIN: SITE, PROJECT_COPILOT_BACKEND_ORIGIN: BACKEND,
  PROJECT_COPILOT_PROXY_SECRET: PROXY_SECRET };
const SESSION_COOKIE = '__Host-pc_session=alice-session; Path=/; Secure; HttpOnly; SameSite=Lax';
const CSRF_COOKIE = 'helper_csrf=synthetic; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/; Secure; SameSite=Lax';
let server;
let localOrigin;
let seen = [];
let override = null;
const sockets = new Set();

function reply(res, status, data, headers = {}) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'public, max-age=3600',
    'access-control-allow-origin': '*', 'access-control-allow-credentials': 'true',
    'access-control-expose-headers': 'set-cookie,x-internal-auth', 'vary': 'origin',
    'x-internal-auth': 'private-upstream-auth-marker', ...headers });
  res.end(JSON.stringify(data));
}

before(async () => {
  server = http.createServer(async (req, res) => {
    seen.push({ url: req.url, method: req.method, headers: { ...req.headers } });
    if (override) return override(req, res);
    if (req.headers['x-project-copilot-proxy-secret'] !== PROXY_SECRET) return reply(res, 403, { detail: 'proxy rejected' });
    const cookie = req.headers.cookie || '';
    const user = cookie === '__Host-pc_session=alice-session' ? 'alice' : cookie === '__Host-pc_session=bob-session' ? 'bob' : null;
    const path = new URL(req.url, 'http://controlled.local').pathname;
    if (path === '/auth/login' && req.method === 'POST') {
      return reply(res, 200, { user: { username: 'alice' }, csrf_token: 'alice-csrf' },
        { 'set-cookie': [SESSION_COOKIE, CSRF_COOKIE] });
    }
    if (!user) return reply(res, 401, { detail: 'controlled server requires a valid session' });
    if (path === '/auth/me') return reply(res, 200, { user: { username: user }, csrf_token: `${user}-csrf` });
    if (path === '/tickets') return reply(res, 200, { items: [{ id: user === 'alice' ? ALICE_TICKET : BOB_TICKET, title: `${user}-private-ticket` }] });
    if (path === `/tickets/${ALICE_TICKET}` && user === 'alice') return reply(res, 200, { id: ALICE_TICKET, title: 'alice-private-ticket' });
    if (path === `/tickets/${BOB_TICKET}` && user === 'bob') return reply(res, 200, { id: BOB_TICKET, title: 'bob-private-ticket' });
    if (path === `/actions/${ACTION}/confirm` && req.method === 'POST') {
      if (req.headers['x-csrf-token'] !== `${user}-csrf`) return reply(res, 403, { detail: 'controlled CSRF rejected' });
      return reply(res, 200, { action_id: ACTION, status: 'confirmed', ticket_id: ALICE_TICKET, synthetic_only: true });
    }
    return reply(res, 404, { detail: 'controlled resource not found' });
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  localOrigin = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  for (const socket of sockets) socket.destroy();
  if (server?.listening) await new Promise(resolve => server.close(resolve));
});

function reset() { seen = []; override = null; }

function invoke(path, { method = 'GET', headers = {}, body, runtimeOrigin = SITE } = {}) {
  const request = new Request(`${runtimeOrigin}${path}`, { method, headers, body });
  return createProxyHandler({ timeoutMs: 1500, fetchImpl(input, init) {
    const target = new URL(input);
    assert.equal(target.origin, BACKEND, 'request URL or client headers must not pick the upstream');
    return fetch(`${localOrigin}${target.pathname}${target.search}`, init);
  } })({ request, env: ENV });
}

function assertPrivateHeaders(response) {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  assert.equal(response.headers.get('access-control-allow-origin'), null);
  assert.equal(response.headers.get('access-control-allow-credentials'), null);
  assert.equal(response.headers.get('access-control-expose-headers'), null);
  assert.equal(response.headers.get('x-internal-auth'), null);
  assert.equal(response.headers.get('x-project-copilot-proxy-secret'), null);
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
}

test('same-origin hints and forged proxy headers do not authenticate sensitive GETs', async () => {
  reset();
  for (const path of ['/api/auth/me', '/api/tickets', `/api/tickets/${ALICE_TICKET}`, `/api/jobs/${ALICE_TICKET}`,
    '/api/auth/me?eo_token=private-preview-auth-marker&eo_time=123']) {
    const response = await invoke(path, { headers: { 'sec-fetch-site': 'same-origin',
      'x-project-copilot-proxy-secret': 'attacker-browser-marker', authorization: 'Bearer fake-identity' } });
    assert.equal(response.status, 401);
    assertPrivateHeaders(response);
    const body = await response.text();
    assert.doesNotMatch(body, /alice-private-ticket|bob-private-ticket|alice-csrf|synthetic-privacy-proxy-secret|fake-identity/);
  }
  assert.equal(seen.length, 5);
  for (const call of seen) {
    assert.equal(call.headers['x-project-copilot-proxy-secret'], PROXY_SECRET);
    assert.equal(call.headers.authorization, undefined);
    assert.doesNotMatch(call.url, /private-preview-auth-marker|eo_token|eo_time/);
  }
});

test('missing browser metadata does not bypass controlled sessions or prevent legitimate CLI GET', async () => {
  reset();
  const unauthenticated = await invoke('/api/auth/me');
  assert.equal(unauthenticated.status, 401);
  assertPrivateHeaders(unauthenticated);
  await unauthenticated.arrayBuffer();
  const authenticated = await invoke('/api/auth/me', { headers: { cookie: '__Host-pc_session=alice-session' } });
  assert.equal(authenticated.status, 200);
  assertPrivateHeaders(authenticated);
  assert.deepEqual(await authenticated.json(), { user: { username: 'alice' }, csrf_token: 'alice-csrf' });
});

test('known preview cookie names are removed defensively without changing sessions or hiding duplicates', async () => {
  reset();
  const response = await invoke('/api/auth/me', { headers: {
    cookie: 'eo_token=private-preview-cookie; __Host-pc_session=alice-session; eo_time=private-preview-time',
  } });
  assert.equal(response.status, 200);
  await response.arrayBuffer();
  assert.equal(seen[0].headers.cookie, '__Host-pc_session=alice-session');
  assert.doesNotMatch(JSON.stringify(seen), /private-preview-cookie|private-preview-time/);
  override = (_req, res) => reply(res, 401, { detail: 'controlled duplicate-cookie rejection' });
  const duplicate = await invoke('/api/auth/me', { headers: {
    cookie: '__Host-pc_session=one; eo_token=private; __Host-pc_session=two; helper_csrf=value; eo_time=123',
  } });
  assert.equal(duplicate.status, 401);
  await duplicate.arrayBuffer();
  assert.equal(seen[1].headers.cookie, '__Host-pc_session=one; __Host-pc_session=two; helper_csrf=value');
});

test('cookie identity and controlled ownership survive proxying and a previous successful response is never cached', async () => {
  reset();
  const alice = await invoke(`/api/tickets/${ALICE_TICKET}`, { headers: { cookie: '__Host-pc_session=alice-session' } });
  assert.equal(alice.status, 200);
  assertPrivateHeaders(alice);
  assert.equal((await alice.json()).title, 'alice-private-ticket');
  const anonymous = await invoke(`/api/tickets/${ALICE_TICKET}`);
  assert.equal(anonymous.status, 401);
  assert.doesNotMatch(await anonymous.text(), /alice-private-ticket/);
  const other = await invoke(`/api/tickets/${ALICE_TICKET}`, { headers: { cookie: '__Host-pc_session=bob-session' } });
  assert.equal(other.status, 404);
  assertPrivateHeaders(other);
  assert.doesNotMatch(await other.text(), /alice-private-ticket/);
  assert.equal(seen.length, 3, 'each identity must reach the controlled authorization boundary independently');
});

test('session plus same-origin is insufficient for a controlled write without the matching CSRF token', async () => {
  reset();
  const headers = { cookie: '__Host-pc_session=alice-session', origin: SITE, 'content-type': 'application/json',
    'sec-fetch-site': 'same-origin', 'x-copilot-context-id': ACTION, 'x-copilot-context-token': 'synthetic-context' };
  for (const csrf of [undefined, 'wrong-csrf']) {
    const response = await invoke(`/api/actions/${ACTION}/confirm`, { method: 'POST', body: '{}',
      headers: { ...headers, ...(csrf ? { 'x-csrf-token': csrf } : {}) } });
    assert.equal(response.status, 403);
    assertPrivateHeaders(response);
    await response.arrayBuffer();
  }
  const accepted = await invoke(`/api/actions/${ACTION}/confirm`, { method: 'POST', body: '{}',
    headers: { ...headers, 'x-csrf-token': 'alice-csrf' } });
  assert.equal(accepted.status, 200);
  assertPrivateHeaders(accepted);
  assert.equal((await accepted.json()).synthetic_only, true);
  assert.equal(seen[2].headers.cookie, headers.cookie);
  assert.equal(seen[2].headers['x-csrf-token'], 'alice-csrf');
  assert.equal(seen[2].headers['x-copilot-context-token'], 'synthetic-context');
});

test('public session cookie flags and a second Expires cookie remain distinct and host-only', async () => {
  reset();
  const response = await invoke('/api/auth/login', { method: 'POST', body: '{}',
    headers: { origin: SITE, 'content-type': 'application/json' } });
  assert.equal(response.status, 200);
  assertPrivateHeaders(response);
  assert.deepEqual(response.headers.getSetCookie(), [SESSION_COOKIE, CSRF_COOKIE]);
  for (const cookie of response.headers.getSetCookie()) assert.doesNotMatch(cookie, /(?:^|;)\s*domain\s*=/i);
  assert.match(response.headers.getSetCookie()[0], /; Secure; HttpOnly; SameSite=Lax$/);
  await response.arrayBuffer();
});

test('a Domain-scoped session cookie is refused rather than transferred to a different site', async () => {
  reset();
  override = (_req, res) => reply(res, 200, { status: 'untrusted' },
    { 'set-cookie': '__Host-pc_session=private-cookie-marker; Domain=trycloudflare.com; Path=/; Secure; HttpOnly; SameSite=Lax' });
  const response = await invoke('/api/auth/me', { headers: { cookie: '__Host-pc_session=alice-session' } });
  assert.equal(response.status, 502);
  assert.equal(response.headers.get('set-cookie'), null);
  assertPrivateHeaders(response);
  assert.doesNotMatch(await response.text(), /private-cookie-marker|trycloudflare|synthetic-privacy-proxy-secret/);
});

test('public session cookies fail closed when required protections are missing, weakened or ambiguous', async () => {
  for (const attributes of [
    'Path=/; HttpOnly; SameSite=Lax',
    'Path=/; Secure; SameSite=Lax',
    'Path=/; Secure; HttpOnly',
    'Path=/; Secure; HttpOnly; SameSite=None',
    'Path=/other; Secure; HttpOnly; SameSite=Lax',
    'Path=/; Path=/; Secure; HttpOnly; SameSite=Lax',
    'Path=/; Secure; Secure; HttpOnly; SameSite=Lax',
    'Path=/; Secure; HttpOnly; HttpOnly; SameSite=Lax',
    'Path=/; Secure; HttpOnly; SameSite=Lax; SameSite=Lax',
    'Path=/; Secure=true; HttpOnly; SameSite=Lax',
    'Path=/; Secure; HttpOnly=true; SameSite=Lax',
  ]) {
    reset();
    override = (_req, res) => reply(res, 200, { status: 'untrusted' },
      { 'set-cookie': `__Host-pc_session=private-cookie-marker; ${attributes}` });
    const response = await invoke('/api/auth/me', { headers: { cookie: '__Host-pc_session=alice-session' } });
    assert.equal(response.status, 502, attributes);
    assert.equal(response.headers.get('set-cookie'), null);
    assertPrivateHeaders(response);
    assert.doesNotMatch(await response.text(), /private-cookie-marker|trycloudflare|synthetic-privacy-proxy-secret/);
  }
});

test('session protection attributes are case-insensitive and a secure expiration cookie is preserved', async () => {
  reset();
  const expired = '__Host-pc_session=""; expires=Thu, 01 Jan 1970 00:00:00 GMT; Max-Age=0; path=/; SECURE; httponly; samesite=lax';
  override = (_req, res) => reply(res, 200, { status: 'controlled expiration' }, { 'set-cookie': [expired, CSRF_COOKIE] });
  const response = await invoke('/api/auth/me', { headers: { cookie: '__Host-pc_session=alice-session' } });
  assert.equal(response.status, 200);
  assertPrivateHeaders(response);
  assert.deepEqual(response.headers.getSetCookie(), [expired, CSRF_COOKIE]);
  await response.arrayBuffer();
});

test('preview parameters are stripped before the HTTP upstream while business query values remain intact', async () => {
  reset();
  const captured = [];
  const originals = new Map(['info', 'log', 'warn', 'error'].map(name => [name, console[name]]));
  try {
    for (const name of originals.keys()) console[name] = (...values) => captured.push(values);
    const response = await invoke('/api/tickets?q=%E4%B8%AD%E6%96%87%20eo_token%3Dbusiness&status=open&eo_token=private-preview-one&eo_time=123&eo%5Ftoken=private-preview-two&q=second',
      { headers: { cookie: '__Host-pc_session=alice-session' } });
    assert.equal(response.status, 200);
    await response.arrayBuffer();
    const invalid = await invoke('/api/docs?eo_token=private-preview-rejected');
    assert.equal(invalid.status, 404);
    assert.doesNotMatch(await invalid.text(), /private-preview-rejected/);
  } finally {
    for (const [name, original] of originals) console[name] = original;
  }
  assert.deepEqual(captured, []);
  assert.equal(seen.length, 1);
  const query = new URL(seen[0].url, 'http://controlled.local').searchParams;
  assert.deepEqual(query.getAll('q'), ['中文 eo_token=business', 'second']);
  assert.equal(query.get('status'), 'open');
  assert.equal(query.has('eo_token'), false);
  assert.equal(query.has('eo_time'), false);
  assert.doesNotMatch(seen[0].url, /private-preview-one|private-preview-two/);
});
