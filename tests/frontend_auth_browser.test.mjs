/** Offline public-build UI smoke: every URL is intercepted; no real network. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = new URL('../src/project_copilot/web/', import.meta.url);
const files = new Map();
for (const [url, file, type] of [
  ['/', 'index.html', 'text/html'], ['/ui-assets/app.js', 'assets/app.js', 'text/javascript'],
  ['/ui-assets/markdown.js', 'assets/markdown.js', 'text/javascript'], ['/ui-assets/styles.css', 'assets/styles.css', 'text/css'], ['/ui-assets/mark.svg', 'assets/mark.svg', 'image/svg+xml'],
  ['/ui-assets/ink-mountains.svg', 'assets/ink-mountains.svg', 'image/svg+xml'], ['/ui-assets/bamboo.svg', 'assets/bamboo.svg', 'image/svg+xml'],
]) files.set(url, { body: await readFile(new URL(file, base)), type });
const source = await readFile(new URL('assets/api.js', base), 'utf8');
const marker = 'const API_PREFIX = ""; // EDGEONE_API_PREFIX';
assert.equal(source.split(marker).length, 2);
files.set('/ui-assets/api.js', { body: source.replace(marker, 'const API_PREFIX = "/api"; // EDGEONE_API_PREFIX'), type: 'text/javascript' });
const ID = '11111111-1111-4111-8111-111111111111';
const ACTION = '22222222-2222-4222-8222-222222222222';
const CSRF = 'a'.repeat(64);
const expires = () => new Date(Date.now() + 3600000).toISOString();
const user = (name = 'alice', role = 'user') => ({ id: ID, username: name, role });
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

async function fixture(options = {}) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });
  const f = { session: options.session || null, meStatus: options.meStatus, calls: [], unexpected: [], action: null,
    logoutFailure: options.logoutFailure || false, tickets401: false, progressMode: options.progressMode || false, jobDone: false, holdRag: null, releaseRag: null };
  await context.route('**/*', async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (url.origin !== 'https://fixture.invalid') { f.unexpected.push(url.origin); return route.abort(); }
    if (request.method() === 'GET' && files.has(path)) {
      const file = files.get(path); return route.fulfill({ status: 200, contentType: file.type, body: file.body });
    }
    if (path === '/favicon.ico') return route.fulfill({ status: 204 });
    const body = request.headers()['content-type']?.includes('application/json') ? request.postDataJSON() : null;
    f.calls.push({ path, method: request.method(), headers: request.headers(), body });
    const reply = (data, status = 200, headers = {}) => route.fulfill({ status, contentType: 'application/json', headers, body: JSON.stringify(data) });
    if (path === '/api/auth/me') {
      if (f.meStatus) return reply({ detail: 'mock offline' }, f.meStatus);
      return f.session ? reply({ user: f.session, csrf_token: CSRF }) : reply({ detail: 'invalid session' }, 401);
    }
    if (path === '/api/auth/login') {
      if (body.password !== 'example-password') return reply({ detail: 'bad credentials' }, 401);
      f.session = user(body.username, body.username === 'admin' ? 'admin' : 'user');
      return reply({ user: f.session, csrf_token: CSRF }, 200, { 'Set-Cookie': '__Host-pc_session=example; Path=/; Secure; HttpOnly; SameSite=Lax' });
    }
    if (path === '/api/auth/register') return reply({ ...user(body.username), role: 'user' }, 201);
    if (path === '/api/auth/logout') {
      if (f.logoutFailure) return route.abort('failed');
      f.session = null; return reply({ status: 'ok' });
    }
    if (path === '/api/health') return reply({ status: 'ok' });
    if (path === '/api/ui/config') return reply({ upload_max_bytes: 4194304 });
    if (path === '/api/documents' && request.method() === 'GET') return reply({ documents: f.deleted ? [] : [{ document_id: ID, source_name: 'temporary.md', chunk_count: 1 }] });
    if (path === `/api/documents/${ID}` && request.method() === 'DELETE') { f.deleted = true; return reply({ status: 'deleted', document_id: ID }); }
    if (path === '/api/documents/upload') return reply({ status: 'indexed', mode: 'HYBRID_RERANK',
      document: { document_id: ID, source_name: 'temporary.md', chunk_ids: [ID] }, chunk_count: 1 });
    if (path === '/api/tickets') return f.tickets401 ? reply({ detail: 'expired' }, 401) : reply({ items: [], limit: 10, offset: 0 });
    if (path === '/api/agent/contexts') return reply({ context_id: ID, context_token: 'c'.repeat(43), expires_at: expires() }, 201);
    if (path === '/api/actions/preview') {
      f.action = { action_id: ACTION, context_id: ID, status: 'pending', payload: body,
        payload_fingerprint: 'b'.repeat(64), created_at: new Date().toISOString(), expires_at: expires(), ticket_id: null };
      return reply(f.action, 201);
    }
    if (path === `/api/actions/${ACTION}`) return reply(f.action);
    if (path === '/api/jobs/upload' && request.method() === 'POST') { f.jobKind = 'upload'; return reply({ job_id: ID, status: 'reserved' }, 202); }
    if (path === '/api/jobs' && request.method() === 'POST') { f.jobKind = body.kind; return reply({ job_id: ID, status: 'reserved' }, 202); }
    if (path === `/api/jobs/${ID}/events` && f.progressMode) return route.fulfill({ status: 200,
      contentType: 'text/event-stream', body: 'id: 1\nevent: progress\ndata: {"stage":"search","label":"正在检索知识库"}\n\nid: 2\nevent: delta\ndata: {"text":"尚未验证的真实事件草稿"}\n\n' });
    if (path === `/api/jobs/${ID}` && request.method() === 'GET') {
      if (f.progressMode) return reply({ job_id: ID, status: f.jobDone ? 'succeeded' : 'running', result: f.jobDone ? {
        status: 'answered', answer: '# 最终回答\n\n**核对完成**。 <img src=x onerror=alert(1)>\n\n| 项目 | 结果 |\n| --- | --- |\n| 校验 | 通过 |', citations: [] } : null });

      if (f.jobKind === 'upload') return reply({ job_id: ID, status: 'succeeded', result: { status: 'indexed', mode: 'HYBRID_RERANK', document: { document_id: ID, source_name: 'temporary.md' }, chunk_count: 1 } });
      if (f.holdRag) await f.holdRag;
      return reply({ job_id: ID, status: 'succeeded', result: { question: 'test', status: 'answered', answer: 'private-old-account-answer', citations: [], evidence_count: 0 } });
    }
    f.unexpected.push(`${request.method()} ${path}`); return reply({ detail: 'unexpected' }, 599);
  });
  const page = await context.newPage();
  const errors = []; page.on('pageerror', (error) => errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.goto('https://fixture.invalid/', { waitUntil: 'networkidle' });
  return { context, page, f, errors };
}

async function login(page, name = 'alice') {
  await page.locator('#login-username').fill(name);
  await page.locator('#login-password').fill('example-password');
  await page.locator('#login-submit').click();
  await page.locator('#app-shell').waitFor({ state: 'visible' });
}

test('login, ordinary-user upload restriction, proposal warning and logout clear state', async () => {
  const { context, page, f, errors } = await fixture();
  try {
    await page.locator('#login-form').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#app-shell').isVisible(), false);
    await login(page);
    assert.equal(await page.locator('#login-password').inputValue(), '');
    assert.equal(await page.locator('#account-name').textContent(), 'alice');
    assert.equal(await page.locator('#api-docs-link').isVisible(), false);
    await page.locator('[data-view="documents"]').click();
    assert.equal(await page.locator('#document-admin-controls').isVisible(), false);
    assert.equal(await page.locator('#upload-public-info').isVisible(), true);
    await page.locator('[data-view="tickets"]').click();
    await page.locator('#new-ticket').click();
    await page.locator('#proposal-title').fill('temporary proposal');
    await page.locator('#proposal-description').fill('private text');
    await page.locator('#proposal-submit').click();
    await page.locator('.proposal-card').getByRole('button', { name: '确认创建工单' }).waitFor();
    const preview = f.calls.find((call) => call.path === '/api/actions/preview');
    assert.equal(preview.headers['x-csrf-token'], CSRF);
    let warning = '';
    page.once('dialog', async (dialog) => { warning = dialog.message(); await dialog.dismiss(); });
    await page.locator('#logout-button').click();
    assert.match(warning, /提案/);
    assert.equal(f.calls.filter((call) => call.path === '/api/auth/logout').length, 0);
    page.once('dialog', (dialog) => dialog.accept());
    await page.locator('#logout-button').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    const logout = f.calls.filter((call) => call.path === '/api/auth/logout');
    assert.equal(logout.length, 1); assert.equal(logout[0].headers['x-csrf-token'], CSRF);
    assert.equal(f.calls.some((call) => /\/(confirm|reject)$/.test(call.path)), false);
    assert.equal(await page.locator('#chat-messages').textContent().then((text) => text.includes('temporary proposal')), false);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('administrator can upload through jobs, list and delete documents', async () => {
  const { context, page, f, errors } = await fixture({ session: user('admin', 'admin') });
  try {
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    await page.locator('[data-view="documents"]').click();
    assert.equal(await page.locator('#document-admin-controls').isVisible(), true);
    await page.locator('#file-input').setInputFiles({ name: 'temporary.md', mimeType: 'text/markdown', buffer: Buffer.from('synthetic text') });
    await page.locator('#upload-button').click();
    await page.getByText('资料已入库，现在可以在智能协作中提问。').waitFor();
    assert.equal(f.calls.find((call) => call.path === '/api/jobs/upload').headers['x-csrf-token'], CSRF);
    assert.equal(f.calls.some((call) => call.path === '/api/documents/upload'), false);
    await page.locator('#public-documents-list').getByText('temporary.md').waitFor();
    page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: '删除资料' }).click();
    await page.getByText('知识库暂无资料。').waitFor();
    assert.equal(f.calls.find((call) => call.path === `/api/documents/${ID}`).headers['x-csrf-token'], CSRF);
    await page.locator('#logout-button').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    assert.doesNotMatch(await page.locator('#public-documents-list').textContent(), /temporary\.md/);
    assert.equal(await page.locator('#file-input').inputValue(), '');
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('registration clears password/invite and login failure is safe', async () => {
  const { context, page, f, errors } = await fixture();
  try {
    await page.locator('#auth-show-register').click();
    await page.locator('#register-username').fill('newuser');
    await page.locator('#register-password').fill('example-password');
    await page.locator('#register-invitation').fill('invitation');
    await page.locator('#register-submit').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#register-password').inputValue(), '');
    assert.equal(await page.locator('#register-invitation').inputValue(), '');
    assert.equal(await page.locator('#login-username').inputValue(), 'newuser');
    await page.locator('#login-password').fill('wrong');
    await page.locator('#login-submit').click();
    await page.getByText('账号、密码或会话无效，请检查后重试。').waitFor();
    assert.equal(await page.locator('#login-password').inputValue(), '');
    assert.equal(f.calls.filter((call) => call.path === '/api/auth/login').length, 1);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('offline auth and uncertain logout each make one request and show a safe message', async () => {
  const first = await fixture({ meStatus: 503 });
  try {
    await first.page.getByText('认证服务暂不可用，请检查网络或稍后刷新页面。').waitFor();
    assert.equal(first.f.calls.filter((call) => call.path === '/api/auth/me').length, 1);
    assert.equal(await first.page.locator('#app-shell').isVisible(), false);
    assert.deepEqual(first.f.unexpected, []); assert.deepEqual(first.errors, []);
  } finally { await first.context.close(); }
  const second = await fixture({ logoutFailure: true });
  try {
    await login(second.page);
    await second.page.locator('#logout-button').click();
    await second.page.getByText(/退出结果尚未确认/).waitFor();
    assert.equal(second.f.calls.filter((call) => call.path === '/api/auth/logout').length, 1);
    assert.equal(await second.page.locator('#app-shell').isVisible(), false);
    assert.deepEqual(second.f.unexpected, []); assert.deepEqual(second.errors, []);
  } finally { await second.context.close(); }
});

test('expired session cannot show a late answer from a previous account', async () => {
  const { context, page, f, errors } = await fixture({ session: user('alice') });
  try {
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    f.holdRag = new Promise((resolve) => { f.releaseRag = resolve; });
    await page.locator('#chat-mode').selectOption('rag');
    await page.locator('#question-input').fill('Question from previous account');
    await page.locator('#send-button').click();
    await page.waitForFunction(() => document.getElementById('question-input').disabled);
    f.tickets401 = true;
    await page.locator('[data-view="tickets"]').click();
    await page.locator('#login-form').waitFor({ state: 'visible' });
    f.tickets401 = false;
    await login(page, 'bob');
    f.releaseRag();
    await page.waitForTimeout(200);
    const text = await page.locator('#app-shell').textContent();
    assert.doesNotMatch(text, /private-old-account-answer|Question from previous account/);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []);
  } finally { f.releaseRag?.(); await context.close(); }
});


test('real event display, stop, refresh and resume preserve identifier without a second POST', async () => {
  const { context, page, f, errors } = await fixture({ session: user('alice'), progressMode: true });
  try {
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    await page.locator('#chat-mode').selectOption('rag');
    await page.locator('#question-input').fill('private-question-not-stored');
    await page.locator('#send-button').click();
    await page.getByText('尚未验证的真实事件草稿', { exact: true }).waitFor();
    assert.match(await page.locator('.job-progress').textContent(), /最终校验尚未完成/);
    const before = await page.evaluate(() => ({ ...localStorage }));
    assert.match(JSON.stringify(before), new RegExp(ID));
    assert.doesNotMatch(JSON.stringify(before), /private-question|真实事件草稿|context_token|csrf|password/);
    await page.locator('#stop-wait').click();
    await page.getByText(/已停止等待，后台任务可能继续执行/).waitFor();
    assert.equal(await page.locator('.stream-preview').count(), 0);
    assert.equal(await page.locator('#question-input').isEnabled(), true);
    assert.equal(f.calls.filter((c) => c.path === '/api/jobs' && c.method === 'POST').length, 1);
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('#job-recovery').waitFor({ state: 'visible' });
    f.jobDone = true;
    await page.getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.markdown-answer h2').getByText('最终回答').waitFor();
    assert.equal(await page.locator('.markdown-answer table').count(), 1);
    assert.equal(await page.locator('.markdown-answer img').count(), 0);
    assert.match(await page.locator('.markdown-answer').textContent(), /<img src=x/);
    assert.equal(f.calls.filter((c) => c.path === '/api/jobs' && c.method === 'POST').length, 1);
    assert.equal(await page.evaluate(() => localStorage.length), 0);
    assert.deepEqual(f.unexpected, []); assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
