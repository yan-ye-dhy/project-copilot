/** Explicit, bounded real-browser exercise. No automatic write/model retries. */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, writeFile, access, readFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const args = process.argv.slice(2);
const option = (key, fallback) => args.includes(key) ? args[args.indexOf(key) + 1] : fallback;
if (!args.includes("--live") || !option("--output-dir")) {
  throw new Error("Explicit --live and a new --output-dir are required: this exercise calls real models and creates one ticket.");
}
const baseURL = option("--base-url", "http://127.0.0.1:8002");
const outputDir = path.resolve(option("--output-dir"));
// Explicit recovery for the recorded close-dialog selector failure only. This
// never repeats the successful upload/RAG calls or resumes a write in doubt.
const resumePath = option("--resume-after-rag");
let predecessor = null;
let predecessorHash = null;
if (resumePath) {
  const bytes = await readFile(resumePath);
  predecessor = JSON.parse(bytes);
  assert.equal(predecessor.status, "failed");
  assert.equal(predecessor.base_url, baseURL);
  assert.match(predecessor.failure?.message || "", /strict mode violation/);
  assert.equal(predecessor.requests.length, 2);
  assert.equal(predecessor.requests[0].path, "/documents/upload");
  assert.equal(predecessor.requests[0].result_status, "indexed");
  assert.equal(predecessor.requests[1].path, "/rag/ask");
  assert.equal(predecessor.requests[1].result_status, "answered");
  assert(predecessor.document_id && !predecessor.action_id && !predecessor.agent_action_id);
  predecessorHash = createHash("sha256").update(bytes).digest("hex");
}
try { await access(outputDir); throw new Error("Output directory already exists; refusing to overwrite."); }
catch (error) { if (error.code !== "ENOENT") throw error; }
await mkdir(outputDir, { recursive: true, mode: 0o700 });
const marker = predecessor?.marker || `UI-${new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14)}`;
const ticketTitle = `前端蓝鹤验收 ${marker}`;
const proposedTitle = `${ticketTitle} 后续提案`;
const report = {
  schema_version: 1, executor: "Codex", mode: "real_browser_real_services_and_models",
  base_url: baseURL, started_at: new Date().toISOString(), automatic_retries: 0,
  marker, assertions: [], requests: [], status: "running", actual_account_bill: null,
  ...(predecessor ? { continued_from: { report_sha256: predecessorHash,
    boundary: "Previous upload and RAG succeeded; stopped at an ambiguous dialog-close selector. They are not repeated. This report records only the remaining new requests." },
    document_id: predecessor.document_id } : {}),
};
const save = () => writeFile(path.join(outputDir, "summary.json"), JSON.stringify(report, null, 2) + "\n", { mode: 0o600 });
const passed = async (name) => { report.assertions.push({ name, status: "passed" }); await save(); };
const captures = [];
const pending = [];
const browser = await chromium.launch();
const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(20000);
const errors = [];
page.on("pageerror", (error) => errors.push(error.name + ": " + error.message));
page.on("response", (response) => {
  const url = new URL(response.url());
  if (url.origin !== new URL(baseURL).origin || !["/rag/ask", "/agent/ask", "/documents/upload", "/actions/preview"].includes(url.pathname) && !/^\/actions\/[^/]+\/(confirm|reject)$/.test(url.pathname)) return;
  pending.push((async () => {
    try {
      const data = await response.json();
      captures.push({ path: url.pathname, data, status: response.status() });
      report.requests.push({ path: url.pathname, status: response.status(), result_status: data.status,
        run_id: response.headers()["x-run-id"], trace_id: response.headers()["x-trace-id"],
        trace_exported: response.headers()["x-trace-exported"] === "true",
        ...(url.pathname === "/agent/ask" ? { model_calls: data.model_calls, usage: data.usage ?? null,
          model_events: data.model_events, tools: data.events?.map((item) => ({ name: item.name, status: item.status, failure_type: item.failure_type })) } : {}),
        ...(url.pathname === "/rag/ask" ? { generation: data.generation ?? null } : {}),
      });
      await save();
    } catch { /* A missing body is left unverified; assertions below stop the run. */ }
  })());
});

async function requestFromClick(pathname, click) {
  const waiting = page.waitForResponse((response) => new URL(response.url()).pathname === pathname && response.request().method() === "POST", { timeout: 190000 });
  await click();
  const response = await waiting;
  assert(response.ok(), `unexpected HTTP ${response.status()} for ${pathname}`);
  return response.json();
}

async function question(text, mode) {
  await page.locator('[data-view="chat"]').click();
  await page.locator("#chat-mode").selectOption(mode);
  await page.locator("#question-input").fill(text);
  const result = await requestFromClick(mode === "rag" ? "/rag/ask" : "/agent/ask", () => page.locator("#send-button").click());
  await page.waitForFunction(() => !document.getElementById("question-input").disabled);
  return result;
}

try {
  await page.goto("/");
  await page.waitForFunction(() => document.getElementById("service-status").textContent.includes("已连接"));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: path.join(outputDir, "home.png"), fullPage: true });
  await passed("real deployed homepage and browser scripts loaded");

  if (!predecessor) {
    await page.locator('[data-view="documents"]').click();
    const document = `# 前端蓝鹤验收资料 ${marker}\n蓝鹤服务切换环境后连接失败，应检查应用的数据库连接配置与PostgreSQL服务就绪状态。\n本资料的唯一识别码为 ${marker}。新工单必须先预览，再由用户通过独立确认创建。\n`;
    await page.locator("#file-input").setInputFiles({ name: `frontend-${marker}.md`, mimeType: "text/markdown", buffer: Buffer.from(document) });
    const upload = await requestFromClick("/documents/upload", () => page.locator("#upload-button").click());
    assert.equal(upload.status, "indexed"); assert.equal(upload.mode, "HYBRID_RERANK");
    report.document_id = upload.document.document_id;
    await page.locator("#upload-feedback").getByText("资料已入库", { exact: false }).waitFor();
    await page.screenshot({ path: path.join(outputDir, "uploaded.png"), fullPage: true });
    await passed("browser multipart upload indexed real Chinese document");

    const rag = await question(`请根据前端蓝鹤验收资料，说明识别码 ${marker} 对应的排查建议，并返回原文依据。`, "rag");
    assert.equal(rag.status, "answered");
    assert(rag.citations.some((item) => item.metadata.document_id === upload.document.document_id && item.text.includes(marker)));
    await page.locator(".evidence-card").filter({ hasText: upload.document.source_name }).first().click();
    assert((await page.locator("#detail-content").textContent()).includes(marker));
    await page.getByRole("button", { name: "关闭详情", exact: true }).click();
    await passed("real RAG answer and browser original-evidence dialog");
  }

  await page.locator('[data-view="tickets"]').click();
  await page.locator("#new-ticket").click();
  await page.locator("#proposal-title").fill(ticketTitle);
  await page.locator("#proposal-description").fill(`用真实浏览器验证同域提案与确认；排查数据库连接配置及服务就绪。标识 ${marker}。`);
  const preview = await requestFromClick("/actions/preview", () => page.locator("#proposal-submit").click());
  assert.equal(preview.status, "pending"); assert.equal(preview.ticket_id, null);
  report.action_id = preview.action_id;
  const card = page.locator(".proposal-card").filter({ has: page.getByRole("heading", { name: ticketTitle, exact: true }) });
  await card.getByRole("button", { name: "确认创建工单", exact: true }).waitFor();
  const before = await context.request.get("/tickets", { params: { q: ticketTitle } });
  assert.equal((await before.json()).items.filter((item) => item.title === ticketTitle).length, 0);
  assert.equal(captures.filter((item) => item.path.endsWith("/confirm")).length, 0);
  await passed("canonical backend preview shown; no ticket or confirmation before explicit click");
  const confirmed = await requestFromClick(`/actions/${preview.action_id}/confirm`, () => card.getByRole("button", { name: "确认创建工单", exact: true }).click());
  assert.equal(confirmed.status, "completed"); assert(confirmed.ticket_id);
  report.ticket_id = confirmed.ticket_id;
  await card.getByRole("button", { name: "查看已创建工单" }).click();
  await page.locator("#detail-content").getByText(ticketTitle, { exact: true }).waitFor();
  await page.getByRole("button", { name: "关闭详情", exact: true }).click();
  const after = await context.request.get("/tickets", { params: { q: ticketTitle } });
  const matches = (await after.json()).items.filter((item) => item.title === ticketTitle);
  assert.equal(matches.length, 1); assert.equal(matches[0].id, confirmed.ticket_id);
  await passed("explicit browser confirmation creates one real ticket and detail matches");

  const combined = await question(`请查询标题包含“${ticketTitle}”的真实工单，并结合识别码 ${marker} 对应的蓝鹤验收资料，分析现状与下一步建议。这里只分析，不生成提案。`, "agent");
  assert.equal(combined.status, "answered");
  assert(combined.sources.some((source) => source.kind === "ticket" && source.data.id === confirmed.ticket_id));
  assert(combined.sources.some((source) => source.kind === "knowledge" && source.data.metadata.document_id === report.document_id));
  await page.screenshot({ path: path.join(outputDir, "combined.png"), fullPage: true });
  await passed("real model combines actual ticket and uploaded knowledge through browser");

  const proposed = await question(`请根据刚才的分析，生成一个待确认工单，标题为“${proposedTitle}”，描述为继续检查数据库连接配置与服务就绪。只提出提案，等待我决定。`, "agent");
  assert.equal(proposed.status, "pending_confirmation");
  const action = proposed.sources.find((source) => source.kind === "proposal").data;
  report.agent_action_id = action.action_id;
  assert.equal(action.ticket_id, null);
  const proposedCard = page.locator(".proposal-card").filter({ has: page.getByRole("heading", { name: proposedTitle, exact: true }) });
  await proposedCard.getByRole("button", { name: "拒绝提案", exact: true }).waitFor();
  const rejected = await requestFromClick(`/actions/${action.action_id}/reject`, () => proposedCard.getByRole("button", { name: "拒绝提案", exact: true }).click());
  assert.equal(rejected.status, "cancelled"); assert.equal(rejected.ticket_id, null);
  const noNewTicket = await context.request.get("/tickets", { params: { q: proposedTitle } });
  assert.equal((await noNewTicket.json()).items.filter((item) => item.title === proposedTitle).length, 0);
  await passed("real Agent proposal can be rejected without creating a second ticket");

  await page.locator('[data-view="tickets"]').click();
  await page.locator("#ticket-query").fill(ticketTitle);
  await page.locator("#tickets-form").getByRole("button", { name: "查询", exact: true }).click();
  await page.locator(".ticket-card").filter({ hasText: ticketTitle }).waitFor();
  await page.screenshot({ path: path.join(outputDir, "tickets.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: path.join(outputDir, "mobile-tickets.png"), fullPage: true });
  await passed("real filtered ticket list and mobile layout");

  await Promise.all(pending);
  assert.equal(errors.length, 0);
  const agents = captures.filter((item) => item.path === "/agent/ask");
  const rags = captures.filter((item) => item.path === "/rag/ask");
  const usage = [...agents.map((item) => item.data.usage), ...rags.map((item) => item.data.generation?.usage)].filter(Boolean);
  report.model_usage = {
    reported_llm_attempts: agents.reduce((sum, item) => sum + item.data.model_calls, 0) + rags.filter((item) => item.data.generation).length,
    responses_with_usage: usage.length,
    prompt_tokens: usage.reduce((sum, item) => sum + item.prompt_tokens, 0),
    completion_tokens: usage.reduce((sum, item) => sum + item.completion_tokens, 0),
  };
  report.page_errors = errors; report.new_ticket_count = 1;
  report.status = "passed";
} catch (error) {
  report.status = "failed"; report.failure = { type: error.name, message: error.message };
  await page.screenshot({ path: path.join(outputDir, "failure.png"), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await Promise.all(pending);
  report.finished_at = new Date().toISOString();
  await save(); await context.close(); await browser.close();
  console.log(JSON.stringify({ status: report.status, passed_assertions: report.assertions.length, marker, output_dir: outputDir }));
}
