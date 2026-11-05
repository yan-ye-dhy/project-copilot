/** Synthetic runtime envelopes + real local HTTP bytes. No cloud or real auth. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { Readable } from 'node:stream';
import { createProxyHandler } from '../cloud-functions/proxy.mjs';

const site = 'https://copilot.example';
const backend = 'https://body-hotfix-test.trycloudflare.com';
const env = {
  PROJECT_COPILOT_SITE_ORIGIN: site,
  PROJECT_COPILOT_BACKEND_ORIGIN: backend,
  PROJECT_COPILOT_PROXY_SECRET: 'synthetic-body-hotfix-'.padEnd(48, 'x'),
};
const credentials = '{"username":"synthetic","password":"synthetic-only"}';
let server, base, seen = [];
const sockets = new Set();
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

before(async () => {
  server = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    seen.push({ method: req.method, url: req.url, body, type: req.headers['content-type'] });
    if (req.url === '/health') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{"status":"ok"}');
    } else if (req.url === '/auth/login' && body.equals(Buffer.from(credentials))) {
      res.writeHead(200, { 'content-type': 'application/json', 'set-cookie': [
        '__Host-pc_session=synthetic; Path=/; Secure; HttpOnly; SameSite=Lax',
        'helper=synthetic; Expires=Wed, 21 Oct 2037 07:28:00 GMT; Path=/; Secure; SameSite=Lax',
      ] });
      res.end('{"user":{"username":"synthetic"}}');
    } else if (req.url === '/jobs/upload') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{"status":"received"}');
    } else {
      // A controlled backend validation response, not real FastAPI credentials.
      res.writeHead(422, { 'content-type': 'application/json' });
      res.end('{"detail":"invalid synthetic login"}');
    }
  });
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  for (const socket of sockets) socket.destroy();
  if (server?.listening) await new Promise(resolve => server.close(resolve));
});

function invoke(body, { path = '/api/auth/login', method = 'POST', headers = {}, signal, ...options } = {}) {
  const requestHeaders = new Headers({ 'sec-fetch-site': 'same-origin', ...headers });
  if (method !== 'GET' && !requestHeaders.has('origin')) requestHeaders.set('origin', site);
  if (body !== undefined && body !== null && !requestHeaders.has('content-type')) requestHeaders.set('content-type', 'application/json');
  const request = { url: `http://runtime.internal${path}`, method, headers: requestHeaders, body };
  if (signal) request.signal = signal;
  const handler = createProxyHandler({ timeoutMs: 1500, fetchImpl: async (input, init) => {
    const url = new URL(input);
    assert.equal(url.origin, backend);
    if (init.body !== undefined) assert(Buffer.isBuffer(init.body));
    return fetch(`${base}${url.pathname}${url.search}`, init);
  }, ...options });
  return handler({ request, env });
}

test('runtime GET without body or client signal returns the HTTP backend health JSON', async () => {
  seen = [];
  const response = await invoke(undefined, { method: 'GET', path: '/api/health' });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { status: 'ok' });
  assert.equal(seen.length, 1);
  assert.equal(seen[0].body.length, 0);
});

test('bodyless POST reaches the HTTP backend and preserves validation 422', async () => {
  seen = [];
  const response = await invoke(undefined);
  assert.equal(response.status, 422);
  await response.json();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].body.length, 0);
});

const bodyFactories = {
  node_stream: () => Readable.from([Buffer.from('{'), Buffer.from('}')]),
  web_stream: () => new ReadableStream({ start(c) { c.enqueue(new Uint8Array([123, 125])); c.close(); } }),
  buffer: () => Buffer.from('{}'),
  uint8_subview: () => new Uint8Array([9, 123, 125, 9]).subarray(1, 3),
  array_buffer: () => new Uint8Array([123, 125]).buffer,
  utf8_string: () => '{}',
};
for (const [kind, makeBody] of Object.entries(bodyFactories)) {
  test(`${kind} JSON body reaches the HTTP backend as exact {} bytes and returns 422`, async () => {
    seen = [];
    const response = await invoke(makeBody());
    assert.equal(response.status, 422);
    await response.json();
    assert.equal(seen.length, 1);
    assert.deepEqual(seen[0].body, Buffer.from('{}'));
    assert.equal(seen[0].type, 'application/json');
  });
}

test('UTF-8 string body retains whitespace and bytes instead of parsing JSON', async () => {
  const payload = ' {"问题":"竹", "extra":true}\r\n';
  seen = [];
  const response = await invoke(payload);
  assert.equal(response.status, 422);
  await response.json();
  assert.deepEqual(seen[0].body, Buffer.from(payload));
});

test('synthetic login preserves separate session and comma-containing expiration cookies', async () => {
  seen = [];
  const response = await invoke(Readable.from([Buffer.from(credentials)]));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).user.username, 'synthetic');
  assert.deepEqual(seen[0].body, Buffer.from(credentials));
  const cookies = response.headers.getSetCookie();
  assert.equal(cookies.length, 2);
  assert.match(cookies[0], /^__Host-pc_session=/);
  assert.match(cookies[0], /HttpOnly/);
  assert.match(cookies[1], /Expires=Wed, 21 Oct 2037 07:28:00 GMT/);
});

test('Node multipart stream preserves the boundary and binary file bytes', async () => {
  const payload = Buffer.concat([Buffer.from('--body-test\r\nContent-Disposition: form-data; name="file"; filename="synthetic.txt"\r\n\r\n'),
    Buffer.from([0, 255, 128, 13, 10]), Buffer.from('\r\n--body-test--\r\n')]);
  seen = [];
  const response = await invoke(Readable.from([payload.subarray(0, 70), payload.subarray(70)]), {
    path: '/api/jobs/upload', headers: { 'content-type': 'multipart/form-data; boundary=body-test' },
  });
  assert.equal(response.status, 200);
  await response.json();
  assert.deepEqual(seen[0].body, payload);
  assert.equal(seen[0].type, 'multipart/form-data; boundary=body-test');
});

test('oversized actual or declared bodies return 413 before any upstream request', async () => {
  let calls = 0;
  const options = { maxRequestBytes: 8, fetchImpl: () => { calls += 1; assert.fail('oversize must not fetch'); } };
  for (const body of [Readable.from([Buffer.alloc(5), Buffer.alloc(4)]), Buffer.alloc(9), new Uint8Array(9).buffer, '竹竹竹']) {
    assert.equal((await invoke(body, options)).status, 413);
  }
  const response = await invoke(Readable.from([Buffer.from('{}')]), { ...options, headers: { 'content-length': '9' } });
  assert.equal(response.status, 413);
  assert.equal(calls, 0);
});

test('read failure and parsed objects never reach upstream or replay the input', async () => {
  let calls = 0;
  const fetchImpl = () => { calls += 1; assert.fail('invalid input must not fetch'); };
  const stream = Readable.from((async function* () { yield Buffer.from('{'); throw new TypeError('synthetic-private-error'); })());
  const failed = await invoke(stream, { fetchImpl });
  assert.equal(failed.status, 502);
  assert(!(await failed.text()).includes('synthetic-private-error'));
  assert.equal((await invoke({ already: 'parsed' }, { fetchImpl })).status, 400);
  assert.equal(calls, 0);
});

test('foreign Origin is rejected before body reading or upstream fetch', async () => {
  let reads = 0;
  const body = { getReader() { reads += 1; throw new Error('must not read'); } };
  const response = await invoke(body, { headers: { origin: 'https://wrong.example' },
    fetchImpl: () => assert.fail('foreign source must not fetch') });
  assert.equal(response.status, 403);
  assert.equal(reads, 0);
});

test('internal timeout and client cancellation interrupt Node body reads without fetch or retry', async () => {
  let calls = 0;
  const fetchImpl = () => { calls += 1; assert.fail('incomplete body must not fetch'); };
  const slow = () => Readable.from((async function* () { yield Buffer.from('{'); await sleep(60); yield Buffer.from('}'); })());
  assert.equal((await invoke(slow(), { timeoutMs: 10, fetchImpl })).status, 504);
  const client = new AbortController();
  const pending = invoke(slow(), { signal: client.signal, fetchImpl });
  setTimeout(() => client.abort(), 10);
  assert.equal((await pending).status, 499);
  assert.equal(calls, 0);
});

test('fetch failure after reading JSON is sanitized and attempted only once', async () => {
  let calls = 0;
  const response = await invoke(Readable.from([Buffer.from('{}')]), { fetchImpl: async (_url, init) => {
    calls += 1;
    assert.deepEqual(init.body, Buffer.from('{}'));
    throw new TypeError('synthetic-fetch-private-error');
  } });
  assert.equal(response.status, 502);
  assert.equal(calls, 1);
  assert(!(await response.text()).includes('synthetic-fetch-private-error'));
});
