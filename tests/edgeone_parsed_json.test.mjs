/** Parsed-runtime JSON fallback, raw-byte precedence and real localhost transport. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const site = 'https://copilot.example';
const backend = 'https://parsed-json-test.trycloudflare.com';
const env = { PROJECT_COPILOT_SITE_ORIGIN: site, PROJECT_COPILOT_BACKEND_ORIGIN: backend,
  PROJECT_COPILOT_PROXY_SECRET: 'synthetic-json-secret-'.padEnd(48, 'x') };
let server, base, seen = [];
const sockets = new Set();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

before(async () => {
  server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    seen.push({ body: Buffer.concat(chunks), type: req.headers['content-type'], path: req.url });
    res.writeHead(req.url === '/jobs/upload' ? 200 : 422, { 'content-type': 'application/json' });
    res.end('{"detail":"controlled validation"}');
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  for (const socket of sockets) socket.destroy();
  if (server?.listening) await new Promise(resolve => server.close(resolve));
});

function invoke(body, { path = '/api/auth/login', headers = {}, requestFields = {}, ...options } = {}) {
  const request = { url: `http://runtime.internal${path}`, method: 'POST', body,
    headers: new Headers({ origin: site, 'content-type': 'application/json', ...headers }), ...requestFields };
  return createProxyHandler({ timeoutMs: 1500, fetchImpl: async (input, init) => {
    const target = new URL(input);
    assert.equal(target.origin, backend);
    assert(Buffer.isBuffer(init.body));
    return fetch(`${base}${target.pathname}${target.search}`, init);
  }, ...options })({ request, env });
}
async function consume(response, status) {
  assert.equal(response.status, status);
  await response.text();
}
const mustNotFetch = () => assert.fail('rejected input must not contact upstream');

test('parsed plain {} reaches the HTTP backend as JSON and preserves backend 422', async () => {
  seen = [];
  await consume(await invoke({}), 422);
  assert.equal(seen.length, 1);
  assert.deepEqual(seen[0].body, Buffer.from('{}'));
  assert.equal(seen[0].type, 'application/json');
});

test('ordinary and null-prototype JSON trees retain backend meaning and UTF-8', async () => {
  const value = Object.assign(Object.create(null), { question: '蓝鹤', nested: [1, true, null, { text: '竹' }] });
  seen = [];
  await consume(await invoke(value), 422);
  assert.deepEqual(JSON.parse(seen[0].body.toString('utf8')), { question: '蓝鹤', nested: [1, true, null, { text: '竹' }] });
});

test('parsed JSON arrays retain JSON types for backend validation', async () => {
  seen = [];
  await consume(await invoke([{ question: '竹' }, true, 1, null]), 422);
  assert.deepEqual(JSON.parse(seen[0].body.toString()), [{ question: '竹' }, true, 1, null]);
});

test('existing rawBody bytes win over a parsed object without invoking its accessors', async () => {
  const body = {};
  Object.defineProperty(body, 'password', { enumerable: true, get() { assert.fail('parsed accessor executed'); } });
  const raw = Buffer.from(' { "question" : "竹" }\r\n');
  seen = [];
  await consume(await invoke(body, { requestFields: { rawBody: raw } }), 422);
  assert.deepEqual(seen[0].body, raw);
});

test('runtime arrayBuffer bytes take precedence over JSON reconstruction and are read once', async () => {
  const raw = Buffer.from(' { "question" : "蓝鹤" } ');
  let reads = 0;
  const body = {};
  Object.defineProperty(body, 'password', { enumerable: true, get() { assert.fail('parsed accessor executed'); } });
  seen = [];
  await consume(await invoke(body, { requestFields: { async arrayBuffer() {
    reads += 1;
    return raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
  } } }), 422);
  assert.equal(reads, 1);
  assert.deepEqual(seen[0].body, raw);
});

test('Blob JSON is read as original bytes rather than serialized as an object', async () => {
  const raw = Buffer.from(' {"question":"竹"}\r\n');
  seen = [];
  await consume(await invoke(new Blob([raw])), 422);
  assert.deepEqual(seen[0].body, raw);
});

test('runtime raw bytes take precedence over a decoded string body too', async () => {
  const raw = Buffer.from(' { "question" : "竹" }\r\n');
  seen = [];
  await consume(await invoke('{"question":"竹"}', { requestFields: { async arrayBuffer() {
    return raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength);
  } } }), 422);
  assert.deepEqual(seen[0].body, raw);
});

test('Blob multipart preserves original boundary and arbitrary binary bytes', async () => {
  const raw = Buffer.concat([Buffer.from('--blob-test\r\n'), Buffer.from([0, 255, 128]), Buffer.from('\r\n--blob-test--\r\n')]);
  seen = [];
  await consume(await invoke(new Blob([raw]), { path: '/api/jobs/upload',
    headers: { 'content-type': 'multipart/form-data; boundary=blob-test' } }), 200);
  assert.deepEqual(seen[0].body, raw);
  assert.equal(seen[0].type, 'multipart/form-data; boundary=blob-test');
});

test('multipart parsed object never uses JSON reconstruction', async () => {
  await consume(await invoke({}, { path: '/api/jobs/upload',
    headers: { 'content-type': 'multipart/form-data; boundary=example' }, fetchImpl: mustNotFetch }), 400);
});

test('wrong Content-Type and foreign Origin reject before reading a runtime raw interface', async () => {
  let reads = 0;
  const requestFields = { arrayBuffer() { reads += 1; assert.fail('must not read'); } };
  await consume(await invoke({}, { headers: { 'content-type': 'text/plain' }, requestFields, fetchImpl: mustNotFetch }), 415);
  await consume(await invoke({}, { headers: { origin: 'https://wrong.example' }, requestFields, fetchImpl: mustNotFetch }), 403);
  assert.equal(reads, 0);
});

test('parsed JSON output is limited by actual UTF-8 and escaped JSON byte length', async () => {
  for (const value of [{ text: '竹'.repeat(90_000) }, { text: '\u0000'.repeat(50_000) }]) {
    await consume(await invoke(value, { fetchImpl: mustNotFetch }), 413);
  }
});

test('accessors, hooks, cycles and non-JSON values are rejected without executing user code', async () => {
  let hooks = 0;
  const accessor = {};
  Object.defineProperty(accessor, 'password', { enumerable: true, get() { hooks += 1; return 'private'; } });
  const hook = { toJSON() { hooks += 1; return {}; } };
  const cycle = {}; cycle.self = cycle;
  for (const value of [accessor, hook, cycle, { value: NaN }, { value: Infinity }, { value: 1n }, { value: () => null }, new Date()]) {
    await consume(await invoke(value, { fetchImpl: mustNotFetch }), 400);
  }
  assert.equal(hooks, 0);
});

test('failed raw read is never retried as serialized JSON or as a second POST', async () => {
  let reads = 0;
  const response = await invoke({}, { requestFields: { async arrayBuffer() {
    reads += 1;
    throw new TypeError('synthetic-private-body-error');
  } }, fetchImpl: mustNotFetch });
  assert.equal(response.status, 502);
  assert.equal(reads, 1);
  assert(!(await response.text()).includes('synthetic-private-body-error'));
});

test('runtime raw reader output still obeys the byte limit before forwarding', async () => {
  let reads = 0;
  await consume(await invoke({}, { maxRequestBytes: 8, requestFields: { async arrayBuffer() {
    reads += 1; return new Uint8Array(9).buffer;
  } }, fetchImpl: mustNotFetch }), 413);
  assert.equal(reads, 1);
});

test('raw interface wait respects timeout and client cancellation without POST', async () => {
  const requestFields = { async arrayBuffer() { await sleep(60); return new Uint8Array([123, 125]).buffer; } };
  await consume(await invoke({}, { requestFields, timeoutMs: 10, fetchImpl: mustNotFetch }), 504);
  const client = new AbortController();
  const pending = invoke({}, { requestFields: { ...requestFields, signal: client.signal }, fetchImpl: mustNotFetch });
  setTimeout(() => client.abort(), 10);
  await consume(await pending, 499);
});

test('upstream failure after JSON reconstruction is attempted once and sanitized', async () => {
  let calls = 0;
  const response = await invoke({ question: '竹' }, { fetchImpl: async (_url, init) => {
    calls += 1;
    assert.deepEqual(JSON.parse(init.body.toString()), { question: '竹' });
    throw new TypeError('synthetic-private-fetch-error');
  } });
  assert.equal(response.status, 502);
  assert.equal(calls, 1);
  assert(!(await response.text()).includes('synthetic-private-fetch-error'));
});

test('unknown body diagnosis contains only fixed phase and allowlisted body kind', async t => {
  const logs = [];
  t.mock.method(console, 'warn', line => logs.push(JSON.parse(line)));
  const secret = 'synthetic-private-body-value';
  class PrivateRuntimeBody { constructor() { this.password = secret; } }
  await consume(await invoke(new PrivateRuntimeBody(), { fetchImpl: mustNotFetch }), 400);
  assert.deepEqual(logs, [{ event: 'pc_proxy_body_type_v1', stage: 'read_body', body_kind: 'unsupported' }]);
  assert(!JSON.stringify(logs).includes(secret));
  assert(!JSON.stringify(logs).includes('PrivateRuntimeBody'));
  assert(!JSON.stringify(logs).includes(backend));
  assert(!JSON.stringify(logs).includes(env.PROJECT_COPILOT_PROXY_SECRET));
});
