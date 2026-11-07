/** Actual static build + real browser IndexedDB; all HTTP is intercepted. */
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
const qaSerif = process.env.PLAYWRIGHT_TEST_SERIF_FONT ? await readFile(process.env.PLAYWRIGHT_TEST_SERIF_FONT) : null;
const USER = '11111111-1111-4111-8111-111111111111';
const OTHER = '99999999-9999-4999-8999-999999999999';
const LEGACY = '051fd1eb-979e-4a83-852b-662b65f43342'; // A fixture only; never handled specially by application code.
const OTHER_JOB = '77777777-7777-4777-8777-777777777777';
const CSRF = 'a'.repeat(64);
const id = (prefix, n) => `${prefix.repeat(8)}-${prefix.repeat(4)}-4${prefix.repeat(3)}-8${prefix.repeat(3)}-${String(n).padStart(12, '0')}`;
const longAnswer = '# 排查结论\n\n这是通过最终校验的**合成回答**，所有请求均由测试截获。\n\n## 建议\n\n' +
  '- 先查原文\n- 核对工单\n\n> 提案需要独立确认。\n\n| 项目 | 结果 |\n| --- | --- |\n| 引用 | 可核对 |\n\n' +
  '```js\nconst longIdentifier = "' + 'code-identifier-'.repeat(80) + '";\n```\n\n' +
  '### 详细核对\n\n' + '这是合成预览里的研发排查说明，不能当作真实业务证据。'.repeat(24) +
  '\n\n<img src=x onerror=alert(1)>';
let browser, scratch;
const files = new Map();
before(async () => {
  scratch = await mkdtemp(path.join(tmpdir(), 'pc-history-preview-'));
  const dist = path.join(scratch, 'dist');
  await run(process.execPath, [path.join(root, 'scripts/build_edgeone.mjs'), '--output-dir', dist]);
  files.set('/', { body: await readFile(path.join(dist, 'index.html')), type: 'text/html' });
  const types = { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.webp': 'image/webp' };
  for (const name of await readdir(path.join(dist, 'ui-assets'))) files.set(`/ui-assets/${name}`, {
    body: await readFile(path.join(dist, 'ui-assets', name)), type: types[path.extname(name)] || 'application/octet-stream',
  });
  browser = await chromium.launch({ headless: true });
});
after(async () => { await browser?.close(); if (scratch) await rm(scratch, { recursive: true, force: true }); });

async function fixture({ width = 1440, records = [], unavailable = false, proposal = false } = {}) {
  const context = await browser.newContext({ viewport: { width, height: width === 1440 ? 900 : width === 1280 ? 800 : 844 }, serviceWorkers: 'block' });
  const f = { calls: [], unexpected: [], errors: [], dialogs: [], jobs: new Map(), actions: new Map(),
    userId: USER, currentState: 'succeeded', proposal, contexts: 0, questionIndex: 0, nextDialog: null,
    submitGate: null, releaseSubmit: null, previewFailStatus: null };
  await context.addInitScript(({ owner, records, unavailable }) => {
    if (records.length && !localStorage.getItem(`pc.jobs.v1.${owner}`)) localStorage.setItem(`pc.jobs.v1.${owner}`, JSON.stringify(records));
    if (unavailable) Object.defineProperty(window, 'indexedDB', { configurable: true, get() { throw new DOMException('Unavailable', 'SecurityError'); } });
  }, { owner: USER, records, unavailable });
  const personal = { expires_at: new Date(Date.now() + 86400000).toISOString(), remaining_seconds: 86399,
    job_limit: 500, jobs_used: 37, user_daily_limit: 1000, global_daily_limit: 2000,
    concurrent_limit: 3, occupied_slots: 1, worker_enabled: true };
  await context.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url()), p = url.pathname;
    if (url.origin !== 'https://history-preview.invalid') { f.unexpected.push(url.origin); return route.abort(); }
    if (req.method() === 'GET' && files.has(p)) {
      const file = files.get(p); return route.fulfill({ status: 200, contentType: file.type, body: file.body });
    }
    if (p === '/__qa_font' && qaFont) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaFont });
    if (p === '/__qa_serif' && qaSerif) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaSerif });
    if (p === '/favicon.ico') return route.fulfill({ status: 204 });
    const body = req.headers()['content-type']?.includes('application/json') ? req.postDataJSON() : null;
    f.calls.push({ path: p, method: req.method(), body, headers: req.headers() });
    const reply = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (p === '/api/auth/me') return f.userId ? reply({ user: { id: f.userId, username: f.userId === USER ? 'qinghe' : 'other-user', role: 'user' }, csrf_token: CSRF }) : reply({ detail: 'signed out' }, 401);
    if (p === '/api/auth/logout') { f.userId = null; return reply({ status: 'ok' }); }
    if (p === '/api/auth/login') { f.userId = body.username === 'other-user' ? OTHER : USER; return reply({ user: { id: f.userId, username: body.username, role: 'user' }, csrf_token: CSRF }); }
    if (p === '/api/health') return reply({ status: 'ok' });
    if (p === '/api/ui/config') return reply({ upload_max_bytes: 4194304, personal_demo: personal });
    if (p === '/api/tickets' && req.method() === 'GET') return reply({ items: [], limit: 10, offset: 0 });
    if (p === '/api/actions/preview' && req.method() === 'POST' && f.previewFailStatus) return reply({ detail: '受控上游失败' }, f.previewFailStatus);
    if (p === '/api/agent/contexts') {
      f.contexts += 1; return reply({ context_id: id('3', f.contexts), context_token: `CONTEXT_SECRET_${f.contexts}`, expires_at: new Date(Date.now() + 3600000).toISOString() }, 201);
    }
    if (p === '/api/jobs' && req.method() === 'POST') {
      f.questionIndex += 1; const jobId = id('2', f.questionIndex), actionId = id('4', f.questionIndex);
      const job = { id: jobId, kind: body.kind, question: body.payload.question, state: f.currentState, actionId, proposal: f.proposal };
      f.jobs.set(jobId, job);
      if (job.proposal) f.actions.set(actionId, { action_id: actionId, context_id: req.headers()['x-copilot-context-id'], status: 'pending',
        payload: { title: '演示工单提案', description: '这是合成预览，只用于核对历史只读边界。\n'.repeat(5) },
        payload_fingerprint: 'b'.repeat(64), expires_at: new Date(Date.now() + 900000).toISOString(), created_at: new Date().toISOString(), ticket_id: null });
      if (f.submitGate) await f.submitGate;
      return reply({ job_id: jobId, status: 'reserved' }, 202);
    }
    const getJob = p.match(/^\/api\/jobs\/([\da-f-]+)$/);
    if (getJob && req.method() === 'GET') {
      const jobId = getJob[1], job = f.jobs.get(jobId) || { id: jobId, kind: 'agent', state: 'unknown' };
      const citations = [{ number: 1, text: '引用原文：保持权限隔离，不重放历史操作。', metadata: { source_name: '历史安全说明.md', chunk_id: id('5', 1), page: 0, context_token: 'SOURCE_SECRET' } }];
      const sources = [{ id: `knowledge:${id('5', 1)}`, kind: 'knowledge', data: citations[0] },
        ...(job.proposal ? [{ id: `proposal:${job.actionId}`, kind: 'proposal', data: { action_id: job.actionId, payload: { title: '演示工单提案', description: '历史里的旧提案不能自动确认。' }, context_token: 'SOURCE_SECRET' } }] : [])];
      const result = job.state === 'succeeded' ? { status: job.proposal ? 'pending_confirmation' : 'answered', answer: longAnswer,
        citations, sources, elapsed_ms: 2500, usage: { prompt_tokens: 300, completion_tokens: 100, total_tokens: 400 }, system_prompt: 'SYSTEM_SECRET', tool_arguments: { token: 'TOOL_SECRET' } } : null;
      return reply({ job_id: job.id, kind: job.kind, status: job.state, result,
        failure_type: job.state === 'failed' ? 'tool_budget' : null, progress: job.state === 'running' ? { stage: 'tool', label: '正在查询工单' } : null });
    }
    if (p.endsWith('/events')) return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: 'id: 1\nevent: progress\ndata: {"stage":"tool","label":"正在查询工单"}\n\nid: 2\nevent: delta\ndata: {"text":"UNVALIDATED_DRAFT"}\n\n' });
    if (/^\/api\/actions\/[\da-f-]+$/.test(p) && req.method() === 'GET') return reply(f.actions.get(p.split('/').at(-1)));
    f.unexpected.push(`${req.method()} ${p}`); return reply({ detail: 'unexpected mock request' }, 599);
  });
  const page = await historyPage(context, f);
  return { context, page, f };
}
async function historyPage(context, f) {
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror', error => f.errors.push(error.message));
  page.on('dialog', async dialog => {
    f.dialogs.push({ type: dialog.type(), message: dialog.message() });
    const decision = f.nextDialog; f.nextDialog = null;
    if (decision === false) await dialog.dismiss(); else await dialog.accept(typeof decision === 'string' ? decision : undefined);
  });
  await page.goto('https://history-preview.invalid/', { waitUntil: 'networkidle' });
  await page.locator('#app-shell').waitFor({ state: 'visible' });
  if (qaFont) await page.addStyleTag({ content: '@font-face{font-family:"Microsoft YaHei";src:url("/__qa_font")} @font-face{font-family:"Noto Serif SC";src:url("/__qa_font")}' });
  if (qaSerif) await page.addStyleTag({ content: '@font-face{font-family:SimSun;src:url("/__qa_serif")} :root{--font-display:SimSun,serif} .markdown-answer code{font-family:ui-monospace,SimSun,monospace}' });
  await page.evaluate(() => document.fonts.ready);
  return page;
}
const posts = f => f.calls.filter(c => c.method === 'POST' && c.path === '/api/jobs');
async function send(page, question, mode = 'rag') {
  const prior = await page.locator('.markdown-answer h2').filter({ hasText: '排查结论' }).count();
  await page.locator('#chat-mode').selectOption(mode);
  await page.locator('#question-input').fill(question); await page.locator('#send-button').click();
  await page.waitForFunction(prior => [...document.querySelectorAll('.markdown-answer h2')].filter(el => el.textContent.includes('排查结论')).length > prior, prior);
  await page.locator('#send-button').waitFor({ state: 'visible' });
}
async function openHistory(page) {
  if (!await page.locator('#history-dialog').evaluate(el => el.open)) await page.locator('#history-toggle').click();
  await page.locator('#history-dialog').waitFor({ state: 'visible' });
}
async function openConversation(page, title) {
  await openHistory(page); await page.locator('.history-open').filter({ hasText: title }).click();
  await page.locator('#history-dialog').waitFor({ state: 'hidden' });
}
async function safe(f) { assert.deepEqual(f.unexpected, []); assert.deepEqual(f.errors, []); }
async function storedData(page) {
  return page.evaluate(async () => {
    const names = await indexedDB.databases(), records = [];
    for (const { name } of names) {
      const database = await new Promise((resolve, reject) => { const request = indexedDB.open(name); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
      for (const store of database.objectStoreNames) {
        const values = await new Promise((resolve, reject) => { const request = database.transaction(store, 'readonly').objectStore(store).getAll(); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); });
        records.push(...values);
      }
      database.close();
    }
    return JSON.stringify(records);
  });
}
async function waitForStored(page, text, present = true) {
  for (let i = 0; i < 40; i += 1) {
    if ((await storedData(page)).includes(text) === present) return;
    await page.waitForTimeout(100);
  }
  assert.fail(`IndexedDB ${present ? 'did not save' : 'did not clear'} expected display data`);
}
async function assertLayout(page) {
  const geometry = await page.evaluate(() => ({ w: innerWidth, h: innerHeight, bodyW: document.body.scrollWidth, htmlW: document.documentElement.scrollWidth, bodyH: document.body.scrollHeight }));
  assert.ok(geometry.bodyW <= geometry.w && geometry.htmlW <= geometry.w && geometry.bodyH <= geometry.h + 1, JSON.stringify(geometry));
  const composer = await page.locator('#composer-form').boundingBox();
  assert.ok(composer.y >= 0 && composer.y + composer.height <= geometry.h + 1);
  assert.ok((await page.locator('.workspace-header').boundingBox()).height <= 72);
}

for (const action of ['disable', 'clear', 'delete', 'rename']) {
  test(`two tabs reject a stale snapshot after another tab performs ${action}`, async () => {
    const { context, page, f } = await fixture();
    try {
      await send(page, '跨页已保存会话');
      await openHistory(page); await page.locator('#history-persist').check();
      await waitForStored(page, '跨页已保存会话');
      const stale = await historyPage(context, f);
      await openHistory(stale);
      assert.equal(await stale.locator('.history-row').count(), 1);
      assert.equal(await stale.locator('#history-persist').isChecked(), true);
      if (action === 'disable') await page.locator('#history-persist').uncheck();
      else if (action === 'clear') await page.locator('#history-clear').click();
      else if (action === 'delete') await page.getByRole('button', { name: '删除对话', exact: true }).click();
      else {
        f.nextDialog = '其他标签页的新标题';
        await page.getByRole('button', { name: '重命名', exact: true }).click();
      }
      await waitForStored(page, action === 'rename' ? '其他标签页的新标题' : '跨页已保存会话', action === 'rename');
      // This page still holds the old full snapshot. Its local rename must not
      // re-enable saving, restore a deleted row or overwrite the newer title.
      f.nextDialog = '过期标签页试图覆盖';
      await stale.getByRole('button', { name: '重命名', exact: true }).click();
      await stale.waitForFunction(() => /其他标签页.*刷新/.test(document.querySelector('#history-status').textContent));
      const persisted = JSON.parse(await storedData(page));
      const rows = persisted.filter(row => row.messages);
      assert.equal(rows.length, action === 'rename' ? 1 : 0);
      if (action === 'rename') assert.equal(rows[0].title, '其他标签页的新标题');
      assert.equal(persisted.find(row => row.owner === USER && 'enabled' in row).enabled, action === 'rename' || action === 'delete');
      assert.equal(await stale.locator('#history-persist').isDisabled(), true);
      assert.match(await stale.locator('.history-title').textContent(), /过期标签页/);
      await stale.reload({ waitUntil: 'networkidle' }); await openHistory(stale);
      assert.equal(await stale.locator('.history-row').count(), action === 'rename' ? 1 : 0);
      assert.equal(await stale.locator('#history-persist').isChecked(), action === 'rename' || action === 'delete');
      assert.equal(posts(f).length, 1); await safe(f);
    } finally { await context.close(); }
  });
}

test('two tabs retain the first new conversation and reject a conflicting new snapshot', async () => {
  const { context, page, f } = await fixture();
  try {
    await openHistory(page); await page.locator('#history-persist').check();
    await page.waitForFunction(() => /已开启本浏览器保存/.test(document.querySelector('#history-status').textContent));
    const stale = await historyPage(context, f);
    await page.keyboard.press('Escape');
    await send(page, '第一个标签页新增'); await waitForStored(page, '第一个标签页新增');
    await send(stale, '第二个标签页新增'); await openHistory(stale);
    await stale.waitForFunction(() => /其他标签页.*刷新/.test(document.querySelector('#history-status').textContent));
    const stored = await storedData(page);
    assert.match(stored, /第一个标签页新增/); assert.doesNotMatch(stored, /第二个标签页新增/);
    assert.match(await stale.locator('.history-title').textContent(), /第二个标签页新增/);
    assert.equal(posts(f).length, 2); await safe(f);
  } finally { await context.close(); }
});

test('simultaneous tab transactions accept exactly one writer for a revision', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '事务竞争会话'); await openHistory(page);
    await page.locator('#history-persist').check(); await waitForStored(page, '事务竞争会话');
    const other = await historyPage(context, f);
    const read = async tab => tab.evaluate(async owner => {
      const { HistoryStore } = await import('/ui-assets/conversations.js');
      const loaded = await new HistoryStore().load(owner);
      return loaded;
    }, USER);
    const [first, second] = await Promise.all([read(page), read(other)]);
    assert.equal(first.revision, second.revision, 'reading another tab does not invalidate an unchanged snapshot');
    const write = (tab, snapshot, title) => tab.evaluate(async ({ owner, snapshot, title }) => {
      const { HistoryStore } = await import('/ui-assets/conversations.js');
      snapshot.conversations[0].title = title;
      try {
        const result = await new HistoryStore().save(owner, snapshot.conversations, snapshot.revision);
        return { revision: result.revision, title };
      } catch (error) { return { error: error.name }; }
    }, { owner: USER, snapshot, title });
    const results = await Promise.all([write(page, first, '事务甲成功'), write(other, second, '事务乙成功')]);
    assert.equal(results.filter(result => result.error === 'HistoryConflictError').length, 1);
    const winner = results.find(result => result.revision);
    assert.equal(winner.revision, first.revision + 1);
    const loaded = await read(page);
    assert.equal(loaded.conversations[0].title, winner.title);
    assert.equal(loaded.revision, winner.revision); await safe(f);
  } finally { await context.close(); }
});

test('stale explicit preferences cannot clear a newer tab snapshot', async () => {
  const { context, page, f } = await fixture();
  try {
    await openHistory(page); await page.locator('#history-persist').check();
    await page.waitForFunction(() => /已开启本浏览器保存/.test(document.querySelector('#history-status').textContent));
    const stale = await historyPage(context, f); await openHistory(stale);
    await page.keyboard.press('Escape'); await send(page, '更新后的会话必须保留');
    await waitForStored(page, '更新后的会话必须保留');
    await stale.locator('#history-clear').click();
    await stale.waitForFunction(() => /其他标签页.*刷新/.test(document.querySelector('#history-status').textContent));
    assert.match(await storedData(page), /更新后的会话必须保留/);
    assert.equal(JSON.parse(await storedData(page)).find(row => row.owner === USER && 'enabled' in row).enabled, true);
    await safe(f);
  } finally { await context.close(); }
});

test('v1 IndexedDB history upgrades intact, keeps account isolation and blocks old writers', async () => {
  const { context, page, f } = await fixture();
  try {
    await page.evaluate(async ({ owner, other }) => {
      const name = 'project-copilot-chat-history-v1';
      const requestResult = request => new Promise((resolve, reject) => {
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      await requestResult(indexedDB.deleteDatabase(name));
      const request = indexedDB.open(name, 1);
      request.onupgradeneeded = () => {
        request.result.createObjectStore('conversations', { keyPath: ['owner', 'id'] }).createIndex('owner', 'owner');
        request.result.createObjectStore('preferences', { keyPath: 'owner' });
      };
      const db = await requestResult(request), tx = db.transaction(['conversations', 'preferences'], 'readwrite');
      for (const [account, content] of [[owner, '旧版保留会话'], [other, '其他账号旧版内容']]) {
        tx.objectStore('preferences').put({ owner: account, enabled: true });
        tx.objectStore('conversations').put({ owner: account, id: crypto.randomUUID(), title: content,
          createdAt: Date.now(), updatedAt: Date.now(), mode: 'rag', messages: [{ role: 'user', content, mode: 'rag' }], jobs: [] });
      }
      await new Promise((resolve, reject) => { tx.oncomplete = resolve; tx.onabort = tx.onerror = reject; }); db.close();
    }, { owner: USER, other: OTHER });
    await page.reload({ waitUntil: 'networkidle' }); await openHistory(page);
    assert.equal(await page.locator('.history-title').textContent(), '旧版保留会话');
    assert.equal(await page.locator('#history-persist').isChecked(), true);
    const stored = JSON.parse(await storedData(page));
    assert.equal(stored.filter(row => row.messages).length, 2);
    assert.equal(stored.find(row => row.owner === USER && 'enabled' in row).revision, 0);
    const legacyError = await page.evaluate(() => new Promise(resolve => {
      const request = indexedDB.open('project-copilot-chat-history-v1', 1);
      request.onerror = () => resolve(request.error.name);
      request.onsuccess = () => { request.result.close(); resolve('unexpected success'); };
    }));
    assert.equal(legacyError, 'VersionError');
    await page.locator('#history-clear').click(); await waitForStored(page, '旧版保留会话', false);
    const afterClear = JSON.parse(await storedData(page));
    assert.equal(afterClear.find(row => row.owner === USER && 'enabled' in row).revision, 1);
    assert.equal(afterClear.filter(row => row.messages).length, 1);
    assert.match(JSON.stringify(afterClear), /其他账号旧版内容/); await safe(f);
  } finally { await context.close(); }
});

test('automatic saves cannot opt in or revive history even with the current disabled revision', async () => {
  const { context, page, f } = await fixture();
  try {
    const result = await page.evaluate(async owner => {
      const { HistoryStore } = await import('/ui-assets/conversations.js');
      const store = new HistoryStore(), loaded = await store.load(owner);
      try { await store.save(owner, [], loaded.revision); return 'unexpected success'; }
      catch (error) { return error.name; }
    }, USER);
    assert.equal(result, 'HistoryConflictError');
    assert.equal(JSON.parse(await storedData(page)).find(row => row.owner === USER && 'enabled' in row).enabled, false);
    await safe(f);
  } finally { await context.close(); }
});

test('new chat archives content and switching restores safe Markdown/citations without any POST', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '第一场研发排查的问题');
    await page.locator('#new-chat').click();
    assert.equal(await page.locator('#chat-welcome').isVisible(), true);
    await send(page, '第二场研发排查的问题');
    await openConversation(page, '第一场研发排查的问题');
    assert.equal(await page.locator('.message.user').textContent().then(s => s.includes('第一场')), true);
    assert.equal(await page.locator('.message.user').textContent().then(s => s.includes('第二场')), false);
    for (const selector of ['h2', 'ul', 'blockquote', 'table', 'pre']) assert.ok(await page.locator(`.markdown-answer ${selector}`).count());
    assert.equal(await page.locator('.markdown-answer img,.markdown-answer script').count(), 0);
    await page.getByRole('button', { name: /查看.*条依据/ }).click();
    assert.match(await page.locator('#evidence-content').textContent(), /历史安全说明/);
    assert.equal(posts(f).length, 2);
    await safe(f);
  } finally { await context.close(); }
});

test('empty chats are excluded; titles support search, rename and local deletion', async () => {
  const { context, page, f } = await fixture();
  try {
    await page.locator('#new-chat').click(); await page.locator('#new-chat').click();
    await openHistory(page); assert.equal(await page.locator('.history-row').count(), 0); await page.keyboard.press('Escape');
    await send(page, '竹青研发问题：这一段超过二十个字用于核对自动标题截断');
    await page.locator('#new-chat').click(); await openHistory(page);
    assert.equal(await page.locator('.history-row').count(), 1);
    assert.equal(await page.locator('.history-title').textContent(), Array.from('竹青研发问题：这一段超过二十个字用于核对自动标题截断').slice(0, 20).join(''));
    f.nextDialog = '已重命名的研发会话'; await page.getByRole('button', { name: /重命名/ }).click();
    await page.locator('#history-search').fill('不存在'); assert.equal(await page.locator('.history-row').count(), 0);
    await page.locator('#history-search').fill('已重命名'); assert.equal(await page.locator('.history-row').count(), 1);
    await page.getByRole('button', { name: /删除对话/ }).click(); assert.equal(await page.locator('.history-row').count(), 0);
    assert.equal(posts(f).length, 1); assert.equal(f.calls.some(c => c.method === 'DELETE'), false); await safe(f);
  } finally { await context.close(); }
});

test('browser persistence is opt-in and stores only whitelisted final display data in IndexedDB', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '此条普通正文应保存在 IndexedDB'); await page.locator('#new-chat').click();
    assert.doesNotMatch(await storedData(page), /此条普通正文/);
    await openHistory(page); await page.locator('#history-persist').check();
    await waitForStored(page, '此条普通正文');
    await page.keyboard.press('Escape'); await page.reload({ waitUntil: 'networkidle' });
    await openHistory(page); await page.locator('.history-open').filter({ hasText: '此条普通正文' }).waitFor();
    assert.equal(await page.locator('#history-persist').isChecked(), true);
    const stored = await storedData(page);
    assert.match(stored, /此条普通正文/);
    assert.doesNotMatch(stored, /CONTEXT_SECRET|SOURCE_SECRET|SYSTEM_SECRET|TOOL_SECRET|context_token|csrf_token|payload_fingerprint|tool_arguments|system_prompt/);
    const local = await page.evaluate(() => JSON.stringify(Object.fromEntries(Object.keys(localStorage).map(key => [key, localStorage.getItem(key)]))));
    assert.doesNotMatch(local, /此条普通正文|排查结论|CONTEXT_SECRET/);
    await page.locator('.history-open').filter({ hasText: '此条普通正文' }).click();
    assert.equal(posts(f).length, 1); await safe(f);
  } finally { await context.close(); }
});

test('disabling browser save clears only disk while leaving same-page history usable', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '关闭保存前的会话'); await page.locator('#new-chat').click(); await openHistory(page);
    await page.locator('#history-persist').check(); await waitForStored(page, '关闭保存前');
    await page.locator('#history-persist').uncheck(); await waitForStored(page, '关闭保存前', false);
    assert.equal(await page.locator('.history-row').count(), 1);
    await page.keyboard.press('Escape'); await page.reload({ waitUntil: 'networkidle' }); await openHistory(page);
    assert.equal(await page.locator('.history-row').count(), 0); assert.equal(await page.locator('#history-persist').isChecked(), false);
    assert.equal(posts(f).length, 1); await safe(f);
  } finally { await context.close(); }
});

test('IndexedDB denial falls back to memory without breaking chat or creating work twice', async () => {
  const { context, page, f } = await fixture({ unavailable: true });
  try {
    await send(page, '浏览器存储不可用时的会话'); await page.locator('#new-chat').click(); await openHistory(page);
    if (await page.locator('#history-persist').isEnabled()) await page.locator('#history-persist').click();
    await page.locator('#history-status').getByText(/内存|不可用|无法/).waitFor();
    await page.keyboard.press('Escape'); await openConversation(page, '浏览器存储不可用');
    assert.match(await page.locator('#chat-messages').textContent(), /浏览器存储不可用时的会话/);
    assert.equal(posts(f).length, 1); await safe(f);
  } finally { await context.close(); }
});

test('an unfinished RAG draft is never persisted as a final historical answer', async () => {
  const { context, page, f } = await fixture();
  try {
    await openHistory(page); await page.locator('#history-persist').check();
    // Native confirm acceptance and the drawer close are distinct interactions.
    await page.locator('#history-close').click();
    await page.locator('#history-dialog').waitFor({ state: 'hidden' });
    f.currentState = 'running'; await page.locator('#chat-mode').selectOption('rag');
    await page.locator('#question-input').fill('只保存用户问题，不保存未校验草稿'); await page.locator('#send-button').click();
    await page.getByText('UNVALIDATED_DRAFT', { exact: true }).waitFor();
    await page.locator('#new-chat').click(); await waitForStored(page, '只保存用户问题');
    assert.doesNotMatch(await storedData(page), /UNVALIDATED_DRAFT|CONTEXT_SECRET|context_token/);
    await page.reload({ waitUntil: 'networkidle' });
    assert.equal(posts(f).length, 1); assert.equal(await page.locator('.stream-preview').count(), 0);
    await safe(f);
  } finally { await context.close(); }
});

test('restored Agent conversations create a fresh context and do not mix other chat history', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '甲会话问题', 'agent'); await page.locator('#new-chat').click();
    await send(page, '乙会话问题', 'agent'); await openConversation(page, '甲会话问题');
    await send(page, '甲会话继续问题', 'agent');
    assert.equal(f.contexts, 3);
    const calls = posts(f);
    assert.notEqual(calls[0].headers['x-copilot-context-token'], calls[2].headers['x-copilot-context-token']);
    assert.match(JSON.stringify(calls[2].body.payload.history), /甲会话问题/);
    assert.doesNotMatch(JSON.stringify(calls[2].body.payload.history), /乙会话问题/);
    assert.equal(calls.length, 3); await safe(f);
  } finally { await context.close(); }
});

test('pending proposal switch warns, can be cancelled and becomes read-only after leaving', async () => {
  const { context, page, f } = await fixture({ proposal: true });
  try {
    await send(page, '请准备一份待确认工单', 'agent');
    await page.getByRole('button', { name: '确认创建工单' }).waitFor();
    f.nextDialog = false; await page.locator('#new-chat').click();
    assert.equal(await page.getByRole('button', { name: '确认创建工单' }).isVisible(), true);
    await page.locator('#new-chat').click();
    assert.ok(f.dialogs.some(d => /提案|上下文|确认/.test(d.message)));
    await openConversation(page, '请准备一份待确认工单');
    assert.equal(await page.getByRole('button', { name: '确认创建工单' }).count(), 0);
    assert.match(await page.locator('#chat-messages').textContent(), /历史|只读|重新/);
    assert.equal(f.calls.some(c => /\/(confirm|reject)$/.test(c.path)), false); assert.equal(posts(f).length, 1); await safe(f);
  } finally { await context.close(); }
});

test('an uncertain manual proposal response warns before switching without repeating the write', async () => {
  const { context, page, f } = await fixture();
  try {
    f.previewFailStatus = 502;
    await page.locator('[data-view="tickets"]').click(); await page.locator('#new-ticket').click();
    await page.locator('#proposal-title').fill('提交结果尚不确定的工单提案');
    await page.locator('#proposal-description').fill('仅供离线错误响应测试，不访问真实工单。');
    await page.locator('#proposal-submit').click();
    await page.locator('#proposal-dialog [role="alert"]').waitFor();
    await page.getByRole('button', { name: '关闭新建工单' }).click();
    f.nextDialog = false; await page.locator('#new-chat').click();
    assert.ok(f.dialogs.some(dialog => /不确定|未核实|待确认|未确认/.test(dialog.message)));
    assert.equal(await page.locator('#view-tickets').isVisible(), true, 'declining leaves the current workspace and authority intact');
    assert.equal(f.contexts, 1);
    assert.equal(f.calls.filter(call => call.path === '/api/actions/preview' && call.method === 'POST').length, 1);
    assert.equal(posts(f).length, 0); assert.equal(f.calls.some(call => /\/(confirm|reject)$/.test(call.path)), false);
    await safe(f);
  } finally { await context.close(); }
});

test('switching away from a running job stops waiting only and retains read-only recovery', async () => {
  const { context, page, f } = await fixture();
  try {
    f.currentState = 'running';
    await page.locator('#question-input').fill('运行中的任务问题'); await page.locator('#send-button').click();
    await page.locator('.job-progress').getByText(/查询工单/).waitFor();
    const jobId = [...f.jobs.keys()][0];
    await page.locator('#new-chat').click();
    assert.equal(await page.locator('#chat-welcome').isVisible(), true);
    await openHistory(page); assert.match(await page.locator('#history-list').textContent(), /运行中的任务/); await page.keyboard.press('Escape');
    await page.locator('#task-center-toggle').click(); assert.match(await page.locator('#job-recovery').textContent(), new RegExp(jobId.slice(0, 8)));
    f.jobs.get(jobId).state = 'succeeded'; await page.getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.markdown-answer h2').getByText('排查结论').waitFor();
    assert.equal(posts(f).length, 1); assert.equal(f.calls.some(c => c.method === 'DELETE' || /cancel/.test(c.path)), false); await safe(f);
  } finally { await context.close(); }
});

test('stopping before the submission response cannot discard its unknown job identifier or send twice', async () => {
  const { context, page, f } = await fixture();
  try {
    f.currentState = 'running'; f.submitGate = new Promise(resolve => { f.releaseSubmit = resolve; });
    await page.locator('#chat-mode').selectOption('rag');
    await page.locator('#question-input').fill('提交响应仍在等待时保留原请求'); await page.locator('#send-button').click();
    for (let i = 0; i < 40 && posts(f).length === 0; i += 1) await page.waitForTimeout(50);
    assert.equal(posts(f).length, 1);
    await page.locator('#stop-wait').click();
    assert.equal(await page.locator('#new-chat').isEnabled(), false);
    assert.match(await page.locator('#chat-messages').textContent(), /提交响应仍在等待/);
    await page.locator('#account-panel > summary').click(); await page.locator('#logout-button').click();
    assert.equal(await page.locator('#app-shell').isVisible(), true, 'unknown submission cannot lose its account before job ID arrives');
    assert.equal(f.calls.some(call => call.path === '/api/auth/logout'), false);
    await page.locator('#account-panel > summary').click();
    f.releaseSubmit();
    await page.waitForFunction(() => document.getElementById('task-center-count').textContent === '1');
    await page.locator('#new-chat').click();
    await page.locator('#task-center-toggle').click();
    assert.match(await page.locator('#job-recovery').textContent(), new RegExp([...f.jobs.keys()][0].slice(0, 8)));
    assert.equal(posts(f).length, 1); assert.equal(f.calls.some(c => c.method === 'DELETE'), false); await safe(f);
  } finally { f.releaseSubmit?.(); await context.close(); }
});

test('recovering a known job returns to its original conversation without polluting another chat', async () => {
  const { context, page, f } = await fixture();
  try {
    f.currentState = 'running';
    await page.locator('#question-input').fill('甲运行任务问题'); await page.locator('#send-button').click();
    await page.locator('.job-progress').getByText(/查询工单/).waitFor();
    const originalId = [...f.jobs.keys()][0];
    await page.locator('#new-chat').click();
    f.currentState = 'succeeded'; await send(page, '乙正常会话问题', 'agent');
    f.jobs.get(originalId).state = 'succeeded';
    await page.locator('#task-center-toggle').click();
    await page.locator('.job-recovery-row').filter({ hasText: originalId.slice(0, 8) }).getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.message.user').filter({ hasText: '甲运行任务问题' }).waitFor();
    await page.locator('.markdown-answer h2').getByText('排查结论').waitFor();
    await page.locator('#send-button').waitFor({ state: 'visible' });
    assert.doesNotMatch(await page.locator('#chat-messages').textContent(), /乙正常会话问题/);
    assert.equal(posts(f).length, 2); assert.equal(f.contexts, 2, 'GET recovery does not create an execution context');
    await send(page, '甲恢复之后继续', 'agent');
    assert.equal(f.contexts, 3);
    const history = JSON.stringify(posts(f)[2].body.payload.history);
    assert.match(history, /甲运行任务问题/); assert.doesNotMatch(history, /乙正常会话问题/);
    await openConversation(page, '乙正常会话问题');
    assert.doesNotMatch(await page.locator('#chat-messages').textContent(), /甲恢复之后继续/);
    assert.equal(posts(f).length, 3); await safe(f);
  } finally { await context.close(); }
});

test('account authentication gates persisted history and clear removes only the signed-in owner data', async () => {
  const { context, page, f } = await fixture();
  try {
    await send(page, '原账号私人会话'); await page.locator('#new-chat').click(); await openHistory(page); await page.locator('#history-persist').check(); await waitForStored(page, '原账号私人');
    await page.locator('#history-search').fill('原账号私人'); await page.keyboard.press('Escape');
    await page.locator('#account-panel > summary').click(); await page.locator('#logout-button').click();
    await page.locator('#auth-screen').waitFor({ state: 'visible' });
    assert.doesNotMatch(await page.locator('#chat-messages').textContent(), /原账号私人/);
    assert.doesNotMatch(await page.locator('#history-list').textContent(), /原账号私人/);
    assert.equal(await page.locator('#history-search').inputValue(), '');
    await page.locator('#login-username').fill('other-user'); await page.locator('#login-password').fill('mock-password'); await page.locator('#login-submit').click();
    await page.locator('#app-shell').waitFor({ state: 'visible' }); await openHistory(page);
    assert.doesNotMatch(await page.locator('#history-list').textContent(), /原账号私人/); assert.equal(await page.locator('#history-persist').isChecked(), false);
    await page.keyboard.press('Escape'); await send(page, '另一个账号独立保存的对话'); await page.locator('#new-chat').click();
    await openHistory(page); await page.locator('#history-persist').check(); await waitForStored(page, '另一个账号独立');
    await page.keyboard.press('Escape'); f.userId = USER; await page.reload({ waitUntil: 'networkidle' }); await openHistory(page);
    await page.locator('.history-open').filter({ hasText: '原账号私人' }).waitFor();
    assert.doesNotMatch(await page.locator('#history-list').textContent(), /另一个账号独立/);
    await page.locator('#history-clear').click(); await waitForStored(page, '原账号私人', false);
    assert.match(await storedData(page), /另一个账号独立/);
    assert.equal(posts(f).length, 2); await safe(f);
  } finally { await context.close(); }
});

test('failed and unknown indexes require GET proof and explicit UUID confirmation before local deletion', async () => {
  const records = [{ id: LEGACY, kind: 'agent', createdAt: Date.now() }, { id: OTHER_JOB, kind: 'agent', createdAt: Date.now() - 1000 }];
  const { context, page, f } = await fixture({ records });
  try {
    f.jobs.set(LEGACY, { id: LEGACY, kind: 'agent', state: 'unknown' }); f.jobs.set(OTHER_JOB, { id: OTHER_JOB, kind: 'agent', state: 'failed' });
    await page.locator('#task-center-toggle').click();
    const first = page.locator('.job-recovery-row').filter({ hasText: LEGACY.slice(0, 8) });
    await first.getByRole('button', { name: '删除本地记录' }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('#job-recovery .job-state[data-state]').length === 2);
    await page.screenshot({ path: '/tmp/pc-history-task-delete.png', fullPage: true });
    f.nextDialog = false; await first.getByRole('button', { name: '删除本地记录' }).click(); assert.equal(await page.locator('#task-center-count').textContent(), '2');
    await first.getByRole('button', { name: '删除本地记录' }).click();
    await page.waitForFunction(() => document.getElementById('task-center-count').textContent === '1');
    assert.ok(f.dialogs.some(d => d.message.includes(LEGACY) && /本地|浏览器/.test(d.message)));
    const second = page.locator('.job-recovery-row').filter({ hasText: OTHER_JOB.slice(0, 8) });
    await second.getByRole('button', { name: '删除本地记录' }).click();
    await page.waitForFunction(() => document.getElementById('task-center-count').textContent === '0');
    assert.equal(f.calls.some(c => c.method === 'POST' || c.method === 'DELETE'), false); await safe(f);
  } finally { await context.close(); }
});

test('reserved and running task identifiers have no ordinary delete action', async () => {
  const records = [{ id: LEGACY, kind: 'agent', createdAt: Date.now() }, { id: OTHER_JOB, kind: 'rag', createdAt: Date.now() - 1000 }];
  const { context, page, f } = await fixture({ records });
  try {
    f.jobs.set(LEGACY, { id: LEGACY, kind: 'agent', state: 'running' }); f.jobs.set(OTHER_JOB, { id: OTHER_JOB, kind: 'rag', state: 'reserved' });
    await page.locator('#task-center-toggle').click();
    await page.waitForFunction(() => document.querySelectorAll('#job-recovery .job-state[data-state]').length === 2);
    assert.equal(await page.getByRole('button', { name: '删除本地记录' }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '隐藏此记录' }).count(), 0);
    assert.equal(await page.locator('#task-center-count').textContent(), '2'); assert.equal(f.calls.some(c => c.method !== 'GET'), false); await safe(f);
  } finally { await context.close(); }
});

for (const width of [1440, 1280, 390]) {
  test(`history and task drawers preserve compact ink layout and avatars at ${width}px`, async () => {
    const { context, page, f } = await fixture({ width, proposal: true });
    try {
      await send(page, '检索超时排查与工单建议', 'agent');
      await page.locator('.proposal-card').scrollIntoViewIfNeeded(); await assertLayout(page);
      await page.screenshot({ path: `/tmp/pc-history-${width}-proposal.png`, fullPage: true });
      await page.locator('#new-chat').click(); await send(page, '第二场知识问答'); await openHistory(page);
      assert.equal(await page.locator('.history-row').count(), 2);
      await page.screenshot({ path: `/tmp/pc-history-${width}-list.png`, fullPage: true });
      for (let i = 0; i < 12; i += 1) { await page.keyboard.press('Tab'); assert.equal(await page.locator('#history-dialog').evaluate(el => el.contains(document.activeElement)), true); }
      await page.keyboard.press('Escape'); assert.equal(await page.locator('#history-toggle').evaluate(el => el === document.activeElement), true);
      await openConversation(page, '检索超时排查与工单建议');
      assert.equal(await page.getByRole('button', { name: '确认创建工单' }).count(), 0);
      await page.locator('#chat-messages').evaluate(el => { el.scrollTop = 0; }); await assertLayout(page);
      assert.ok(await page.locator('.message.user .message-avatar img').evaluate(el => el.complete && el.naturalWidth > 0));
      assert.equal(await page.locator('.assistant-seal').textContent(), '知');
      await page.screenshot({ path: `/tmp/pc-history-${width}-restored.png`, fullPage: true });
      assert.equal(posts(f).length, 2); await safe(f);
    } finally { await context.close(); }
  });
}
