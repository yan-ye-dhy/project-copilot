/*
 * Fixed-site browser policy and fixed-destination EdgeOne proxy.
 * Public use also requires the backend's separate proxy-secret, session,
 * ownership, CSRF and quota enforcement; this module is not that enforcement.
 */
import { Blob, Buffer } from 'node:buffer';

const DEFAULT_TIMEOUT_MS = 115_000;
// Reconnect a read-only event tail well below Makers' 120s invocation limit.
const DEFAULT_EVENT_TIMEOUT_MS = 25_000;
const DEFAULT_REQUEST_LIMIT = 4 * 1024 * 1024 + 64 * 1024;
const DEFAULT_RESPONSE_LIMIT = 5_900_000;
const JSON_REQUEST_LIMIT = 256 * 1024;
const HEADER_LIMIT = 16 * 1024;
const UUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const ROUTES = [
  ['GET', /^\/health$/],
  ['GET', /^\/ui\/config$/],
  ['GET', /^\/auth\/me$/],
  ['POST', /^\/auth\/(?:register|login|logout)$/],
  ['GET', /^\/tickets$/],
  ['GET', new RegExp(`^/tickets/${UUID}$`)],
  ['POST', /^\/agent\/contexts$/],
  ['GET', /^\/documents$/],
  ['DELETE', new RegExp(`^/documents/${UUID}$`)],
  ['POST', /^\/actions\/preview$/],
  ['GET', new RegExp(`^/actions/${UUID}$`)],
  ['POST', new RegExp(`^/actions/${UUID}/(?:confirm|reject)$`)],
  ['POST', /^\/jobs$/],
  ['POST', /^\/jobs\/upload$/],
  ['GET', new RegExp(`^/jobs/${UUID}$`)],
  ['GET', new RegExp(`^/jobs/${UUID}/events$`)],
];
const REQUEST_HEADERS = [
  'accept', 'content-type', 'cookie', 'origin', 'x-csrf-token',
  'x-copilot-context-id', 'x-copilot-context-token', 'idempotency-key',
];
const RESPONSE_HEADERS = [
  'content-type', 'x-run-id', 'x-trace-id', 'x-trace-exported',
  'retry-after', 'allow',
];

class ProxyFailure extends Error {
  constructor(status) { super('Proxy request failed'); this.status = status; }
}

function errorResponse(status, extra = {}) {
  const messages = {
    400: '请求格式无效。', 403: '请求来源不允许。', 415: '请求类型不支持。', 431: '请求头过大。', 404: '请求地址不存在。', 413: '请求内容过大。',
    499: '请求已取消。', 502: '上游服务暂时不可用。',
    503: '服务暂时不可用。', 504: '服务响应超时，请查询操作状态。',
  };
  return Response.json({ detail: messages[status] || messages[502] }, {
    status, headers: { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...extra },
  });
}

function backendDestination(raw) {
  if (typeof raw !== 'string' || raw !== raw.trim()) return null;
  let url;
  try { url = new URL(raw); } catch { return null; }
  const host = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com$/;
  if (url.protocol !== 'https:' || !host.test(url.hostname) || url.port ||
      url.username || url.password || url.pathname !== '/' || url.search || url.hash ||
      (raw !== url.origin && raw !== `${url.origin}/`)) return null;
  return url.origin;
}

function validConfiguration(env) {
  const secret = env?.PROJECT_COPILOT_PROXY_SECRET;
  const site = env?.PROJECT_COPILOT_SITE_ORIGIN;
  if (typeof secret !== 'string' || !/^[A-Za-z0-9_-]{32,128}$/.test(secret)) return null;
  let siteUrl;
  try { siteUrl = new URL(site); } catch { return null; }
  if (typeof site !== 'string' || siteUrl.protocol !== 'https:' || site !== siteUrl.origin ||
      siteUrl.username || siteUrl.password || siteUrl.port || siteUrl.hostname === 'localhost' ||
      !/^[a-z0-9.-]+$/.test(siteUrl.hostname) || !siteUrl.hostname.includes('.')) return null;
  // No destination is a supported CLOSED bootstrap configuration. Early
  // request checks remain testable without ever starting a public Tunnel.
  return { origin: backendDestination(env?.PROJECT_COPILOT_BACKEND_ORIGIN), secret, siteOrigin: siteUrl.origin };
}

function resolveRoute(request) {
  // Node's URL may describe platform-internal transport. Validate its syntax,
  // not its hostname identity. Check raw path BEFORE WHATWG normalization can
  // hide dot segments, encoded paths, backslashes or whitespace.
  const raw = request.url;
  if (typeof raw !== 'string' || raw.length > 8192 || /[\s\\\u0000-\u001f\u007f-\u009f]/u.test(raw)) return null;
  const parts = /^(https?):\/\/([^/?#]+)(\/[^?#]*)?(?:\?([^#]*))?$/i.exec(raw);
  if (!parts || parts[2].includes('@')) return null;
  let url;
  try { url = new URL(raw); } catch { return null; }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) return null;
  const path = parts[3] || '/';
  if (path !== url.pathname || !path.startsWith('/api/') ||
      path.includes('//') || !/^\/[a-z0-9_/-]+$/.test(path)) return null;
  const target = path.slice(4);
  if (!ROUTES.some(([method, pattern]) => method === request.method && pattern.test(target))) return null;
  // Validate ALL input, including preview parameters that will be removed.
  // Keep business bytes/order/duplicates intact, avoiding URLSearchParams'
  // re-serialization of encodings. Platform preview tokens are not API auth.
  const query = parts[4];
  if ((query !== undefined && query.length + 1 > 4096) || url.search.length > 4096) return null;
  const fields = query ? query.split('&') : [];
  if (fields.length > 64) return null;
  const forwarded = [];
  try {
    for (const field of fields) {
      if (!field || /%(?![0-9a-f]{2})/i.test(field)) return null;
      const separator = field.indexOf('=');
      const key = separator < 0 ? field : field.slice(0, separator);
      const value = separator < 0 ? '' : field.slice(separator + 1);
      const name = decodeURIComponent(key.replace(/\+/g, ' '));
      const decoded = decodeURIComponent(value.replace(/\+/g, ' '));
      encodeURIComponent(name); encodeURIComponent(decoded); // Reject lone UTF-16 surrogates too.
      if (!name || /[\u0000-\u001f\u007f-\u009f]/u.test(name + decoded)) return null;
      if (name !== 'eo_token' && name !== 'eo_time') forwarded.push(field);
    }
  } catch { return null; } // Invalid UTF-8 must not be silently repaired.
  const eventStream = new RegExp(`^/jobs/${UUID}/events$`).test(target);
  if (eventStream && (forwarded.length > 1 || forwarded.some(field =>
    !/^after=(?:0|[1-9][0-9]{0,9})$/.test(field) || Number(field.slice(6)) > 2147483647))) return null;
  return { target, search: forwarded.length ? `?${forwarded.join('&')}` : '', eventStream };
}

// Makers routes this project's /api/* to its own handler. Its Node URL can be
// an internal HTTP envelope; hostname is neither browser Origin nor authority.
// Do not reconstruct browser identity from Host/XFH. Fixed browser Origin and
// Fetch Metadata below provide browser CSRF defence, NEVER user authentication.
function requestPolicy(request, route, config, requestLimit) {
  let headerBytes = 0;
  const encoder = new TextEncoder();
  for (const [name, value] of request.headers) {
    headerBytes += encoder.encode(name).byteLength + encoder.encode(value).byteLength + 4;
    if (/[\x00-\x08\x0b-\x1f\x7f]/.test(value)) throw new ProxyFailure(400);
  }
  if (headerBytes > HEADER_LIMIT) throw new ProxyFailure(431);
  const limits = {
    cookie: 8192, origin: 512, 'content-type': 512, accept: 1024,
    'x-csrf-token': 512, 'x-copilot-context-id': 128,
    'x-copilot-context-token': 512, 'idempotency-key': 256,
  };
  for (const [name, limit] of Object.entries(limits)) {
    if ((request.headers.get(name) || '').length > limit) throw new ProxyFailure(431);
  }
  for (const name of ['transfer-encoding', 'upgrade', 'proxy-authorization', 'x-http-method-override']) {
    if (request.headers.has(name)) throw new ProxyFailure(400);
  }
  const encoding = request.headers.get('content-encoding');
  if (encoding && encoding.toLowerCase() !== 'identity') throw new ProxyFailure(415);
  const expect = request.headers.get('expect');
  if (expect) throw new ProxyFailure(400);
  const origin = request.headers.get('origin');
  if ((origin !== null && origin !== config.siteOrigin) ||
      (request.method !== 'GET' && origin !== config.siteOrigin)) throw new ProxyFailure(403);
  const fetchSite = request.headers.get('sec-fetch-site');
  if (fetchSite !== null && !['same-origin', 'none'].includes(fetchSite)) throw new ProxyFailure(403);
  const eventId = request.headers.get('last-event-id');
  if (route.eventStream && eventId !== null &&
      (!/^(?:0|[1-9][0-9]{0,9})$/.test(eventId) || Number(eventId) > 2147483647)) throw new ProxyFailure(400);

  const limit = route.target === '/jobs/upload' ? requestLimit : Math.min(requestLimit, JSON_REQUEST_LIMIT);
  // Declared oversize/malformed bodies are rejected before getting a reader.
  boundedLength(request.headers, limit, 400, 413);
  if (request.method === 'GET' && request.body) throw new ProxyFailure(400);
  if (request.body || Number(request.headers.get('content-length')) > 0) bodyContentType(request.headers, route.target);
  return limit;
}

function bodyContentType(headers, target) {
  const type = headers.get('content-type') || '';
  if (target === '/jobs/upload') {
    if (!/^multipart\/form-data;\s*boundary=(?:[A-Za-z0-9'()+_,.\/:=?-]{1,70}|"[A-Za-z0-9'()+_,.\/:=?-]{1,70}")$/i.test(type)) throw new ProxyFailure(415);
  } else if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(type)) throw new ProxyFailure(415);
}

function connectionTokens(headers) {
  return new Set((headers.get('connection') || '').split(',').map((value) => value.trim().toLowerCase()).filter(Boolean));
}

function withoutPreviewCookies(value) {
  const fields = value.split(';');
  const retained = fields.filter(field => {
    const name = field.split('=', 1)[0].trim();
    return name !== 'eo_token' && name !== 'eo_time';
  });
  // Preserve untouched headers exactly. Never deduplicate session cookies:
  // ambiguity must still reach the backend's duplicate-cookie rejection.
  return retained.length === fields.length ? value : retained.map(field => field.trim()).filter(Boolean).join('; ');
}

function forwardedRequestHeaders(request, secret, route) {
  const result = new Headers();
  const excluded = connectionTokens(request.headers);
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null && !excluded.has(name)) {
      const forwarded = name === 'cookie' ? withoutPreviewCookies(value) : value;
      if (name !== 'cookie' || forwarded) result.set(name, forwarded);
    }
  }
  result.set('Accept-Encoding', 'identity');
  result.set('Cache-Control', 'no-store');
  if (route.eventStream && request.headers.has('last-event-id') && !excluded.has('last-event-id')) {
    result.set('Last-Event-ID', request.headers.get('last-event-id'));
  }
  // Never use the untrusted client's value of this header.
  result.set('X-Project-Copilot-Proxy-Secret', secret);
  return result;
}

function responseHeaders(upstream) {
  const headers = new Headers({ 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
  const excluded = connectionTokens(upstream.headers);
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value !== null && !excluded.has(name)) headers.set(name, value);
  }
  if (!excluded.has('set-cookie')) {
    // Set-Cookie cannot be split on commas: Expires itself contains commas.
    if (typeof upstream.headers.getSetCookie !== 'function') {
      if (upstream.headers.has('set-cookie')) throw new ProxyFailure(502);
    } else {
      for (const cookie of upstream.headers.getSetCookie()) {
        // The session must be host-only for the public site, not the tunnel.
        validateCookie(cookie);
        headers.append('Set-Cookie', cookie);
      }
    }
  }
  return headers;
}

function validateCookie(cookie) {
  if (/(?:^|;)\s*domain\s*=/i.test(cookie)) throw new ProxyFailure(502);
  const [pair, ...rawAttributes] = cookie.split(';');
  const name = pair.slice(0, pair.indexOf('=')).trim();
  if (name !== '__Host-pc_session') return; // Auxiliary cookies may be JS-readable.
  const attributes = new Map();
  const required = new Set(['secure', 'httponly', 'path', 'samesite', 'domain']);
  for (const raw of rawAttributes) {
    const separator = raw.indexOf('=');
    const key = (separator < 0 ? raw : raw.slice(0, separator)).trim().toLowerCase();
    if (!required.has(key)) continue;
    if (attributes.has(key)) throw new ProxyFailure(502);
    attributes.set(key, separator < 0 ? null : raw.slice(separator + 1).trim());
  }
  if (attributes.get('secure') !== null || attributes.get('httponly') !== null ||
      attributes.get('path') !== '/' || attributes.get('samesite')?.toLowerCase() !== 'lax' ||
      attributes.has('domain')) throw new ProxyFailure(502);
}

function boundedLength(headers, limit, invalidStatus, oversizedStatus) {
  const raw = headers.get('content-length');
  if (raw === null) return;
  if (!/^(?:0|[1-9][0-9]*)$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new ProxyFailure(invalidStatus);
  }
  if (Number(raw) > limit) throw new ProxyFailure(oversizedStatus);
}

// Promise.race alone leaves listeners on a long-lived stream; remove each one.
function withAbort(promise, signal) {
  if (signal.aborted) {
    // read()/fetch() may already have returned a rejecting promise before this
    // check; observe it even when cancellation won the race.
    Promise.resolve(promise).catch(() => {});
    return Promise.reject(new Error('aborted'));
  }
  return new Promise((resolve, reject) => {
    const stop = () => { signal.removeEventListener('abort', stop); reject(new Error('aborted')); };
    signal.addEventListener('abort', stop, { once: true });
    Promise.resolve(promise).then(
      (value) => { signal.removeEventListener('abort', stop); resolve(value); },
      (error) => { signal.removeEventListener('abort', stop); reject(error); },
    );
  });
}

function stopReader(reader) {
  try { Promise.resolve(reader.cancel()).catch(() => {}); } catch { /* already closed */ }
}

function bodyKind(value) {
  if (value === null || value === undefined) return 'none';
  if (Buffer.isBuffer(value)) return 'buffer';
  if (ArrayBuffer.isView(value)) return 'byte_view';
  if (value instanceof ArrayBuffer) return 'array_buffer';
  if (typeof value === 'string') return 'string';
  if (value instanceof Blob) return 'blob';
  if (Array.isArray(value)) return 'json_array';
  if (typeof value === 'object') {
    // Inspect plain envelopes without invoking a JSON property's getter.
    const proto = Object.getPrototypeOf(value);
    if (proto === null || proto === Object.prototype) {
      if (typeof Object.getOwnPropertyDescriptor(value, 'getReader')?.value === 'function') return 'web_stream';
      if (typeof Object.getOwnPropertyDescriptor(value, Symbol.asyncIterator)?.value === 'function') return 'node_stream';
      return 'plain_object';
    }
    if (typeof value.getReader === 'function') return 'web_stream';
    if (typeof value[Symbol.asyncIterator] === 'function') return 'node_stream';
  }
  return 'unsupported';
}

function parsedJsonBytes(value, limit) {
  // Validate a bounded JSON data tree before stringify can invoke hooks/getters.
  const stack = [[value, 0]], seen = new WeakSet();
  let size = 0;
  const charge = bytes => { size += bytes; if (size > limit) throw new ProxyFailure(413); };
  while (stack.length) {
    const [item, depth] = stack.pop();
    if (depth > 64) throw new ProxyFailure(400);
    if (item === null || typeof item === 'string' || typeof item === 'boolean' ||
        (typeof item === 'number' && Number.isFinite(item))) {
      if (typeof item === 'string' && Buffer.byteLength(item, 'utf8') > limit) throw new ProxyFailure(413);
      charge(Buffer.byteLength(JSON.stringify(item), 'utf8'));
      continue;
    }
    if (typeof item !== 'object' || seen.has(item) ||
        (!Array.isArray(item) && Object.getPrototypeOf(item) !== Object.prototype && Object.getPrototypeOf(item) !== null)) throw new ProxyFailure(400);
    if (Array.isArray(item) && Object.getPrototypeOf(item) !== Array.prototype && Object.getPrototypeOf(item) !== null) throw new ProxyFailure(400);
    seen.add(item);
    const keys = Object.keys(item), array = Array.isArray(item);
    charge(2 + Math.max(0, keys.length - 1));
    if (Object.getOwnPropertySymbols(item).length ||
        Object.getOwnPropertyNames(item).length !== keys.length + (array ? 1 : 0) ||
        (array && keys.length !== item.length)) throw new ProxyFailure(400);
    for (const key of keys) {
      const descriptor = Object.getOwnPropertyDescriptor(item, key);
      if (!descriptor || !('value' in descriptor)) throw new ProxyFailure(400);
      if (!array) {
        if (Buffer.byteLength(key, 'utf8') > limit) throw new ProxyFailure(413);
        charge(Buffer.byteLength(JSON.stringify(key), 'utf8') + 1);
      }
      stack.push([descriptor.value, depth + 1]);
    }
  }
  const json = JSON.stringify(value);
  if (Buffer.byteLength(json, 'utf8') > limit) throw new ProxyFailure(413);
  return Buffer.from(json, 'utf8');
}

async function readRequestBody(request, limit, signal) {
  boundedLength(request.headers, limit, 400, 413);
  if (signal.aborted) throw new Error('aborted');
  let source = request.rawBody ?? request.body;
  let kind = bodyKind(source);
  // Prefer original bytes to parsed JSON. An opaque arrayBuffer() materializes
  // once, then is checked; stream paths below retain incremental enforcement.
  if (['plain_object', 'json_array', 'string', 'unsupported', 'none'].includes(kind) &&
      request.bodyUsed !== true && typeof request.arrayBuffer === 'function') {
    source = await withAbort(request.arrayBuffer(), signal);
    if (!(source instanceof ArrayBuffer) && !ArrayBuffer.isView(source)) throw new ProxyFailure(400);
    kind = bodyKind(source);
  }
  if (kind === 'none') return undefined;
  if (kind === 'blob') {
    if (source.size > limit) throw new ProxyFailure(413);
    source = source.stream();
    kind = 'web_stream';
  }
  if (kind === 'plain_object' || kind === 'json_array') {
    if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(request.headers.get('content-type') || '')) {
      console.warn(JSON.stringify({ event: 'pc_proxy_body_type_v1', stage: 'read_body', body_kind: kind }));
      throw new ProxyFailure(400); // Never serialize a multipart body.
    }
    return parsedJsonBytes(source, limit);
  }
  if (kind === 'unsupported') {
    console.warn(JSON.stringify({ event: 'pc_proxy_body_type_v1', stage: 'read_body', body_kind: kind }));
    throw new ProxyFailure(400);
  }
  const chunks = [];
  let total = 0;
  const append = (value) => {
    let bytes;
    if (typeof value === 'string') {
      // Check UTF-8 size before allocating; never parse/re-serialize JSON.
      if (Buffer.byteLength(value, 'utf8') > limit - total) throw new ProxyFailure(413);
      bytes = Buffer.from(value, 'utf8');
    } else if (ArrayBuffer.isView(value)) {
      bytes = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    } else if (value instanceof ArrayBuffer) {
      bytes = Buffer.from(value);
    } else {
      throw new ProxyFailure(400); // A parsed object cannot recover original bytes.
    }
    total += bytes.byteLength;
    if (total > limit) throw new ProxyFailure(413);
    chunks.push(bytes);
  };
  // Web Request uses getReader(); Node-backed Request adapters may expose a
  // Readable/async iterator instead. Choose once: never reread a failed body.
  let reader;
  if (kind === 'web_stream') {
    reader = source.getReader();
  } else if (kind === 'node_stream') {
    const iterator = source[Symbol.asyncIterator]();
    reader = { read: () => iterator.next(), cancel() {
      try { source.destroy?.(); } finally { return iterator.return?.(); }
    } };
  } else {
    append(source);
    return Buffer.concat(chunks, total);
  }
  try {
    while (true) {
      const part = await withAbort(reader.read(), signal);
      if (part.done) break;
      append(part.value);
    }
    // Buffer is a byte-preserving BodyInit in Node fetch implementations.
    return Buffer.concat(chunks, total);
  } catch (error) {
    stopReader(reader);
    throw error;
  }
}

function reportProxyStage(stage, error) {
  // Temporary runtime diagnosis: no messages, stacks, URLs, headers or bodies.
  let name;
  try { name = error?.name; } catch { /* Unknown thrown values stay generic. */ }
  const errorType = error instanceof ProxyFailure ? 'ProxyFailure' :
    ['Error', 'TypeError', 'RangeError', 'AbortError'].includes(name) ? name : 'Error';
  console.warn(JSON.stringify({ event: 'pc_proxy_body_v1', stage, error_type: errorType }));
}

function forwardBody(body, signal, limit, cleanup, abort) {
  const reader = body.getReader();
  let total = 0;
  return new ReadableStream({
    async pull(controller) {
      try {
        const part = await withAbort(reader.read(), signal);
        if (part.done) { cleanup(); controller.close(); return; }
        if (!(part.value instanceof Uint8Array)) throw new Error('invalid chunk');
        total += part.value.byteLength;
        if (total > limit) throw new Error('response too large');
        controller.enqueue(part.value);
      } catch {
        stopReader(reader);
        abort(); cleanup();
        // Headers may already be sent; end the body instead of inventing a new status.
        controller.error(new Error('Upstream response interrupted'));
      }
    },
    cancel() { stopReader(reader); abort(); cleanup(); },
  });
}

/** Injection is exclusively for local tests; the production entry uses defaults. */
export function createProxyHandler({
  fetchImpl = globalThis.fetch,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  eventTimeoutMs = DEFAULT_EVENT_TIMEOUT_MS,
  maxRequestBytes = DEFAULT_REQUEST_LIMIT,
  maxResponseBytes = DEFAULT_RESPONSE_LIMIT,
} = {}) {
  if (typeof fetchImpl !== 'function' || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 ||
      !Number.isSafeInteger(eventTimeoutMs) || eventTimeoutMs < 1 || eventTimeoutMs > DEFAULT_EVENT_TIMEOUT_MS ||
      !Number.isSafeInteger(maxRequestBytes) || maxRequestBytes < 1 ||
      !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1) throw new TypeError('Invalid proxy limits');
  return async function proxy({ request, env }) {
    const config = validConfiguration(env);
    if (!config) return errorResponse(503);
    const route = resolveRoute(request);
    if (!route) return errorResponse(404);
    let requestLimit;
    try { requestLimit = requestPolicy(request, route, config, maxRequestBytes); }
    catch (error) { return errorResponse(error instanceof ProxyFailure ? error.status : 400); }
    // Never read a valid request body or send credentials without a reviewed
    // destination. Do not configure a fabricated or third-party Tunnel URL.
    if (!config.origin) return errorResponse(503);

    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); },
      route.eventStream ? Math.min(timeoutMs, eventTimeoutMs) : timeoutMs);
    timer.unref?.();
    // Makers may omit the client signal; our own deadline remains mandatory.
    const clientSignal = request.signal;
    const onAbort = () => controller.abort();
    if (clientSignal?.aborted) onAbort();
    else clientSignal?.addEventListener('abort', onAbort, { once: true });
    const cleanup = () => { clearTimeout(timer); clientSignal?.removeEventListener('abort', onAbort); };
    let upstream;
    let stage = 'read_body';
    try {
      let body = await readRequestBody(request, requestLimit, controller.signal);
      if (controller.signal.aborted) throw new Error('aborted');
      if (body?.byteLength) {
        if (request.method === 'GET') throw new ProxyFailure(400);
        bodyContentType(request.headers, route.target);
      }
      if (request.method === 'GET') body = undefined; // Empty arrayBuffer() is not a GET BodyInit.
      stage = 'fetch_upstream';
      upstream = await withAbort(fetchImpl(`${config.origin}${route.target}${route.search}`, {
        method: request.method, headers: forwardedRequestHeaders(request, config.secret, route),
        body, redirect: 'manual', signal: controller.signal,
      }), controller.signal);
      stage = 'response';
      if (upstream.status < 200 || (upstream.status >= 300 && upstream.status < 400)) throw new ProxyFailure(502);
      const headers = responseHeaders(upstream);
      if (route.eventStream && upstream.status === 200) {
        if (!/^text\/event-stream(?:;\s*charset=utf-8)?$/i.test(headers.get('content-type') || '')) throw new ProxyFailure(502);
        headers.set('Cache-Control', 'no-store, no-transform');
        headers.set('X-Accel-Buffering', 'no');
      }
      // fetch may transparently decompress while retaining the original headers.
      // Reject unexpected encoding rather than forwarding contradictory metadata.
      const encoding = upstream.headers.get('content-encoding');
      if (encoding && encoding.toLowerCase() !== 'identity') throw new ProxyFailure(502);
      boundedLength(upstream.headers, maxResponseBytes, 502, 502);
      if (upstream.status >= 500) {
        if (upstream.body) stopReader(upstream.body.getReader());
        cleanup();
        return errorResponse(upstream.status, Object.fromEntries(
          ['x-run-id', 'x-trace-id', 'x-trace-exported'].filter((key) => headers.has(key)).map((key) => [key, headers.get(key)]),
        ));
      }
      if (!upstream.body || upstream.status === 204 || upstream.status === 205) {
        cleanup();
        return new Response(null, { status: upstream.status, headers });
      }
      return new Response(forwardBody(upstream.body, controller.signal, maxResponseBytes, cleanup,
        () => controller.abort()), { status: upstream.status, headers });
    } catch (error) {
      if (!timedOut && !clientSignal?.aborted && (!(error instanceof ProxyFailure) || error.status === 502)) reportProxyStage(stage, error);
      if (upstream?.body && !upstream.body.locked) stopReader(upstream.body.getReader());
      const status = timedOut ? 504 : clientSignal?.aborted ? 499 : error instanceof ProxyFailure ? error.status : 502;
      controller.abort(); cleanup();
      return errorResponse(status);
    }
  };
}
