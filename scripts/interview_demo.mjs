#!/usr/bin/env node
/** Local synthetic interview demo. No upstream client, database or .env loading. */
import { createServer } from 'node:http';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { USER, CSRF, CREATED, EXPIRES, uuid, scenarios, existingTicket, proposalPayload, resultFor } from './interview-demo/fixtures.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const run = promisify(execFile);
const types = { '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.webp': 'image/webp' };
const fail = (status, message) => Object.assign(new Error(message), { status });
async function jsonBody(req) {
  if (!req.headers['content-type']?.startsWith('application/json')) throw fail(415, 'JSON only in this simulation');
  const chunks = []; let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 32768) throw fail(413, 'Simulation request too large');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString()); }
  catch { throw fail(400, 'Invalid JSON'); }
}

export async function startDemo({ port = 4173, durationMs = 1800 } = {}) {
  if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('Invalid port');
  const scratch = await mkdtemp(path.join(tmpdir(), 'pc-interview-demo-'));
  const files = new Map();
  let server;
  try {
    const dist = path.join(scratch, 'static');
    await run(process.execPath, [path.join(root, 'scripts/build_edgeone.mjs'), '--output-dir', dist]);
    let html = await readFile(path.join(dist, 'index.html'), 'utf8');
    // Only the in-memory served HTML gets a simulation label; build/source assets stay unchanged.
    html = html.replace('</head>', '<link rel="stylesheet" href="/__demo/toolbar.css"><script type="module" src="/__demo/toolbar.js"></script></head>');
    html = html.replace('<body>', '<body><aside id="demo-toolbar" aria-label="本地模拟演示"><strong>本地模拟 · 零模型 / 数据库 / 公网</strong><span>答案、工单和 SSE 进度均为固定演示数据</span><div id="demo-scenarios"></div><button id="demo-reset" type="button">重置模拟</button><output id="demo-status" aria-live="polite"></output></aside>');
    files.set('/', { body: html, type: 'text/html; charset=utf-8' });
    for (const name of await readdir(path.join(dist, 'ui-assets'))) files.set(`/ui-assets/${name}`, { body: await readFile(path.join(dist, 'ui-assets', name)), type: types[path.extname(name)] });
    for (const name of ['toolbar.js', 'toolbar.css']) files.set(`/__demo/${name}`, { body: await readFile(new URL(`./interview-demo/${name}`, import.meta.url)), type: types[path.extname(name)] });
    let scenario = 'knowledge', sequence = 0, contexts = new Map(), jobs = new Map(), actions = new Map();
    let tickets = new Map([[existingTicket.id, structuredClone(existingTicket)]]), submissions = 0, confirmations = 0, recoveries = 0;
    const streams = new Set();
    const reset = () => {
      for (const stream of streams) stream.end();
      streams.clear(); contexts.clear(); jobs.clear(); actions.clear();
      tickets = new Map([[existingTicket.id, structuredClone(existingTicket)]]);
      submissions = confirmations = recoveries = 0; scenario = 'knowledge';
      // Keep the sequence monotonic so old browser identifiers never refer to new work.
    };
    const requireContext = (req, expected) => {
      const id = req.headers['x-copilot-context-id'];
      if (!contexts.has(id) || contexts.get(id) !== req.headers['x-copilot-context-token'] || (expected && id !== expected)) throw fail(403, 'Simulation context mismatch');
      return id;
    };
    const stateOf = (job) => Date.now() - job.started < durationMs ? 'running' : job.scenario === 'failure' ? 'failed' : 'succeeded';
    server = createServer(async (req, res) => {
      const json = (value, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' }); res.end(JSON.stringify(value)); };
      res.setHeader('Cache-Control', 'no-store');
      res.setHeader('X-Content-Type-Options', 'nosniff');
      res.setHeader('X-Interview-Simulation', 'true');
      res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'");
      try {
        const origin = `http://127.0.0.1:${server.address().port}`;
        if (req.headers.host !== new URL(origin).host || (req.headers.origin && req.headers.origin !== origin)) throw fail(403, 'Loopback same-origin requests only');
        if (!req.url.startsWith('/') || req.url.startsWith('//')) throw fail(400, 'Invalid path');
        const url = new URL(req.url, origin), p = url.pathname, method = req.method;
        if (!['GET', 'POST'].includes(method)) throw fail(405, 'Unsupported simulation method');
        if (method === 'POST' && req.headers.origin !== origin) throw fail(403, 'Same-origin POST required');
        if (method === 'GET' && files.has(p)) { const file = files.get(p); res.writeHead(200, { 'Content-Type': file.type }); res.end(file.body); return; }
        if (p === '/favicon.ico') { res.writeHead(204); res.end(); return; }
        if (p === '/__demo/state' && method === 'GET') return json({ simulation: true, scenario, scenarios, submissions, confirmations, recoveries, ticket_count: tickets.size });
        if (p.startsWith('/__demo/') && method === 'POST') {
          if (req.headers['x-interview-demo'] !== 'local-only') throw fail(403, 'Simulation control header required');
          const body = await jsonBody(req);
          if (p === '/__demo/reset') { reset(); return json({ simulation: true, status: 'reset' }); }
          if (p === '/__demo/scenario' && Object.hasOwn(scenarios, body.scenario)) { scenario = body.scenario; return json({ simulation: true, ...scenarios[scenario] }); }
          throw fail(400, 'Unknown simulation scenario');
        }
        if (p === '/api/auth/me' && method === 'GET') return json({ user: { id: USER, username: '本地模拟访客', role: 'user' }, csrf_token: CSRF });
        if (p === '/api/health' && method === 'GET') return json({ status: 'ok', simulation: true });
        if (p === '/api/ui/config' && method === 'GET') return json({ upload_max_bytes: 4194304 });
        if (p === '/api/documents' && method === 'GET') return json({ documents: [{ document_id: uuid('5', 1), source_name: '模拟资料 · 连接池排查手册.md', chunk_count: 1 }] });
        if (p === '/api/tickets' && method === 'GET') {
          const filtered = [...tickets.values()].filter(t => (!url.searchParams.get('status') || t.status === url.searchParams.get('status')) && (!url.searchParams.get('q') || t.title.includes(url.searchParams.get('q'))));
          return json({ items: filtered.slice(0, 10), limit: 10, offset: 0 });
        }
        if (p.startsWith('/api/tickets/') && method === 'GET') { const ticket = tickets.get(p.split('/').at(-1)); if (!ticket) throw fail(404, 'Simulation ticket not found'); return json(ticket); }
        if (method === 'POST' && req.headers['x-csrf-token'] !== CSRF) throw fail(403, 'Simulation CSRF mismatch');
        if (p === '/api/agent/contexts' && method === 'POST') {
          const id = uuid('3', ++sequence), token = `synthetic-context-${sequence}`; contexts.set(id, token);
          return json({ context_id: id, context_token: token, expires_at: EXPIRES }, 201);
        }
        if (p === '/api/jobs' && method === 'POST') {
          const body = await jsonBody(req);
          if (!['rag', 'agent'].includes(body?.kind) || typeof body?.payload?.question !== 'string' || !body.payload.question.trim() || body.payload.question.length > 2000) throw fail(422, 'Invalid simulation question');
          if (jobs.size >= 100) throw fail(429, 'Reset the simulation after 100 jobs');
          const contextId = body.kind === 'agent' ? requireContext(req) : null;
          const id = uuid('2', ++sequence), actionId = uuid('4', sequence);
          const job = { id, scenario, kind: body.kind, started: Date.now(), actionId };
          if (scenario === 'proposal') {
            if (!contextId) throw fail(422, 'Select Agent mode for the proposal scenario');
            actions.set(actionId, { action_id: actionId, context_id: contextId, status: 'pending', payload: proposalPayload,
              payload_fingerprint: 'b'.repeat(64), created_at: CREATED, expires_at: EXPIRES, ticket_id: null });
          }
          jobs.set(id, job); submissions += 1; return json({ job_id: id, status: 'reserved' }, 202);
        }
        const match = p.match(/^\/api\/jobs\/([a-f0-9-]+)(\/events)?$/);
        if (match && method === 'GET') {
          const job = jobs.get(match[1]); if (!job) throw fail(404, 'Simulation job not found');
          if (!match[2]) {
            recoveries += 1; const status = stateOf(job);
            return json({ job_id: job.id, kind: job.kind, status, failure_type: status === 'failed' ? 'tool_budget' : null,
              progress: status === 'running' ? { stage: 'simulation', label: '【模拟进度】正在读取固定场景' } : null,
              result: status === 'succeeded' ? resultFor(job, actions.get(job.actionId)) : null });
          }
          res.writeHead(200, { 'Content-Type': 'text/event-stream; charset=utf-8' }); streams.add(res);
          const after = Number(url.searchParams.get('after') || 0);
          const send = (id, event, data) => { if (!res.destroyed && id > after) res.write(`id: ${id}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`); };
          send(1, 'progress', { stage: 'simulation', label: '【模拟 SSE】知识 / 工单 fixture 已载入' });
          send(2, 'delta', { text: '【模拟草稿】固定演示文本；未调用模型，尚未展示最终结果。' });
          const timer = setTimeout(() => { send(3, 'terminal', { status: stateOf(job), simulation: true }); res.end(); }, Math.max(0, durationMs - (Date.now() - job.started)));
          res.on('close', () => { clearTimeout(timer); streams.delete(res); }); return;
        }
        const actionMatch = p.match(/^\/api\/actions\/([a-f0-9-]+)(?:\/(confirm|reject))?$/);
        if (actionMatch) {
          const action = actions.get(actionMatch[1]); if (!action) throw fail(404, 'Simulation action not found');
          requireContext(req, action.context_id);
          if (method === 'GET' && !actionMatch[2]) return json(action);
          if (method === 'POST' && actionMatch[2]) {
            const body = await jsonBody(req); if (body.payload_fingerprint !== action.payload_fingerprint) throw fail(409, 'Simulation fingerprint mismatch');
            if (action.status === 'pending') {
              if (actionMatch[2] === 'confirm') {
                const id = uuid('6', ++sequence); action.status = 'completed'; action.ticket_id = id; confirmations += 1;
                tickets.set(id, { id, ...action.payload, status: 'open', created_at: CREATED, updated_at: CREATED });
              } else action.status = 'cancelled';
            }
            return json(action);
          }
        }
        throw fail(404, 'Unsupported in local simulation; no request is forwarded');
      } catch (error) { if (!res.headersSent) json({ detail: error.status ? error.message : 'Simulation error' }, error.status || 500); else res.end(); }
    });
    server.requestTimeout = 10000;
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', resolve); });
    return { url: `http://127.0.0.1:${server.address().port}`, scratch,
      close: async () => { for (const stream of streams) stream.end(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); await rm(scratch, { recursive: true, force: true }); } };
  } catch (error) { server?.close(); await rm(scratch, { recursive: true, force: true }); throw error; }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== '--port' || !/^\d+$/.test(args[1]))) {
    process.stderr.write('Usage: node scripts/interview_demo.mjs [--port 4173]\n'); process.exitCode = 1;
  } else {
    try {
      const demo = await startDemo({ port: args.length ? Number(args[1]) : 4173 });
      process.stdout.write(`本地模拟演示：${demo.url}\n固定 fixture / 模拟 SSE；零模型、数据库及公网请求。Ctrl+C 退出并删除临时静态构建。\n`);
      let closing = false;
      const stop = async () => { if (closing) return; closing = true; await demo.close(); };
      process.once('SIGINT', stop); process.once('SIGTERM', stop);
    } catch (error) { process.stderr.write(`Demo failed: ${error.message}\n`); process.exitCode = 1; }
  }
}
