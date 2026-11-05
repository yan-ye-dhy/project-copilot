/** Synthetic runtime observations, never a proof of EdgeOne URL rewriting. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const SITE = 'https://project-synthetic.edgeone.cool';
const DEPLOYMENT = 'https://deployment-synthetic.edgeone.cool';
const SECRET = 'synthetic-diagnostic-proxy-secret-123456';
const ENV = { PROJECT_COPILOT_SITE_ORIGIN: SITE, PROJECT_COPILOT_PROXY_SECRET: SECRET };
const neverFetch = () => assert.fail('bootstrap diagnostics must not contact any upstream');

function context(url, headers = {}, extra = {}) {
  return { env: ENV, request: new Request(url, { headers, ...extra }) };
}

function observer(options = {}) {
  const records = [];
  return { records, handler: createProxyHandler({ fetchImpl: neverFetch, originDiagnosticLog: (record) => records.push(record), ...options }) };
}

test('project URL GET without Origin passes policy and remains503 with no Tunnel', async () => {
  const { handler, records } = observer();
  for (const path of ['/health', '/auth/me']) {
    const response = await handler(context(`${SITE}/api${path}`, { 'sec-fetch-site': 'same-origin' }));
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { detail: '服务暂时不可用。' });
    assert.equal(response.headers.get('cache-control'), 'no-store');
  }
  assert.equal(records.length, 2);
  assert.deepEqual(records[0], {
    event: 'pc_proxy_origin_v1', route: 'health', outcome: 'preflight_pass_no_backend',
    request_url_origin: SITE, expected_site_origin: SITE, url_origin_matches: true,
    origin_header_present: false, origin_header_matches: null,
    fetch_site_present: true, fetch_site_allowed: true,
    host_header_present: false, host_header_matches_site: null, backend_destination_configured: false,
  });
});

test('project/deployment URL mismatch is diagnosed but NEVER admitted on Host or forwarded hints', async () => {
  for (const headers of [
    { 'sec-fetch-site': 'same-origin' },
    { host: new URL(SITE).host, 'x-forwarded-host': new URL(SITE).host, forwarded: `host=${new URL(SITE).host}`, 'sec-fetch-site': 'same-origin' },
    { origin: SITE, 'sec-fetch-site': 'same-origin' },
  ]) {
    const { handler, records } = observer();
    const response = await handler(context(`${DEPLOYMENT}/api/health`, headers));
    assert.equal(response.status, 403);
    assert.deepEqual(await response.json(), { detail: '请求来源不允许。' });
    assert.equal(records[0].outcome, 'url_origin_mismatch');
    assert.equal(records[0].request_url_origin, DEPLOYMENT);
    assert.equal(records[0].expected_site_origin, SITE);
    assert.equal(records[0].url_origin_matches, false);
    assert.equal(records[0].backend_destination_configured, false);
  }
});

test('runtime scheme differences stay403 and identify the first branch', async () => {
  const { handler, records } = observer();
  assert.equal((await handler(context(`${SITE.replace('https:', 'http:')}/api/health`))).status, 403);
  assert.equal(records[0].outcome, 'url_origin_mismatch');
  assert.equal(records[0].request_url_origin, SITE.replace('https:', 'http:'));
});

test('runtime Origin and Fetch-Site mismatches are distinguished without logging their values', async () => {
  for (const [headers, outcome] of [
    [{ origin: 'https://wrong.example', 'sec-fetch-site': 'same-origin' }, 'origin_header_mismatch'],
    [{ origin: '', 'sec-fetch-site': 'same-origin' }, 'origin_header_mismatch'],
    [{ 'sec-fetch-site': 'cross-site' }, 'fetch_site_disallowed'],
    [{ 'sec-fetch-site': 'unexpected-sensitive-marker' }, 'fetch_site_disallowed'],
  ]) {
    const { handler, records } = observer();
    assert.equal((await handler(context(`${SITE}/api/auth/me`, headers))).status, 403);
    assert.equal(records[0].outcome, outcome);
    assert.doesNotMatch(JSON.stringify(records), /wrong\.example|unexpected-sensitive-marker/);
  }
});

test('illegal methods and write Origin still reject; no diagnostic HTTP endpoint is added', async () => {
  const { handler, records } = observer();
  assert.equal((await handler(context(`${SITE}/api/health`, {}, { method: 'PATCH' }))).status, 404);
  assert.equal((await handler(context(`${SITE}/api/origin-diagnostics`))).status, 404);
  assert.equal((await handler(context(`${SITE}/api/jobs`, { origin: 'https://wrong.example', 'content-type': 'application/json' },
    { method: 'POST', body: '{}' }))).status, 403);
  assert.equal(records.length, 0);
});

test('logs omit query, eo_token, credentials, headers, account, body and backend address', async () => {
  const { handler, records } = observer();
  const headers = {
    cookie: 'private-cookie-marker', authorization: 'Bearer private-account-password',
    'x-project-copilot-proxy-secret': SECRET, 'x-csrf-token': 'private-csrf-marker',
    'x-copilot-context-token': 'private-context-marker',
    'x-forwarded-host': 'private-forwarded-host-marker', 'sec-fetch-site': 'same-origin',
  };
  let readBody = false;
  const response = await handler({ env: ENV, request: {
    url: `${DEPLOYMENT}/api/health?eo_token=private-preview-token&q=private-query-marker#private-fragment-marker`,
    headers: new Headers(headers), method: 'GET', signal: new AbortController().signal,
    body: { getReader() { readBody = true; throw new Error('private-body-marker'); } },
  } });
  assert.equal(response.status, 403);
  assert.equal(readBody, false);
  const serialized = JSON.stringify(records);
  for (const marker of [SECRET, 'eo_token', 'private-preview-token', 'private-query-marker', 'private-fragment-marker',
    'private-cookie-marker', 'private-account-password', 'private-csrf-marker', 'private-context-marker',
    'private-forwarded-host-marker', 'private-body-marker', 'trycloudflare']) assert(!serialized.includes(marker), marker);
  assert.equal(records[0].request_url_origin, DEPLOYMENT);
  assert(!JSON.stringify(await response.json()).includes('request_url_origin'));
});

test('userinfo, secret-bearing hostname, invalid and oversized URL never enter diagnostics verbatim', async () => {
  for (const [url, recorded] of [
    ['https://private-user:private-pass@deployment-synthetic.edgeone.cool/api/health?eo_token=hidden', true],
    [`https://${SECRET}.example/api/health`, true],
    [`https://${Array.from({ length: 8 }, () => 'a'.repeat(50)).join('.')}.example/api/health`, true],
    ['not-a-url?eo_token=hidden', false],
  ]) {
    const { handler, records } = observer();
    const response = await handler({ env: ENV, request: { url, method: 'GET', headers: new Headers(), body: null } });
    assert.equal(response.status, recorded ? 403 : 404);
    assert.equal(records.length, recorded ? 1 : 0);
    if (recorded) assert.equal(records[0].request_url_origin, '[redacted]');
    assert.doesNotMatch(JSON.stringify(records), /private-user|private-pass|hidden|eo_token/);
    assert(!JSON.stringify(records).includes(SECRET.toLowerCase()));
  }
});

test('logging failures and per-handler two-record budget cannot alter admission', async () => {
  const { handler, records } = observer();
  for (let i = 0; i < 5; i += 1) assert.equal((await handler(context(`${DEPLOYMENT}/api/health`))).status, 403);
  assert.equal(records.length, 2);
  const failedLogger = observer({ originDiagnosticLog() { throw new Error('private-log-failure'); } }).handler;
  assert.equal((await failedLogger(context(`${DEPLOYMENT}/api/health`))).status, 403);
  assert.equal((await failedLogger(context(`${SITE}/api/health`))).status, 503);
});

test('diagnostic does not run for configured backend, invalid configuration or other routes', async () => {
  const { handler, records } = observer();
  const configured = { ...ENV, PROJECT_COPILOT_BACKEND_ORIGIN: 'https://synthetic.trycloudflare.com' };
  assert.equal((await handler({ ...context(`${DEPLOYMENT}/api/health`), env: configured })).status, 403);
  assert.equal((await handler(context(`${SITE}/api/tickets`))).status, 503);
  assert.equal((await handler({ ...context(`${SITE}/api/health`), env: {} })).status, 503);
  assert.equal(records.length, 0);
});
