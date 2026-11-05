import test from 'node:test';
import assert from 'node:assert/strict';
import { runPublicJob, resumePublicJob, readJobEvents } from '../src/project_copilot/web/assets/api.js';
import { renderMarkdown } from '../src/project_copilot/web/assets/markdown.js';

const ID = '11111111-1111-4111-8111-111111111111';
const delay = async () => {};
const sse = (chunks) => new Response(new ReadableStream({ start(controller) {
  for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk)); controller.close();
} }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } });
const frame = (id, event, data) => `${id === null ? '' : `id: ${id}\n`}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

test('SSE reconnect/dedupe uses real event IDs and never triggers a write', async () => {
  const events = [];
  const value = frame(3, 'delta', { text: '中文' });
  const out = await readJobEvents(ID, { after: 2, onEvent: (event) => events.push(event), fetchImpl: async (url, options) => {
    assert.equal(url, `/jobs/${ID}/events?after=2`); assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers['Last-Event-ID'], '2'); assert.equal(options.body, undefined);
    return sse([value.slice(0, 19), value.slice(19), value, frame(4, 'terminal', { status: 'succeeded' })]);
  } });
  assert.deepEqual(events.map((e) => e.id), [3, 4]); assert.equal(out.after, 4); assert.equal(out.terminal, true);
});

test('reset accepts lower server cursor and old terminal snapshots end waiting', async () => {
  const events = [];
  const out = await readJobEvents(ID, { after: 9999, onEvent: (event) => events.push(event), fetchImpl: async () => sse([
    frame(3, 'reset', { cursor_reset: true, status: 'running' }), frame(4, 'delta', { text: 'new' }), frame(null, 'snapshot', { status: 'unknown' }),
  ]) });
  assert.deepEqual(events.map((e) => e.event), ['reset', 'delta']); assert.equal(out.after, 4); assert.equal(out.terminal, true);
});

test('stream end reconnects to same job and final answer comes from canonical GET', async () => {
  let posts = 0, gets = 0, streams = 0; const seen = [];
  const out = await runPublicJob('rag', { question: 'q' }, { createId: () => 'fixed', delay, useEvents: true,
    request: async (_path, options = {}) => {
      if (options.method === 'POST') { posts++; return { data: { job_id: ID } }; }
      gets++; return { data: streams < 2 ? { status: 'running' } : { status: 'succeeded', result: { answer: 'verified' } } };
    }, eventReader: async (id, options) => {
      assert.equal(id, ID); streams++; seen.push(options.after);
      options.onEvent({ id: streams, event: 'delta', data: { text: 'unverified' } });
      return { after: streams, terminal: streams === 2 };
    } });
  assert.equal(posts, 1); assert.equal(gets, 3); assert.deepEqual(seen, [0, 1]); assert.equal(out.data.answer, 'verified');
});

test('broken SSE falls back to polling and preserves one POST attempt', async () => {
  let posts = 0, gets = 0, streams = 0;
  const out = await runPublicJob('rag', {}, { createId: () => 'fixed', delay, useEvents: true,
    request: async (_path, options = {}) => {
      if (options.method === 'POST') { posts++; return { data: { job_id: ID } }; }
      gets++; return { data: gets < 4 ? { status: 'running', progress: { stage: 'search', label: '真实检索' } } : { status: 'succeeded', result: { answer: 'ok' } } };
    }, eventReader: async () => { streams++; throw new TypeError('offline'); } });
  assert.equal(posts, 1); assert.equal(streams, 2); assert.equal(gets, 4); assert.equal(out.data.answer, 'ok');
});

test('stop wait aborts reads and retains unknown rather than marking it failed', async () => {
  const controller = new AbortController(); let posts = 0;
  await assert.rejects(runPublicJob('agent', {}, { signal: controller.signal, delay,
    createId: () => 'fixed', onSubmitted: ({ id }) => { assert.equal(id, ID); controller.abort(); },
    request: async (_path, options) => { assert.equal(options.method, 'POST'); posts++; return { data: { job_id: ID } }; },
  }), /停止等待/);
  assert.equal(posts, 1);
  await assert.rejects(resumePublicJob(ID, { request: async (_path, options) => {
    assert.equal(options.method, undefined); return { data: { status: 'unknown' } };
  } }), (error) => error.meta.jobStatus === 'unknown' && /待核实/.test(error.message));
});

test('event type/schema and oversized frames fail without rendering arbitrary HTML', async () => {
  await assert.rejects(readJobEvents(ID, { fetchImpl: async () => sse([frame(1, 'delta', 'bad')]) }), /格式无效/);
  await assert.rejects(readJobEvents(ID, { fetchImpl: async () => sse(['x'.repeat(131073)]) }), /超过限制/);
  await assert.rejects(readJobEvents(ID, { fetchImpl: async () => new Response('<html>', { headers: { 'Content-Type': 'text/html' } }) }), /实时进度/);
});

test('SSE abort closes pending transport instead of submitting replacement task', async () => {
  const controller = new AbortController(); let aborted = false;
  const out = readJobEvents(ID, { signal: controller.signal, fetchImpl: async (_url, options) => {
    return new Promise((_resolve, reject) => { options.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('aborted', 'AbortError')); }); });
  } });
  controller.abort(); await assert.rejects(out, /停止等待/); assert.equal(aborted, true);
});

class FakeNode {
  constructor(tag, text = '') { this.tagName = tag; this._text = text; this.children = []; this.dataset = {}; }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
}
const doc = { createElement: (tag) => new FakeNode(tag), createTextNode: (text) => new FakeNode('#text', text) };
const all = (node) => [node, ...node.children.flatMap(all)];

test('Markdown creates title, paragraph, list, quote, code and table nodes with paper typography class', () => {
  const out = renderMarkdown('# 标题\n\n正文 **强调** 与 *重点*。\n\n- 项目甲\n- 项目乙\n\n1. 步骤\n\n> 引用\n\n```js\nconst x = 1;\n```\n\n| 名称 | 状态 |\n| --- | --- |\n| 工单 | open |', doc);
  const tags = all(out).map((n) => n.tagName);
  for (const tag of ['h2', 'p', 'strong', 'em', 'ul', 'ol', 'blockquote', 'pre', 'code', 'table', 'th', 'td']) assert.ok(tags.includes(tag), tag);
  assert.equal(out.className, 'markdown-answer'); assert.match(out.textContent, /const x = 1/);
});

test('Markdown leaves hostile HTML as text and rejects executable/data/password URL links', () => {
  const hostile = '<img src=x onerror=alert(1)> <script>alert(1)</script> [a](javascript:alert(1)) [b](data:text/html,evil) [c](https://user:secret@example.com)';
  const out = renderMarkdown(hostile, doc), tags = all(out).map((n) => n.tagName);
  assert.ok(!tags.includes('img')); assert.ok(!tags.includes('script')); assert.ok(!tags.includes('a'));
  assert.ok(out.textContent.includes('<img')); assert.ok(out.textContent.includes('javascript:'));
});

test('Markdown HTTPS links have safe navigation attributes and code never renders HTML', () => {
  const out = renderMarkdown('[说明](https://example.com/docs)\n\n```html\n<img src=x onerror=alert(1)>\n```', doc);
  const link = all(out).find((n) => n.tagName === 'a');
  assert.equal(link.href, 'https://example.com/docs'); assert.equal(link.rel, 'noopener noreferrer'); assert.equal(link.target, '_blank');
  assert.ok(!all(out).some((n) => n.tagName === 'img')); assert.ok(out.textContent.includes('<img'));
});

test('stop while initial POST is pending still saves committed ID and does not cancel/repeat POST', async () => {
  let release; const gate = new Promise((resolve) => { release = resolve; });
  const controller = new AbortController(); let posts = 0; let saved;
  const result = runPublicJob('rag', {}, { createId: () => 'once', signal: controller.signal,
    onSubmitted: (record) => { saved = record; }, request: async (_path, options) => {
      posts++; assert.equal(options.method, 'POST'); assert.equal(options.signal, undefined);
      await gate; return { data: { job_id: ID } };
    } });
  controller.abort(); release(); await assert.rejects(result, /停止等待/);
  assert.equal(saved.id, ID); assert.equal(posts, 1);
});

test('wall-clock budget bounds SSE reconnections independently of poll count', async () => {
  let clock = 0, reads = 0;
  await assert.rejects(resumePublicJob(ID, { request: async () => { reads++; return { data: { status: 'running' } }; },
    useEvents: true, now: () => clock, maxWaitMs: 1000, delay,
    eventReader: async (_id, options) => { assert.equal(options.timeoutMs, 1000); clock = 1001; return { after: 0, terminal: false }; },
  }), /仍在处理/);
  assert.equal(reads, 1);
});
