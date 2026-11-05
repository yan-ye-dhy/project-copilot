/** Browser-only visual/security regression; every request is intercepted. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const qaFont = process.env.PLAYWRIGHT_TEST_CJK_FONT ? await readFile(process.env.PLAYWRIGHT_TEST_CJK_FONT) : null;
const qaSerifFont = process.env.PLAYWRIGHT_TEST_SERIF_FONT ? await readFile(process.env.PLAYWRIGHT_TEST_SERIF_FONT) : null;
const sourceRoot = new URL('../src/project_copilot/web/', import.meta.url);
const files = new Map();
for (const [url, path, type] of [
  ['/', 'index.html', 'text/html'], ['/ui-assets/app.js', 'assets/app.js', 'text/javascript'],
  ['/ui-assets/markdown.js', 'assets/markdown.js', 'text/javascript'], ['/ui-assets/styles.css', 'assets/styles.css', 'text/css'],
  ...['mark', 'ink-mountains', 'bamboo'].map((name) => [`/ui-assets/${name}.svg`, `assets/${name}.svg`, 'image/svg+xml']),
]) files.set(url, { body: await readFile(new URL(path, sourceRoot)), type });
const api = await readFile(new URL('assets/api.js', sourceRoot), 'utf8');
files.set('/ui-assets/api.js', { body: api.replace('const API_PREFIX = ""; // EDGEONE_API_PREFIX',
  'const API_PREFIX = "/api"; // EDGEONE_API_PREFIX'), type: 'text/javascript' });
const JOB = '11111111-1111-4111-8111-111111111111';
const CSRF = 'a'.repeat(64);
const answer = '# 知行建议\n\n根据资料与工单，先核查**请求预算**，再处理 *连接状态*。\n\n- 保留原任务编号\n- 核对真实工具结果\n\n1. 阅读原文\n2. 查看工单\n\n> 未核实的结果不可当作成功。\n\n| 项目 | 状态 |\n| --- | --- |\n| 任务 | 已核对 |\n\n```js\nconst veryLongValue = "' + '竹青'.repeat(140) + '";\n```\n\n' + 'long-unbroken-text-'.repeat(100) + '\n\n<img src=x onerror=alert(1)>';
const frame = (id, kind, data) => `id: ${id}\nevent: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

async function fixture({ width = 1440, submitStatus = 202, state = 'succeeded', streaming = false,
  personal = null, failureType = null } = {}) {
  const context = await browser.newContext({ viewport: { width, height: width > 500 ? 1000 : 844 }, serviceWorkers: 'block' });
  const f = { calls: [], unexpected: [], errors: [], done: !streaming };
  await context.route('**/*', async (route) => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname;
    if (url.origin !== 'https://visual-fixture.invalid') { f.unexpected.push(url.origin); return route.abort(); }
    if (req.method() === 'GET' && files.has(path)) {
      const file = files.get(path); return route.fulfill({ status: 200, contentType: file.type, body: file.body });
    }
    if (path === '/__qa_serif_font' && qaSerifFont) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaSerifFont });
    if (path === '/__qa_font' && qaFont) return route.fulfill({ status: 200, contentType: 'font/collection', body: qaFont });
    if (path === '/favicon.ico') return route.fulfill({ status: 204 });
    f.calls.push({ path, method: req.method() });
    const reply = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (path === '/api/auth/me') return reply({ user: { id: JOB, username: 'alice', role: 'user' }, csrf_token: CSRF });
    if (path === '/api/health') return reply({ status: 'ok' });
    if (path === '/api/ui/config') return reply({ upload_max_bytes: 4194304, ...(personal ? { personal_demo: personal } : {}) });
    if (path === '/api/auth/logout') return reply({ status: 'ok' });
    if (path === '/api/agent/contexts') return reply({ context_id: JOB, context_token: 'c'.repeat(43),
      expires_at: new Date(Date.now() + 3600000).toISOString() }, 201);
    if (path === '/api/jobs' && req.method() === 'POST') {
      if (submitStatus !== 202) return reply({ detail: submitStatus === 429 ? '请求过于频繁，请稍后重试。' : '上游服务暂时不可用。' }, submitStatus);
      return reply({ job_id: JOB, status: 'reserved' }, 202);
    }
    if (path === `/api/jobs/${JOB}` && req.method() === 'GET') {
      const status = f.done ? state : 'running';
      return reply({ job_id: JOB, kind: 'rag', status, failure_type: failureType, progress: { stage: 'retrieval', label: '正在检索知识库' },
        result: status === 'succeeded' ? { status: 'answered', answer, citations: [{ number: 1, text: '受控原文',
          metadata: { source_name: '研发资料.md', chunk_id: JOB, page: null } }] } : null });
    }
    if (path === `/api/jobs/${JOB}/events`) return route.fulfill({ status: 200, contentType: 'text/event-stream',
      body: frame(1, 'progress', { stage: 'retrieval', label: '正在检索知识库' }) + frame(2, 'delta', { text: '这是真实事件提供的待校验草稿。' }) });
    f.unexpected.push(`${req.method()} ${path}`); return reply({ detail: 'unexpected fixture request' }, 599);
  });
  const page = await context.newPage();
  page.on('pageerror', (error) => f.errors.push(error.message));
  page.setDefaultTimeout(10000);
  await page.goto('https://visual-fixture.invalid/', { waitUntil: 'networkidle' });
  await page.locator('#app-shell').waitFor({ state: 'visible' });
  if (qaFont) {
    // Test-only local OS font: never shipped, copied into dist or fetched remotely.
    await page.addStyleTag({ content: '@font-face{font-family:"Microsoft YaHei";src:url("/__qa_font")} @font-face{font-family:"Noto Serif SC";src:url("/__qa_font")}' });
    await page.evaluate(() => document.fonts.ready);
  }
  if (qaSerifFont) {
    await page.addStyleTag({ content: '@font-face{font-family:"Songti SC";src:url("/__qa_serif_font")} @font-face{font-family:SimSun;src:url("/__qa_serif_font")} :root{--font-display:SimSun,serif} .markdown-answer code{font-family:ui-monospace,SimSun,monospace}' });
    await page.evaluate(() => document.fonts.ready);
  }
  return { context, page, f };
}

async function send(page) {
  await page.locator('#chat-mode').selectOption('rag');
  await page.locator('#question-input').fill('请核对研发问题并给出建议。');
  await page.locator('#send-button').click();
}
function posts(f) { return f.calls.filter((entry) => entry.path === '/api/jobs' && entry.method === 'POST').length; }
async function transparent(locator, { inline = false, separator = false } = {}) {
  const values = await locator.evaluateAll((elements) => elements.map((element) => {
    const style = getComputedStyle(element);
    return { bg: style.backgroundColor, shadow: style.boxShadow, border: ['Top', 'Right', 'Bottom', 'Left'].map((side) => style[`border${side}Width`]) };
  }));
  assert.ok(values.length > 0);
  for (const value of values) { assert.equal(value.bg, 'rgba(0, 0, 0, 0)'); assert.equal(value.shadow, 'none'); assert.ok(separator ? ['0px', '1px'].includes(value.border[0]) : value.border[0] === '0px'); assert.deepEqual(value.border.slice(1, 3), ['0px', '0px']); assert.ok(inline ? ['0px', '2px'].includes(value.border[3]) : value.border[3] === '0px'); }
}
async function noPageOverflow(page) {
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const composer = await page.locator('#composer-form').boundingBox();
  const viewport = page.viewportSize();
  assert.ok(composer && composer.y >= 0 && composer.y + composer.height <= viewport.height + 1, 'composer remains in viewport');
}
async function safe(f) { assert.deepEqual(f.unexpected, []); assert.deepEqual(f.errors, []); }

for (const width of [1440, 390]) {
  test(`ink-style messages have accessible identity and transparent assistant Markdown at ${width}px`, async () => {
    const { context, page, f } = await fixture({ width });
    try {
      await send(page);
      await page.locator('.markdown-answer h2').getByText('知行建议').waitFor();
      const user = page.locator('.message.user'), assistant = page.locator('.message.assistant');
      assert.equal(await user.locator('.message-avatar svg').count(), 1);
      assert.equal(await user.locator('.message-avatar').textContent(), '');
      assert.equal(await user.locator('.message-label').count(), 0);
      assert.doesNotMatch(await user.textContent(), /你|alice/);
      assert.equal(await assistant.locator('.assistant-seal').textContent(), '知');
      assert.equal(await assistant.locator('.message-label strong').textContent(), '知行助手');
      assert.equal(await assistant.locator('.message-label small').textContent(), 'Project Copilot');
      const contrast = await assistant.locator('.assistant-product').evaluate((el) => {
        const rgb = (value) => value.match(/[\d.]+/g).slice(0, 3).map(Number);
        let surface = el, background = 'rgba(0, 0, 0, 0)';
        while (surface && background === 'rgba(0, 0, 0, 0)') { background = getComputedStyle(surface).backgroundColor; surface = surface.parentElement; }
        const luminance = (color) => rgb(color).map((v) => v / 255).map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
          .reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
        const a = luminance(getComputedStyle(el).color), b = luminance(background);
        return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      });
      assert.ok(contrast >= 4.5, `subtitle contrast ${contrast}`);
      assert.equal(await assistant.locator('.message-bubble').count(), 0);
      await transparent(assistant.locator('.message-block, .message-body, .markdown-answer, .message-label'));
      const bubble = user.locator('.message-bubble');
      assert.notEqual(await bubble.evaluate((el) => getComputedStyle(el).backgroundColor), 'rgba(0, 0, 0, 0)');
      assert.notEqual(await bubble.evaluate((el) => getComputedStyle(el).backgroundColor), 'rgb(255, 255, 255)');
      for (const tag of ['h2', 'p', 'strong', 'em', 'ul', 'ol', 'blockquote', 'pre', 'code', 'table']) assert.ok(await assistant.locator(`.markdown-answer ${tag}`).count() > 0, tag);
      assert.equal(await assistant.locator('.markdown-answer img, .markdown-answer script').count(), 0);
      assert.match(await assistant.locator('.markdown-answer').textContent(), /<img src=x/);
      await noPageOverflow(page);
      assert.ok(await assistant.locator('pre').evaluate((el) => el.scrollWidth > el.clientWidth));
      assert.equal(posts(f), 1);
      await assistant.locator('pre').scrollIntoViewIfNeeded();
      await page.screenshot({ path: `/tmp/pc-polish-${width}-markdown-code.png`, fullPage: true });
      await page.locator('#chat-messages').evaluate((el) => { el.style.scrollBehavior = 'auto'; el.scrollTop = el.scrollHeight; });
      await page.screenshot({ path: `/tmp/pc-polish-${width}-markdown-bottom.png`, fullPage: true });
      await page.locator('#chat-messages').evaluate((el) => { el.scrollTop = 0; });
      await page.screenshot({ path: `/tmp/pc-polish-${width}-markdown-top.png`, fullPage: true });
      await safe(f);
    } finally { await context.close(); }
  });
}

for (const [status, width] of [[429, 1440], [502, 1440], [503, 390]]) {
  test(`HTTP${status} displays an inline failure without repeated POST at ${width}px`, async () => {
    const { context, page, f } = await fixture({ submitStatus: status, width });
    try {
      await send(page);
      const notice = page.locator('.message.assistant [role="alert"]');
      await notice.waitFor();
      assert.equal(await page.locator('.message.assistant .message-bubble').count(), 0);
      assert.equal(await notice.locator('.notice-icon').count(), 1);
      await transparent(page.locator('.message.assistant .message-body, .message.assistant .message-block, .message.assistant [role="alert"]'), { inline: true });
      assert.match(await notice.textContent(), status === 429 ? /频繁|限流|拒绝/ : /不确定|核实|确认/);
      assert.doesNotMatch(await notice.textContent(), /后台已取消|正在自动重试|将自动重试/);
      assert.match(await notice.textContent(), /没有自动重试/);
      assert.equal(await page.locator('#question-input').isEnabled(), true);
      await page.waitForTimeout(150);
      assert.equal(posts(f), 1);
      await noPageOverflow(page);
      await page.screenshot({ path: `/tmp/pc-polish-${width}-error-${status}.png`, fullPage: true });
      await safe(f);
    } finally { await context.close(); }
  });
}

test('unknown keeps the task identifier and recovery as transparent inline guidance', async () => {
  const { context, page, f } = await fixture({ width: 390, state: 'unknown' });
  try {
    await send(page);
    const notice = page.locator('.message.assistant [role="alert"]');
    await notice.getByText(/待核实/).waitFor();
    assert.match(await notice.textContent(), new RegExp(JOB));
    const recovery = page.locator('#job-recovery');
    await recovery.waitFor({ state: 'visible' });
    assert.match(await recovery.textContent(), new RegExp(JOB));
    await transparent(recovery, { separator: true });
    await transparent(page.locator('.message.assistant .message-body, .message.assistant [role="alert"]'), { inline: true });
    assert.equal(posts(f), 1);
    await page.waitForTimeout(100);
    assert.equal(posts(f), 1);
    await noPageOverflow(page);
    await page.screenshot({ path: '/tmp/pc-polish-390-unknown.png', fullPage: true });
    await safe(f);
  } finally { await context.close(); }
});

test('progress, draft and stopped wait share inline styling without losing the original job', async () => {
  const { context, page, f } = await fixture({ width: 390, streaming: true });
  try {
    await send(page);
    await page.getByText('这是真实事件提供的待校验草稿。', { exact: true }).waitFor();
    assert.equal(await page.locator('.job-progress.inline-notice').count(), 1);
    await transparent(page.locator('.message.assistant .message-body, .message.assistant .job-progress, .message.assistant .stream-preview'), { inline: true });
    assert.match(await page.locator('.job-progress').textContent(), /最终校验尚未完成/);
    await page.locator('#stop-wait').click();
    const stopped = page.locator('.waiting-stopped.inline-notice');
    await stopped.waitFor();
    assert.match(await stopped.textContent(), /后台任务可能继续执行/);
    assert.equal(await page.locator('.stream-preview').count(), 0);
    assert.equal(await page.locator('#question-input').isEnabled(), true);
    assert.equal(await page.locator('#job-recovery').isVisible(), true);
    assert.equal(posts(f), 1);
    await transparent(stopped, { inline: true });
    await page.reload({ waitUntil: 'networkidle' });
    f.done = true;
    await page.getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.markdown-answer h2').getByText('知行建议').waitFor();
    assert.equal(posts(f), 1);
    await noPageOverflow(page);
    await safe(f);
  } finally { await context.close(); }
});

for (const width of [1440, 390]) {
  test(`personal capacity meter remains readable and private at ${width}px`, async () => {
    const personal = { expires_at: new Date(Date.now() + 86400000).toISOString(), remaining_seconds: 86399,
      job_limit: 500, jobs_used: 37, user_daily_limit: 1000, global_daily_limit: 2000,
      concurrent_limit: 3, occupied_slots: 2, worker_enabled: true, secret: 'PRIVATE_PROFILE_SECRET' };
    const { context, page, f } = await fixture({ width, personal });
    try {
      const meter = page.locator('#personal-demo-meter');
      await meter.waitFor({ state: 'visible' });
      assert.match(await meter.textContent(), /已用 37 \/ 500 次任务/);
      assert.match(await meter.textContent(), /未完成 2 \/ 3/);
      assert.match(await page.locator('#personal-demo-time').textContent(), /^23:59:\d{2}$/);
      assert.equal(await page.locator('#personal-demo-time').getAttribute('aria-live'), 'off');
      assert.match(await meter.textContent(), /后台按顺序处理队列/);
      assert.doesNotMatch(await page.locator('body').textContent(), /PRIVATE_PROFILE_SECRET/);
      await noPageOverflow(page);
      await page.screenshot({ path: `/tmp/pc-capacity-${width}.png`, fullPage: true });
      await page.locator('#logout-button').click();
      await page.locator('#auth-screen').waitFor({ state: 'visible' });
      assert.equal(await meter.isVisible(), false);
      await safe(f);
    } finally { await context.close(); }
  });
}

test('confirmed Agent budget failure shows reason and recoverable job without pretending unknown or success', async () => {
  const { context, page, f } = await fixture({ width: 390, state: 'failed', failureType: 'tool_budget' });
  try {
    await send(page);
    const notice = page.locator('.message.assistant [role="alert"]');
    await notice.getByText(/工具调用次数达到上限/).waitFor();
    assert.match(await notice.textContent(), /tool_budget/);
    assert.match(await notice.textContent(), new RegExp(JOB));
    assert.doesNotMatch(await notice.textContent(), /结果待核实|执行成功/);
    assert.equal(await page.locator('#job-recovery').isVisible(), true);
    await page.reload({ waitUntil: 'networkidle' });
    await page.getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.message.assistant [role="alert"]').getByText(/tool_budget/).waitFor();
    assert.equal(posts(f), 1);
    await noPageOverflow(page);
    await safe(f);
  } finally { await context.close(); }
});
