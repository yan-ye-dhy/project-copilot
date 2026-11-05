/*
 * Same-origin, fixed-destination EdgeOne proxy.
 * Public use also requires the backend's separate proxy-secret, session,
 * ownership, CSRF and quota enforcement; this module is not that enforcement.
 */

const DEFAULT_TIMEOUT_MS = 115_000;
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
  constructor(status, reason = null) { super('Proxy request failed'); this.status = status; this.reason = reason; }
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
  let url;
  try { url = new URL(request.url); } catch { return null; }
  const path = url.pathname;
  if (!path.startsWith('/api/') || path.includes('%') || path.includes('\\') ||
      path.includes('//') || !/^\/[a-z0-9_/-]+$/.test(path) || url.search.length > 4096) return null;
  const target = path.slice(4);
  if (!ROUTES.some(([method, pattern]) => method === request.method && pattern.test(target))) return null;
  return { target, search: url.search };
}

// This runs after the function was invoked, but before reading its request body
// or opening the Tunnel. Origin is CSRF defence, never an API identity.
function requestPolicy(request, route, config, requestLimit) {
  if (new URL(request.url).origin !== config.siteOrigin) throw new ProxyFailure(403, 'url_origin_mismatch');
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
      (request.method !== 'GET' && origin !== config.siteOrigin)) throw new ProxyFailure(403, 'origin_header_mismatch');
  const fetchSite = request.headers.get('sec-fetch-site');
  if (fetchSite && !['same-origin', 'none'].includes(fetchSite)) throw new ProxyFailure(403, 'fetch_site_disallowed');

  const limit = route.target === '/jobs/upload' ? requestLimit : Math.min(requestLimit, JSON_REQUEST_LIMIT);
  // Declared oversize/malformed bodies are rejected before getting a reader.
  boundedLength(request.headers, limit, 400, 413);
  if (request.method === 'GET' && request.body) throw new ProxyFailure(400);
  if (request.body) {
    const type = request.headers.get('content-type') || '';
    if (route.target === '/jobs/upload') {
      if (!/^multipart\/form-data;\s*boundary=(?:[A-Za-z0-9'()+_,.\/:=?-]{1,70}|"[A-Za-z0-9'()+_,.\/:=?-]{1,70}")$/i.test(type)) {
        throw new ProxyFailure(415);
      }
    } else if (!/^application\/json(?:;\s*charset=utf-8)?$/i.test(type)) throw new ProxyFailure(415);
  }
  return limit;
}

// TEMPORARY bootstrap diagnostics. Never log full URLs, headers, error objects
// or bodies. URL.origin omits paths/query/fragment; credentials and excessively
// long or unexpected origins are suppressed as well. These values are only
// observations, never new authority for accepting Host/X-Forwarded-Host.
function diagnosticOrigin(value, secret) {
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password ||
        url.origin.length > 256 || !/^[\x21-\x7e]+$/.test(url.origin) ||
        url.origin.toLowerCase().includes(secret.toLowerCase())) return '[redacted]';
    return url.origin;
  } catch { return '[redacted]'; }
}

function originDiagnostic(request, route, config, outcome) {
  const urlOrigin = new URL(request.url).origin;
  const origin = request.headers.get('origin');
  const fetchSite = request.headers.get('sec-fetch-site');
  const host = request.headers.get('host');
  return {
    event: 'pc_proxy_origin_v1',
    route: route.target === '/health' ? 'health' : 'auth_me',
    outcome,
    request_url_origin: diagnosticOrigin(request.url, config.secret),
    expected_site_origin: diagnosticOrigin(config.siteOrigin, config.secret),
    url_origin_matches: urlOrigin === config.siteOrigin,
    origin_header_present: origin !== null,
    origin_header_matches: origin === null ? null : origin === config.siteOrigin,
    fetch_site_present: fetchSite !== null,
    fetch_site_allowed: !fetchSite || ['same-origin', 'none'].includes(fetchSite),
    host_header_present: host !== null,
    host_header_matches_site: host === null ? null : host === new URL(config.siteOrigin).host,
    backend_destination_configured: false,
  };
}

function connectionTokens(headers) {
  return new Set((headers.get('connection') || '').split(',').map((value) => value.trim().toLowerCase()).filter(Boolean));
}

function forwardedRequestHeaders(request, secret) {
  const result = new Headers();
  const excluded = connectionTokens(request.headers);
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value !== null && !excluded.has(name)) result.set(name, value);
  }
  result.set('Accept-Encoding', 'identity');
  result.set('Cache-Control', 'no-store');
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
        if (/(?:^|;)\s*domain\s*=/i.test(cookie)) throw new ProxyFailure(502);
        headers.append('Set-Cookie', cookie);
      }
    }
  }
  return headers;
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
  if (signal.aborted) return Promise.reject(new Error('aborted'));
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

async function readRequestBody(request, limit, signal) {
  boundedLength(request.headers, limit, 400, 413);
  if (!request.body) return undefined;
  const reader = request.body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const part = await withAbort(reader.read(), signal);
      if (part.done) break;
      if (!(part.value instanceof Uint8Array)) throw new ProxyFailure(400);
      total += part.value.byteLength;
      if (total > limit) throw new ProxyFailure(413);
      chunks.push(part.value);
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
    return body;
  } catch (error) {
    stopReader(reader);
    throw error;
  }
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
  maxRequestBytes = DEFAULT_REQUEST_LIMIT,
  maxResponseBytes = DEFAULT_RESPONSE_LIMIT,
  originDiagnosticLog = (event) => console.info(JSON.stringify(event)),
} = {}) {
  if (typeof fetchImpl !== 'function' || !Number.isSafeInteger(timeoutMs) || timeoutMs < 1 ||
      !Number.isSafeInteger(maxRequestBytes) || maxRequestBytes < 1 ||
      !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1 ||
      typeof originDiagnosticLog !== 'function') throw new TypeError('Invalid proxy limits');
  // At most two records per warm handler, not a global/distributed rate limit.
  // No new env flags or diagnostic HTTP endpoints. Remove after cloud evidence.
  let originLogsRemaining = 2;
  const observeOrigin = (request, route, config, outcome) => {
    if (config.origin || request.method !== 'GET' ||
        !['/health', '/auth/me'].includes(route.target) || originLogsRemaining === 0) return;
    originLogsRemaining -= 1;
    try { originDiagnosticLog(originDiagnostic(request, route, config, outcome)); }
    catch { /* Diagnostics must never change admission or error responses. */ }
  };
  return async function proxy({ request, env }) {
    const config = validConfiguration(env);
    if (!config) return errorResponse(503);
    const route = resolveRoute(request);
    if (!route) return errorResponse(404);
    let requestLimit;
    try { requestLimit = requestPolicy(request, route, config, maxRequestBytes); }
    catch (error) {
      if (error instanceof ProxyFailure && error.status === 403) {
        observeOrigin(request, route, config, error.reason);
      }
      return errorResponse(error instanceof ProxyFailure ? error.status : 400);
    }
    // Never read a valid request body or send credentials without a reviewed
    // destination. Do not configure a fabricated or third-party Tunnel URL.
    if (!config.origin) {
      observeOrigin(request, route, config, 'preflight_pass_no_backend');
      return errorResponse(503);
    }

    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
    timer.unref?.();
    const onAbort = () => controller.abort();
    if (request.signal.aborted) onAbort();
    else request.signal.addEventListener('abort', onAbort, { once: true });
    const cleanup = () => { clearTimeout(timer); request.signal.removeEventListener('abort', onAbort); };
    let upstream;
    try {
      const body = await readRequestBody(request, requestLimit, controller.signal);
      if (controller.signal.aborted) throw new Error('aborted');
      upstream = await withAbort(fetchImpl(`${config.origin}${route.target}${route.search}`, {
        method: request.method, headers: forwardedRequestHeaders(request, config.secret),
        body, redirect: 'manual', signal: controller.signal,
      }), controller.signal);
      if (upstream.status < 200 || (upstream.status >= 300 && upstream.status < 400)) throw new ProxyFailure(502);
      const headers = responseHeaders(upstream);
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
      if (upstream?.body && !upstream.body.locked) stopReader(upstream.body.getReader());
      const status = timedOut ? 504 : request.signal.aborted ? 499 : error instanceof ProxyFailure ? error.status : 502;
      controller.abort(); cleanup();
      return errorResponse(status);
    }
  };
}
