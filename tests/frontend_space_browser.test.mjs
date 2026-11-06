/** Built EdgeOne preview only: every API and asset request is intercepted. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const run = promisify(execFile);
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../', import.meta.url));
const qaFont = process.env.PLAYWRIGHT_TEST_CJK_FONT ? await readFile(process.env.PLAYWRIGHT_TEST_CJK_FONT) : null;
const qaSerifFont = process.env.PLAYWRIGHT_TEST_SERIF_FONT ? await readFile(process.env.PLAYWRIGHT_TEST_SERIF_FONT) : null;
const USER = '11111111-1111-4111-8111-111111111111';
const OTHER = '99999999-9999-4999-8999-999999999999';
const JOB = '22222222-2222-4222-8222-222222222222';
const CONTEXT = '33333333-3333-4333-8333-333333333333';
const ACTION = '44444444-4444-4444-8444-444444444444';
const CSRF = 'a'.repeat(64);
const ids = Array.from({ length: 8 }, (_, i) => `0000000${i}-aaaa-4aaa-8aaa-000000000000`);
const longAnswer = '# 故障分析与处理建议\n\n查询完成后，先核对**真实工单与原文证据**，再决定是否创建新工单。\n\n## 结论与依据\n\n' +
  '- 检索请求超时影响约三成用户，但登录与工单正常。\n- 先降低同时运行的重排任务数量，再观察队列和延迟。\n\n' +
  '> 提案只供审核，确认前不会写入工单。\n\n' +
  '| 检查项 | 实际观察 | 下一步 |\n| --- | --- | --- |\n| 检索延迟 | 超过八秒 | 检查排队 |\n| 原工单 | 没有相同问题 | 预览提案 |\n\n' +
  '```python\nobserved_job_identifier = "' + 'long-code-identifier-'.repeat(70) + '"\nprint(observed_job_identifier)\n```\n\n' +
  '### 完整核对记录\n\n' + '这是合成浏览器预览中的研发排查说明，所有模型和业务结果均由测试提供。'.repeat(32);
let browser, scratch;
const files = new Map();
before(async () => {
  scratch = await mkdtemp(path.join(tmpdir(), 'pc-chat-space-build-'));
  const dist = path.join(scratch, 'dist');
  await run(process.execPath, [path.join(root, 'scripts/build_edgeone.mjs'), '--output-dir', dist]);
  files.set('/', { body: await readFile(path.join(dist, 'index.html')), type: 'text/html' });
  const types = { '.mjs': 'text/javascript', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.webp': 'image/webp' };
  for (const file of await readdir(path.join(dist, 'ui-assets'))) {
    files.set(`/ui-assets/${file}`, { body: await readFile(path.join(dist, 'ui-assets', file)), type: types[path.extname(file)] || 'application/octet-stream' });
  }
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); if (scratch) await rm(scratch, { recursive: true, force: true }); });

async function fixture({ width = 1440, height = width === 1440 ? 900 : width === 1280 ? 800 : 844,
  records = [], currentUser = USER, state = 'succeeded', showProposal = true } = {}) {
  const context = await browser.newContext({ viewport: { width, height }, serviceWorkers: 'block' });
  const personal = { expires_at: new Date(Date.now() + 24 * 3600000).toISOString(), remaining_seconds: 86399,
    job_limit: 500, jobs_used: 37, user_daily_limit: 1000, global_daily_limit: 2000,
    concurrent_limit: 3, occupied_slots: 2, worker_enabled: true };
  const f = { calls: [], unexpected: [], errors: [], state, states: new Map(), currentUser, submitted: false };
  await context.addInitScript(({ records, owner, other, otherJob }) => {
    if (!localStorage.getItem(`pc.jobs.v1.${owner}`)) localStorage.setItem(`pc.jobs.v1.${owner}`, JSON.stringify(records));
    if (!localStorage.getItem(`pc.jobs.v1.${other}`)) localStorage.setItem(`pc.jobs.v1.${other}`, JSON.stringify([{ id: otherJob, kind: 'agent', createdAt: Date.now() }]));
  }, { records, owner: USER, other: OTHER, otherJob: '88888888-8888-4888-8888-888888888888' });
  await context.route('**/*', async (route) => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname;
    if (url.origin !== 'https://space-preview.invalid') { f.unexpected.push(url.origin); return route.abort(); }
    const staticFile = req.method() === 'GET' && files.get(p);
    if (staticFile) return route.fulfill({ status: 200, contentType: staticFile.type, body: staticFile.body });
    if (p === '/__qa_font' && qaFont) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaFont });
    if (p === '/__qa_serif_font' && qaSerifFont) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaSerifFont });
    if (p === '/favicon.ico') return route.fulfill({ status: 204 });
    f.calls.push({ path: p, method: req.method() });
    const reply = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (p === '/api/auth/me') return reply({ user: { id: f.currentUser, username: f.currentUser === USER ? 'qinghe' : 'other-user', role: 'user' }, csrf_token: CSRF });
    if (p === '/api/health') return reply({ status: 'ok' });
    if (p === '/api/ui/config') return reply({ upload_max_bytes: 4194304, personal_demo: personal });
    if (p === '/api/agent/contexts') return reply({ context_id: CONTEXT, context_token: 'c'.repeat(43), expires_at: new Date(Date.now() + 3600000).toISOString() }, 201);
    if (p === '/api/jobs' && req.method() === 'POST') { f.submitted = true; return reply({ job_id: JOB, status: 'reserved' }, 202); }
    const jobMatch = p.match(/^\/api\/jobs\/([\da-f-]+)$/);
    if (jobMatch && req.method() === 'GET') {
      const id = jobMatch[1], status = f.states.get(id) || (id === JOB ? f.state : 'unknown');
      const result = status === 'succeeded' ? { status: 'answered', answer: longAnswer, elapsed_ms: 2531,
        sources: [
          { id: `knowledge:${CONTEXT}`, kind: 'knowledge', data: { text: '合成研发案例：检索延迟上升，需先核对队列和重排并发。', metadata: { source_name: '研发排障知识.md', chunk_id: CONTEXT } } },
          ...(showProposal ? [{ id: `proposal:${ACTION}`, kind: 'proposal', data: { action_id: ACTION, payload: { title: '检索超时排查演示提案', description: '先检查现有工单与知识案例，再决定是否确认创建。' } } }] : []),
        ] } : null;
      return reply({ job_id: id, kind: 'agent', status, failure_type: status === 'failed' ? 'tool_budget' : null,
        progress: status === 'running' ? { stage: 'tool', label: '正在查询工单' } : null, result });
    }
    if (p.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: 'id: 1\nevent: progress\ndata: {"stage":"tool","label":"正在查询工单"}\n\n' });
    if (p === `/api/actions/${ACTION}` && req.method() === 'GET') return reply({ action_id: ACTION,
      context_id: CONTEXT, status: 'pending', payload: { title: '检索超时排查演示提案', description: '请先核对完整提案内容。\n这是本机合成预览，不会访问真实业务服务。\n'.repeat(3) },
      payload_fingerprint: 'b'.repeat(64), created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 900000).toISOString(), ticket_id: null });
    f.unexpected.push(`${req.method()} ${p}`); return reply({ detail: 'unexpected mock request' }, 599);
  });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror', (error) => f.errors.push(error.message));
  await page.goto('https://space-preview.invalid/', { waitUntil: 'networkidle' });
  await page.locator('#app-shell').waitFor({ state: 'visible' });
  if (qaFont) await page.addStyleTag({ content: '@font-face{font-family:"Microsoft YaHei";src:url("/__qa_font")} @font-face{font-family:"Noto Serif SC";src:url("/__qa_font")}' });
  if (qaSerifFont) await page.addStyleTag({ content: '@font-face{font-family:SimSun;src:url("/__qa_serif_font")} :root{--font-display:SimSun,serif} .markdown-answer code{font-family:ui-monospace,SimSun,monospace}' });
  await page.evaluate(() => document.fonts.ready);
  return { context, page, f };
}
const jobPosts = (f) => f.calls.filter((c) => c.path === '/api/jobs' && c.method === 'POST').length;
async function sendAgent(page) {
  await page.locator('#question-input').fill('请查询类似工单，分析知识库案例，并生成待确认提案。');
  await page.locator('#send-button').click();
  await page.locator('.proposal-card .proposal-title').getByText('检索超时排查演示提案').waitFor();
}
async function assertSafe(f) { assert.deepEqual(f.unexpected, []); assert.deepEqual(f.errors, []); }
async function assertNoPageOverflow(page) {
  const metrics = await page.evaluate(() => ({ width: innerWidth, height: innerHeight,
    bodyWidth: document.body.scrollWidth, htmlWidth: document.documentElement.scrollWidth,
    bodyHeight: document.body.scrollHeight, htmlHeight: document.documentElement.scrollHeight }));
  assert.ok(metrics.bodyWidth <= metrics.width && metrics.htmlWidth <= metrics.width, JSON.stringify(metrics));
  assert.ok(metrics.bodyHeight <= metrics.height + 1 && metrics.htmlHeight <= metrics.height + 1, 'page itself must not add a second scrollbar');
  const composer = await page.locator('#composer-form').boundingBox();
  assert.ok(composer && composer.y >= 0 && composer.y + composer.height <= metrics.height + 1, 'composer stays reachable');
}

for (const width of [1440, 1280, 390]) {
  test(`built preview gives long Agent answers, proposals and composer space at ${width}px`, async () => {
    const records = ids.map((id, i) => ({ id, kind: i % 2 ? 'rag' : 'agent', createdAt: Date.now() - i * 60000 }));
    const { context, page, f } = await fixture({ width, records });
    f.states.set(ids[0], 'unknown'); f.states.set(ids[1], 'failed'); f.states.set(ids[2], 'running');
    f.states.set(ids[3], 'reserved'); f.states.set(ids[4], 'succeeded');
    try {
      assert.equal(await page.locator('#task-center-dialog').evaluate((el) => el.open), false);
      assert.equal(await page.locator('#job-recovery').isVisible(), false);
      assert.equal(await page.locator('#task-center-count').textContent(), '8');
      assert.equal(await page.locator('.chat-toolbar').count(), 0);
      const header = await page.locator('.workspace-header').boundingBox();
      if (width > 500) assert.ok(header.height >= 60 && header.height <= 72, `header ${header.height}`);
      const initial = await page.locator('#chat-messages').boundingBox();
      console.info(JSON.stringify({ preview: page.viewportSize(), headerHeight: header.height, chatHeight: initial.height }));
      assert.ok(initial.height >= page.viewportSize().height * (width > 500 ? .55 : .45), `chat height ${initial.height}`);
      await sendAgent(page);
      const avatar = page.locator('.message.user .message-avatar img');
      assert.equal(await avatar.count(), 1);
      assert.ok(await avatar.evaluate((el) => el.complete && el.naturalWidth > 0));
      assert.equal(await page.locator('.message.user .message-label').count(), 0);
      assert.equal(await page.locator('.assistant-seal').textContent(), '知');
      assert.equal(await page.locator('.message-label strong').textContent(), '知行助手');
      const account = page.locator('#account-panel img');
      assert.ok(await account.evaluate((el) => el.complete && el.naturalWidth > 0));
      assert.equal(await account.getAttribute('src'), await avatar.getAttribute('src'));
      assert.equal(await page.locator('.message.assistant .message-bubble').count(), 0);
      assert.equal(await page.locator('.markdown-answer').evaluate((el) => getComputedStyle(el).backgroundColor), 'rgba(0, 0, 0, 0)');
      for (const selector of ['h2', 'h3', 'ul', 'blockquote', 'table', 'pre']) assert.ok(await page.locator(`.markdown-answer ${selector}`).count());
      assert.equal(await page.locator('.proposal-card').getByRole('button', { name: '确认创建工单' }).isEnabled(), true);
      assert.equal(await page.locator('.proposal-card').getByRole('button', { name: '拒绝提案' }).isEnabled(), true);
      await page.locator('.proposal-card').scrollIntoViewIfNeeded();
      await assertNoPageOverflow(page);
      assert.equal(jobPosts(f), 1);
      assert.equal(f.calls.some((c) => /\/(confirm|reject)$/.test(c.path)), false);
      await page.screenshot({ path: `/tmp/pc-space-${width}-proposal.png`, fullPage: true });
      await page.locator('#chat-messages').evaluate((el) => { el.style.scrollBehavior = 'auto'; el.scrollTop = 0; });
      await page.screenshot({ path: `/tmp/pc-space-${width}-answer.png`, fullPage: true });
      await page.locator('#task-center-toggle').click();
      await page.locator('#task-center-dialog').waitFor({ state: 'visible' });
      await page.waitForFunction(() => document.querySelectorAll('#job-recovery .job-state[data-state]').length === 8);
      await page.screenshot({ path: `/tmp/pc-space-${width}-tasks.png`, fullPage: true });
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('#task-center-dialog').evaluate((el) => el.open), false);
      assert.equal(await page.locator('#task-center-toggle').evaluate((el) => el === document.activeElement), true);
      await assertSafe(f);
    } finally { await context.close(); }
  });
}

test('task center traps keyboard focus and closing does not stop a running wait', async () => {
  const { context, page, f } = await fixture({ width: 390, state: 'running', showProposal: false });
  try {
    await page.locator('#question-input').fill('请查询工单。');
    await page.locator('#send-button').click();
    await page.locator('.job-progress').getByText('正在查询工单').waitFor();
    assert.equal(await page.locator('#stop-wait').isVisible(), true);
    await page.locator('#task-center-toggle').click();
    await page.locator('#task-center-dialog').waitFor({ state: 'visible' });
    for (let i = 0; i < 12; i += 1) {
      await page.keyboard.press('Tab');
      assert.equal(await page.locator('#task-center-dialog').evaluate((el) => el.contains(document.activeElement)), true);
    }
    await page.locator('#task-center-close').click();
    assert.equal(await page.locator('#stop-wait').isVisible(), true);
    assert.equal(await page.locator('.waiting-stopped').count(), 0);
    await page.locator('#stop-wait').click();
    await page.locator('.waiting-stopped').waitFor();
    assert.equal(jobPosts(f), 1);
    assert.equal(f.calls.some((c) => c.method === 'DELETE' || /cancel/.test(c.path)), false);
    await assertSafe(f);
  } finally { await context.close(); }
});

test('composer grows only to a bounded height and resets after submitting without covering messages', async () => {
  const { context, page, f } = await fixture({ width: 390 });
  try {
    const input = page.locator('#question-input');
    const initial = await input.boundingBox();
    await input.fill(Array.from({ length: 35 }, (_, i) => `第${i + 1}行：请查询相关研发知识与工单。`).join('\n'));
    const grown = await input.boundingBox();
    assert.ok(grown.height > initial.height && grown.height <= 180, `textarea ${initial.height} → ${grown.height}`);
    await assertNoPageOverflow(page);
    await page.locator('#send-button').click();
    await page.locator('.proposal-card .proposal-title').waitFor();
    const reset = await input.boundingBox();
    assert.ok(reset.height <= initial.height + 2);
    assert.equal(await input.inputValue(), '');
    assert.equal(jobPosts(f), 1);
    await assertSafe(f);
  } finally { await context.close(); }
});

test('task center shows only the signed-in account metadata and never stores question or credentials', async () => {
  const records = [{ id: ids[0], kind: 'agent', createdAt: Date.now(), status: 'succeeded', context_token: 'MUST_NOT_PERSIST', question: 'PRIVATE_QUESTION' }];
  const { context, page, f } = await fixture({ records });
  try {
    await page.locator('#task-center-toggle').click();
    const drawer = page.locator('#task-center-dialog');
    assert.doesNotMatch(await drawer.textContent(), /88888888|succeeded|PRIVATE_QUESTION|MUST_NOT_PERSIST/);
    assert.equal(jobPosts(f), 0);
    assert.equal(f.calls.some((c) => c.method === 'POST'), false);
    await page.keyboard.press('Escape');
    await page.reload({ waitUntil: 'networkidle' });
    assert.equal(await page.locator('#task-center-dialog').evaluate((el) => el.open), false);
    assert.equal(await page.locator('#task-center-count').textContent(), '1');
    await assertSafe(f);
  } finally { await context.close(); }
});

test('unknown and failed task entries can be hidden and restored without deleting or replaying jobs', async () => {
  const records = [
    { id: ids[0], kind: 'agent', createdAt: Date.now(), context_token: 'MUST_NOT_PERSIST', question: 'PRIVATE_QUESTION' },
    { id: ids[1], kind: 'rag', createdAt: Date.now() - 10000 },
  ];
  const { context, page, f } = await fixture({ records });
  f.states.set(ids[0], 'unknown'); f.states.set(ids[1], 'failed');
  try {
    await page.locator('#task-center-toggle').click();
    const first = page.locator('.job-recovery-row').filter({ hasText: ids[0].slice(0, 8) });
    await first.getByRole('button', { name: '隐藏此记录' }).waitFor();
    await first.getByRole('button', { name: '隐藏此记录' }).click();
    assert.equal(await page.locator('#task-center-count').textContent(), '1');
    const stored = await page.evaluate((key) => JSON.parse(localStorage.getItem(key)), `pc.jobs.v1.${USER}`);
    const hidden = stored.find((record) => record.id === ids[0]);
    assert.equal(hidden.hidden, true);
    assert.equal(stored.length, 2);
    assert.ok(stored.every((record) => Object.keys(record).every((key) => ['id', 'kind', 'createdAt', 'lastSequence', 'hidden'].includes(key))));
    assert.doesNotMatch(JSON.stringify(stored), /MUST_NOT_PERSIST|PRIVATE_QUESTION|context_token|result|status/);
    await page.locator('#task-center-show-hidden').click();
    await first.getByRole('button', { name: '取消隐藏' }).waitFor();
    await page.keyboard.press('Escape');
    await page.reload({ waitUntil: 'networkidle' });
    assert.equal(await page.locator('#task-center-count').textContent(), '1');
    await page.locator('#task-center-toggle').click();
    await page.locator('#task-center-show-hidden').click();
    await first.getByRole('button', { name: '取消隐藏' }).click();
    assert.equal(await page.locator('#task-center-count').textContent(), '2');
    assert.equal(jobPosts(f), 0);
    assert.equal(f.calls.some((call) => call.method !== 'GET'), false);
    await assertSafe(f);
  } finally { await context.close(); }
});

test('switching authenticated account shows only that account local recovery identifiers', async () => {
  const { context, page, f } = await fixture({ records: [{ id: ids[0], kind: 'agent', createdAt: Date.now() }] });
  try {
    await page.locator('#task-center-toggle').click();
    assert.match(await page.locator('#job-recovery').textContent(), new RegExp(ids[0].slice(0, 8)));
    f.currentUser = OTHER;
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('#task-center-toggle').click();
    const content = await page.locator('#job-recovery').textContent();
    assert.match(content, /88888888/);
    assert.doesNotMatch(content, new RegExp(ids[0].slice(0, 8)));
    assert.equal(jobPosts(f), 0);
    await assertSafe(f);
  } finally { await context.close(); }
});

test('32 recovery entries stay inside the drawer, use bounded GET reads and close on backdrop click', async () => {
  const records = Array.from({ length: 32 }, (_, i) => ({ id: `${i.toString(16).padStart(8, '0')}-aaaa-4aaa-8aaa-000000000000`,
    kind: i % 2 ? 'rag' : 'agent', createdAt: Date.now() - i * 60000 }));
  const { context, page, f } = await fixture({ records, width: 1280 });
  try {
    assert.equal(await page.locator('#task-center-count').textContent(), '32');
    assert.equal(f.calls.some((call) => /\/jobs\//.test(call.path)), false);
    const before = await page.locator('#chat-messages').boundingBox();
    await page.locator('#task-center-toggle').click();
    assert.equal(await page.locator('.job-recovery-row').count(), 32);
    await page.waitForTimeout(1800);
    const reads = f.calls.filter((call) => /^\/api\/jobs\/[\da-f-]+$/.test(call.path));
    assert.ok(reads.length > 0 && reads.length <= 8, `bounded read count ${reads.length}`);
    assert.ok(reads.every((call) => call.method === 'GET'));
    assert.ok(await page.locator('#job-recovery').evaluate((el) => el.scrollHeight > el.clientHeight));
    await page.screenshot({ path: '/tmp/pc-space-1280-tasks-32.png', fullPage: true });
    await page.mouse.click(8, 240);
    assert.equal(await page.locator('#task-center-dialog').evaluate((el) => el.open), false);
    assert.equal(await page.locator('#task-center-toggle').evaluate((el) => el === document.activeElement), true);
    const after = await page.locator('#chat-messages').boundingBox();
    assert.equal(before.height, after.height);
    assert.equal(jobPosts(f), 0);
    await assertNoPageOverflow(page);
    await assertSafe(f);
  } finally { await context.close(); }
});
