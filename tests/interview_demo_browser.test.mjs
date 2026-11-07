/** Real loopback HTTP + real EdgeOne build, with synthetic API data only. */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { startDemo } from '../scripts/interview_demo.mjs';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
let browser;
before(async () => { browser = await chromium.launch({ headless: true }); });
after(async () => { await browser?.close(); });

for (const width of [1440, 390]) test(`interactive local demo: citations, independent confirmation and read-only recovery at ${width}px`, async () => {
  const demo = await startDemo({ port: 0, durationMs: 650 });
  const context = await browser.newContext({ viewport: { width, height: width === 1440 ? 1000 : 844 }, serviceWorkers: 'block' });
  const errors = [], external = [], writes = [], events = [];
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin !== demo.url) { external.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', req => { if (req.method() === 'POST') writes.push(new URL(req.url()).pathname); if (new URL(req.url()).pathname.endsWith('/events')) events.push(req.url()); });
  const state = async () => (await (await fetch(`${demo.url}/__demo/state`)).json());
  const choose = async name => { await page.locator(`[data-scenario="${name}"]`).click(); await page.locator('#send-button').click(); };
  const overflow = async () => assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  try {
    await page.goto(demo.url, { waitUntil: 'networkidle' });
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    assert.match(await page.locator('#demo-toolbar').textContent(), /本地模拟/);
    await choose('knowledge');
    await page.locator('.stream-preview').getByText(/模拟草稿/).waitFor();
    await page.locator('.markdown-answer').getByText(/连接超时排查/).waitFor();
    await page.getByRole('button', { name: '查看 1 条依据' }).click();
    await page.locator('.evidence-card').click();
    await page.locator('#detail-content').getByText(/模拟资料/).waitFor();
    await page.getByRole('button', { name: '关闭详情', exact: true }).click();
    if (width === 390) await page.locator('#evidence-close').click();
    await choose('proposal');
    const confirm = page.getByRole('button', { name: '确认创建工单', exact: true });
    await confirm.waitFor();
    assert.equal((await state()).ticket_count, 1);
    assert.equal(writes.filter(p => p.endsWith('/confirm')).length, 0);
    await confirm.scrollIntoViewIfNeeded();
    await overflow();
    if (process.env.INTERVIEW_SCREENSHOT_DIR) {
      await mkdir(process.env.INTERVIEW_SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.INTERVIEW_SCREENSHOT_DIR, `INTERVIEW_DEMO_${width}.png`), fullPage: true, animations: 'disabled' });
    }
    await confirm.click();
    await page.locator('.proposal-card').getByText('已创建工单', { exact: true }).waitFor();
    assert.equal((await state()).ticket_count, 2);
    assert.equal((await state()).confirmations, 1);
    await choose('failure');
    await page.locator('.message.assistant [role="alert"]').getByText(/tool_budget/).waitFor();
    const submissions = (await state()).submissions;
    await page.reload({ waitUntil: 'networkidle' });
    await page.locator('#task-center-toggle').click();
    await page.getByRole('button', { name: '查询 / 恢复等待' }).click();
    await page.locator('.message.assistant [role="alert"]').getByText(/tool_budget/).waitFor();
    assert.equal((await state()).submissions, submissions);
    assert.equal(writes.filter(p => p === '/api/jobs').length, 3);
    assert.ok(events.length >= 3);
    await overflow();
    await page.locator('#demo-reset').click();
    await page.locator('#app-shell').waitFor({ state: 'visible' });
    assert.equal((await state()).submissions, 0);
    assert.equal((await state()).ticket_count, 1);
    assert.deepEqual(errors, []); assert.deepEqual(external, []);
  } finally { await context.close(); await demo.close(); }
});
