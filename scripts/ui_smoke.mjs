#!/usr/bin/env node
/* Real Chromium against local assets; every business request is intercepted.
 * This suite never contacts model, database, storage or credential services.
 */
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);

function argumentsFor(argv) {
  const options = { baseUrl: "http://127.0.0.1:8004", outputDir: null };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--base-url") options.baseUrl = argv[++index];
    else if (argument === "--output-dir") options.outputDir = argv[++index];
    else if (argument === "--help") {
      console.log("node scripts/ui_smoke.mjs --base-url http://127.0.0.1:8004 --output-dir data/ui-controlled-01");
      process.exit(0);
    } else throw new Error(`Unknown argument: ${argument}`);
  }
  if (!options.outputDir) throw new Error("--output-dir is required");
  const base = new URL(options.baseUrl);
  if (!["127.0.0.1", "localhost", "[::1]"].includes(base.hostname)) throw new Error("Use a local preview server");
  options.baseUrl = base.origin;
  options.outputDir = path.resolve(options.outputDir);
  if (existsSync(options.outputDir)) throw new Error("Refuse to overwrite an existing output directory");
  return options;
}

const CONTEXT_ID = "11111111-1111-4111-8111-111111111111";
const ACTION_ID = "22222222-2222-4222-8222-222222222222";
const TICKET_ID = "33333333-3333-4333-8333-333333333333";
const DOCUMENT_ID = "44444444-4444-4444-8444-444444444444";
const CHUNK_ID = "55555555-5555-4555-8555-555555555555";
const TOKEN = "T".repeat(43);
const FINGERPRINT = "b".repeat(64);
const XSS = '<img src="/xss-must-never-load" onerror="window.__copilotXss=true">';
const ORIGINAL = `蓝鹤服务需要核对数据库连接配置。${XSS}`;
const timestamp = () => new Date().toISOString();

function ticket(overrides = {}) {
  return { id: TICKET_ID, title: "蓝鹤排障工单", description: `检查环境配置。${XSS}`, status: "open",
    created_at: timestamp(), updated_at: timestamp(), ...overrides };
}

function knowledgeSource() {
  return { id: `knowledge:${CHUNK_ID}`, kind: "knowledge", data: {
    text: ORIGINAL, metadata: { document_id: DOCUMENT_ID, content_hash: "f".repeat(64), source_name: "蓝鹤研发手册.md",
      page: 0, chunk_id: CHUNK_ID, chunk_index: 0, index_version: "controlled-browser-fixture" }, score: 1.2,
  } };
}

function newAction(payload = { title: "蓝鹤后续排查", description: "核对数据库连接配置。" }) {
  return { action_id: ACTION_ID, context_id: CONTEXT_ID, status: "pending", payload,
    payload_fingerprint: FINGERPRINT, created_at: timestamp(),
    expires_at: new Date(Date.now() + 900000).toISOString(), ticket_id: null };
}

function agentAnswer(overrides = {}) {
  return { run_id: "controlled-ui-run", status: "answered", answer: `根据真实来源，先核对连接配置。${XSS}`,
    sources: [knowledgeSource(), { id: `ticket:${TICKET_ID}`, kind: "ticket", data: ticket() }],
    events: [{ tool_call_id: "controlled-call-1", name: "search_tickets", status: "ok", failure_type: null, elapsed_ms: 2 },
      { tool_call_id: "controlled-call-2", name: "search_knowledge", status: "ok", failure_type: null, elapsed_ms: 4 }],
    model_calls: 2, tool_calls: 2, tool_executions: 2,
    usage: { prompt_tokens: 120, completion_tokens: 30, total_tokens: 150 }, model_events: [],
    elapsed_ms: 120, failure_type: null, trace_exported: true, ...overrides };
}

function fixture() {
  return {
    calls: [], unknown: [], agentMode: "answer", agentQuestions: [], action: newAction(),
    uploadStatus: 200, confirmMode: "complete", confirmStatus: 200,
    decisionDelayMs: 0, previewBody: null, ticketDetailGate: null,
  };
}

function count(f, pathname, method = null) {
  return f.calls.filter((call) => call.pathname === pathname && (!method || call.method === method)).length;
}

async function installControlledRoutes(context, baseUrl, f) {
  const staticPaths = new Set(["/", "/ui-assets/styles.css", "/ui-assets/app.js", "/ui-assets/api.js"]);
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    if (url.origin === baseUrl && method === "GET" && staticPaths.has(url.pathname)) {
      await route.continue();
      return;
    }
    if (url.origin === baseUrl && url.pathname === "/favicon.ico") {
      await route.fulfill({ status: 204 });
      return;
    }
    const call = { pathname: url.pathname, method, search: url.search, headers: request.headers(), body: null };
    if (request.headers()["content-type"]?.includes("application/json")) {
      try { call.body = request.postDataJSON(); } catch { /* A malformed body is rejected below. */ }
    }
    f.calls.push(call);
    const reply = async (data, status = 200) => route.fulfill({
      status, contentType: "application/json",
      headers: { "X-Run-ID": `controlled-request-${f.calls.length}`, "X-Trace-ID": "a".repeat(32), "X-Trace-Exported": "true" },
      body: JSON.stringify(data),
    });
    if (url.origin !== baseUrl) {
      f.unknown.push({ method, pathname: url.pathname, origin: url.origin });
      await reply({ detail: "External networking is forbidden during controlled browser QA" }, 599);
      return;
    }
    if (method === "GET" && url.pathname === "/health") return reply({ status: "ok", service: "Controlled browser fixture" });
    if (method === "GET" && url.pathname === "/ui/config") return reply({ upload_max_bytes: 1024 * 1024 });
    if (method === "POST" && url.pathname === "/agent/contexts") {
      return reply({ context_id: CONTEXT_ID, context_token: TOKEN, expires_at: new Date(Date.now() + 86400000).toISOString() }, 201);
    }
    if (method === "POST" && url.pathname === "/agent/ask") {
      f.agentQuestions.push(call.body);
      if (f.agentMode === "failed") return reply(agentAnswer({ status: "failed", answer: "本次模型请求未完成。",
        sources: [], events: [], model_calls: 1, tool_calls: 0, tool_executions: 0, failure_type: "model_error" }));
      if (f.agentMode === "proposal") {
        const suggested = { ...f.action, payload_fingerprint: "a".repeat(64), payload: { title: "模型提供的非最终预览", description: "应读取后端正式预览。" } };
        return reply(agentAnswer({ status: "pending_confirmation", answer: "请核对工单提案。",
          sources: [{ id: `proposal:${ACTION_ID}`, kind: "proposal", data: suggested }],
          events: [{ tool_call_id: "proposal-call", name: "propose_ticket", status: "ok", failure_type: null, elapsed_ms: 2 }] }));
      }
      return reply(agentAnswer({ answer: f.agentQuestions.length === 1 ? agentAnswer().answer : "已结合本轮历史继续分析。" }));
    }
    if (method === "POST" && url.pathname === "/rag/ask") {
      const citation = knowledgeSource().data;
      return reply({ question: call.body?.question, status: "answered", answer: "知识问答：应先核对运行配置。",
        citations: [{ number: 1, text: citation.text, metadata: citation.metadata }], evidence_count: 1,
        generation: { usage: { prompt_tokens: 50, completion_tokens: 10, total_tokens: 60 } } });
    }
    if (method === "POST" && url.pathname === "/documents/upload") {
      call.multipart = request.postDataBuffer()?.toString("utf8") || "";
      if (f.uploadStatus !== 200) return reply({ detail: "受控入库失败，请检查服务状态。" }, f.uploadStatus);
      return reply({ status: "indexed", mode: "HYBRID_RERANK", document: { document_id: DOCUMENT_ID,
        content_hash: "f".repeat(64), source_name: "browser-knowledge.md", chunk_ids: [CHUNK_ID] },
        chunk_count: 1, size_bytes: 18, index_version: "controlled-browser-fixture", run_id: "controlled-upload", trace_exported: true });
    }
    if (method === "GET" && url.pathname === "/tickets") {
      let items;
      if (url.searchParams.has("q") || url.searchParams.has("status")) items = [ticket({ status: "in_progress", title: "正在处理的蓝鹤工单" })];
      else if (url.searchParams.get("offset") === "10") items = [ticket({ title: "下一页工单" })];
      else items = Array.from({ length: 10 }, (_, index) => ticket({ title: index ? `演示工单 ${index}` : `蓝鹤排障 ${XSS}` }));
      return reply({ items, limit: 10, offset: Number(url.searchParams.get("offset") || 0) });
    }
    if (method === "GET" && url.pathname === `/tickets/${TICKET_ID}`) {
      if (f.ticketDetailGate) await f.ticketDetailGate;
      return reply(ticket());
    }
    const privileged = url.pathname.startsWith("/actions/");
    if (privileged && (request.headers()["x-copilot-context-id"] !== CONTEXT_ID || request.headers()["x-copilot-context-token"] !== TOKEN)) {
      f.unknown.push({ method, pathname: url.pathname, reason: "missing or mismatched controlled context" });
      return reply({ detail: "上下文凭据不匹配" }, 403);
    }
    if (method === "POST" && url.pathname === "/actions/preview") {
      f.previewBody = call.body;
      f.action = newAction(call.body);
      return reply(f.action, 201);
    }
    if (method === "GET" && url.pathname === `/actions/${ACTION_ID}`) return reply(f.action);
    if (method === "POST" && url.pathname === `/actions/${ACTION_ID}/confirm`) {
      if (f.decisionDelayMs) await new Promise((resolve) => setTimeout(resolve, f.decisionDelayMs));
      if (f.confirmStatus !== 200) return reply({ detail: f.confirmStatus === 403 ? "上下文凭据无效或已过期" : "提案指纹不匹配" }, f.confirmStatus);
      if (call.body?.payload_fingerprint !== FINGERPRINT) {
        f.unknown.push({ method, pathname: url.pathname, reason: "wrong canonical fingerprint" });
        return reply({ detail: "提案指纹不匹配" }, 409);
      }
      if (f.confirmMode === "expired") {
        f.action = { ...f.action, status: "expired" };
        return reply(f.action);
      }
      f.action = { ...f.action, status: "completed", ticket_id: TICKET_ID };
      if (f.confirmMode === "ambiguous") return route.abort("failed");
      if (f.confirmMode === "changed-payload") return reply({ ...f.action, payload: { ...f.action.payload, description: "与已预览内容不一致" } });
      return reply(f.action);
    }
    if (method === "POST" && url.pathname === `/actions/${ACTION_ID}/reject`) {
      f.action = { ...f.action, status: "cancelled" };
      return reply(f.action);
    }
    f.unknown.push({ method, pathname: url.pathname });
    return reply({ detail: "Unexpected business request was blocked by the controlled suite" }, 599);
  });
}

async function send(page, question) {
  await page.locator("#question-input").fill(question);
  await page.locator("#send-button").click();
  await page.waitForFunction(() => document.querySelector("#question-input")?.disabled === false);
}

async function formProposal(page, title = "浏览器受控排障工单") {
  await page.locator('[data-view="tickets"]').click();
  await page.locator("#new-ticket").click();
  await page.locator("#proposal-title").fill(title);
  await page.locator("#proposal-description").fill("核对环境、数据库配置和服务就绪状态。");
  await page.locator("#proposal-submit").click();
  const card = page.locator(".proposal-card").last();
  await card.getByRole("button", { name: "确认创建工单", exact: true }).waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector(".proposal-card .proposal-actions button")?.disabled);
  return card;
}

async function noHorizontalOverflow(page) {
  const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }));
  assert.ok(dimensions.scroll <= dimensions.width + 1, `Horizontal overflow: ${JSON.stringify(dimensions)}`);
}

async function main() {
  const options = argumentsFor(process.argv.slice(2));
  const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  await mkdir(options.outputDir, { recursive: true });
  const summary = { schema_version: 1, mode: "controlled_browser_routes", executor: "Codex",
    base_url: options.baseUrl, started_at: timestamp(), real_model_calls: 0, real_database_calls: 0,
    business_api_policy: "All business and configuration APIs fulfilled by browser routes; only same-origin HTML/CSS/JS reach the preview server.",
    cases: [] };
  const browser = await chromium.launch({ headless: true });
  const runCase = async (name, verify, { mobile = false, expectedNetworkError = false } = {}) => {
    const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 },
      deviceScaleFactor: 1, serviceWorkers: "block" });
    const f = fixture();
    const errors = [];
    const consoleErrors = [];
    const item = { name, status: "running", controlled_requests: [], page_errors: errors, console_errors: consoleErrors,
      expected_controlled_network_error: expectedNetworkError };
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
    await installControlledRoutes(context, options.baseUrl, f);
    try {
      await page.goto(options.baseUrl, { waitUntil: "networkidle", timeout: 20000 });
      await page.waitForFunction(() => document.querySelector("#service-status")?.textContent === "服务已连接");
      await verify(page, f, options.outputDir);
      assert.deepEqual(f.unknown, [], "An unrecognized request was blocked; the UI must not leak business calls to a real service");
      assert.deepEqual(errors, [], "The browser reported an uncaught page error");
      if (!expectedNetworkError) assert.deepEqual(consoleErrors, [], "The browser console reported an unexpected error");
      item.status = "passed";
    } catch (error) {
      item.status = "failed";
      item.error = { name: error.name, message: error.message };
      await page.screenshot({ path: path.join(options.outputDir, `${name}-failure.png`), fullPage: true }).catch(() => {});
    } finally {
      item.controlled_requests = f.calls.map(({ method, pathname, search }) => ({ method, pathname, search }));
      item.blocked_unknown_requests = f.unknown;
      summary.cases.push(item);
      await context.close();
    }
  };
  try {
    await runCase("desktop-agent-history-sources-xss", async (page, f, output) => {
      await page.screenshot({ path: path.join(output, "desktop-welcome.png"), fullPage: true });
      await page.locator("#question-input").fill("请结合蓝鹤资料与真实工单分析。");
      await page.locator("#composer-form").evaluate((form) => { form.requestSubmit(); form.requestSubmit(); });
      await page.locator(".message.assistant .message-text").getByText(agentAnswer().answer, { exact: true }).waitFor();
      await page.waitForFunction(() => !document.querySelector("#question-input").disabled);
      assert.equal(count(f, "/agent/ask", "POST"), 1);
      assert.equal(count(f, "/agent/contexts", "POST"), 1);
      assert.equal(await page.locator(".message-text img, .message-text script").count(), 0);
      assert.equal(await page.evaluate(() => window.__copilotXss), undefined);
      assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
      assert.ok(!(await page.locator("body").textContent()).includes(TOKEN));
      await page.locator(".evidence-card").first().click();
      assert.equal(await page.locator("#detail-content .source-original").textContent(), ORIGINAL);
      assert.equal(await page.locator("#detail-content img").count(), 0);
      await page.locator('[data-close-dialog="detail-dialog"]').first().click();
      await send(page, "请继续说明下一步。");
      assert.equal(f.agentQuestions.length, 2);
      assert.deepEqual(f.agentQuestions[1].history, [
        { role: "user", content: "请结合蓝鹤资料与真实工单分析。" },
        { role: "assistant", content: agentAnswer().answer },
      ]);
      assert.equal(await page.locator(".evidence-card").count(), 2);
      await page.screenshot({ path: path.join(output, "desktop-conversation.png"), fullPage: true });
    });

    await runCase("rag-mode", async (page, f) => {
      await page.locator("#chat-mode").selectOption("rag");
      await send(page, "知识库建议先检查什么？");
      await page.locator(".message.assistant .message-text").getByText("知识问答：应先核对运行配置。", { exact: true }).waitFor();
      assert.equal(count(f, "/agent/ask"), 0);
      assert.equal(count(f, "/agent/contexts"), 0);
      const call = f.calls.find((item) => item.pathname === "/rag/ask");
      assert.deepEqual(call.body, { question: "知识库建议先检查什么？" });
      assert.equal(await page.locator(".evidence-card").count(), 1);
    });

    await runCase("late-ticket-detail-does-not-overwrite-knowledge", async (page, f, output) => {
      await send(page, "请结合蓝鹤资料与工单说明排查建议。");
      let releaseTicket;
      f.ticketDetailGate = new Promise((resolve) => { releaseTicket = resolve; });
      try {
        await page.locator(".evidence-card").nth(1).click();
        const ticketRequest = page.waitForRequest((request) => new URL(request.url()).pathname === `/tickets/${TICKET_ID}`);
        await page.locator("#detail-content").getByRole("button", { name: "查看工单详情", exact: true }).click();
        await ticketRequest;
        await page.locator("#detail-content").getByText("正在读取工单详情…", { exact: true }).waitFor();
        await page.locator('[data-close-dialog="detail-dialog"]').first().click();
        await page.locator(".evidence-card").first().click();
        assert.equal(await page.locator("#detail-title").textContent(), "蓝鹤研发手册.md");
        assert.equal(await page.locator("#detail-content .source-original").textContent(), ORIGINAL);

        const ticketResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `/tickets/${TICKET_ID}`);
        releaseTicket();
        await (await ticketResponse).finished();
        // Let the response JSON and its awaited UI callback finish before checking
        // that a newer, unrelated source detail still owns the dialog.
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await page.locator("#detail-title").textContent(), "蓝鹤研发手册.md");
        assert.equal(await page.locator("#detail-content .source-original").textContent(), ORIGINAL);
        assert.equal(await page.locator("#detail-content .ticket-detail").count(), 0);
        assert.equal(count(f, `/tickets/${TICKET_ID}`, "GET"), 1);
        await page.screenshot({ path: path.join(output, "detail-race-keeps-knowledge.png"), fullPage: true });
      } finally { releaseTicket(); }
    });

    await runCase("upload-multipart-and-errors", async (page, f, output) => {
      await page.locator('[data-view="documents"]').click();
      await page.locator("#file-input").setInputFiles({ name: "not-allowed.exe", mimeType: "application/octet-stream", buffer: Buffer.from("not allowed") });
      await page.locator("#toast-root").getByText("请选择 TXT、Markdown 或 PDF 文件。", { exact: true }).waitFor();
      assert.equal(count(f, "/documents/upload"), 0);
      assert.equal(await page.locator("#upload-button").isDisabled(), true);
      const upload = { name: "browser-knowledge.md", mimeType: "text/markdown", buffer: Buffer.from("浏览器受控资料") };
      await page.locator("#file-input").setInputFiles(upload);
      assert.equal(count(f, "/documents/upload"), 0);
      await page.locator("#upload-button").click();
      await page.locator("#upload-feedback").getByText("资料已入库，现在可以在智能协作中提问。", { exact: true }).waitFor();
      const call = f.calls.find((item) => item.pathname === "/documents/upload");
      assert.match(call.headers["content-type"], /^multipart\/form-data; boundary=/);
      assert.match(call.multipart, /name="file"; filename="browser-knowledge.md"/);
      assert.match(call.multipart, /浏览器受控资料/);
      assert.equal(await page.locator(".upload-record").count(), 1);
      f.uploadStatus = 503;
      await page.locator("#file-input").setInputFiles({ ...upload, name: "failed.md" });
      await page.locator("#upload-button").click();
      await page.locator("#upload-feedback").getByText("受控入库失败，请检查服务状态。", { exact: true }).waitFor();
      assert.match(await page.locator("#upload-feedback").textContent(), /请求编号：controlled-request-/);
      assert.equal(count(f, "/documents/upload"), 2);
      assert.equal(await page.locator(".upload-record").count(), 1);
      await page.screenshot({ path: path.join(output, "documents-and-upload-feedback.png"), fullPage: true });
    }, { expectedNetworkError: true });

    await runCase("tickets-filter-pagination-detail", async (page, f, output) => {
      await page.locator('[data-view="tickets"]').click();
      await page.locator(".ticket-card").first().waitFor();
      assert.equal(await page.locator(".ticket-card").count(), 10);
      assert.equal(await page.locator("#tickets-next").isDisabled(), false);
      await page.locator("#tickets-next").click();
      await page.locator(".ticket-card").getByText("下一页工单", { exact: true }).waitFor();
      assert.equal(new URLSearchParams(f.calls.filter((item) => item.pathname === "/tickets").at(-1).search).get("offset"), "10");
      await page.locator("#tickets-prev").click();
      await page.waitForFunction(() => document.querySelectorAll(".ticket-card").length === 10);
      assert.equal(await page.locator("#tickets-list img, #tickets-list script").count(), 0);
      await page.locator("#ticket-query").fill("蓝鹤");
      await page.locator("#ticket-status").selectOption("in_progress");
      await page.locator(".ticket-card").getByText("正在处理的蓝鹤工单", { exact: true }).waitFor();
      const query = new URLSearchParams(f.calls.filter((item) => item.pathname === "/tickets").at(-1).search);
      assert.equal(query.get("q"), "蓝鹤");
      assert.equal(query.get("status"), "in_progress");
      assert.equal(query.get("offset"), "0");
      await page.locator(".ticket-card").first().click();
      await page.locator("#detail-content .ticket-detail").waitFor();
      assert.match(await page.locator("#detail-content").textContent(), new RegExp(TICKET_ID));
      assert.equal(await page.locator("#detail-content img, #detail-content script").count(), 0);
      await page.locator('[data-close-dialog="detail-dialog"]').first().click();
      await page.screenshot({ path: path.join(output, "tickets-filtered.png"), fullPage: true });
    });

    await runCase("manual-preview-confirm-double-click", async (page, f, output) => {
      f.decisionDelayMs = 180;
      const card = await formProposal(page);
      assert.equal(count(f, "/actions/preview", "POST"), 1);
      assert.equal(count(f, `/actions/${ACTION_ID}`, "GET"), 1);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 0);
      assert.match(await card.textContent(), /浏览器受控排障工单/);
      await card.getByRole("button", { name: "确认创建工单", exact: true }).dblclick({ delay: 30 });
      await card.getByText("已创建工单", { exact: true }).waitFor();
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 1);
      const decision = f.calls.find((item) => item.pathname.endsWith("/confirm"));
      assert.deepEqual(decision.body, { payload_fingerprint: FINGERPRINT });
      assert.equal(count(f, "/tickets", "POST"), 0);
      assert.match(await card.textContent(), new RegExp(TICKET_ID));
      await page.screenshot({ path: path.join(output, "proposal-confirmed.png"), fullPage: true });
    });

    await runCase("blank-proposal-no-write", async (page, f) => {
      await page.locator('[data-view="tickets"]').click();
      await page.locator("#new-ticket").click();
      await page.locator("#proposal-title").fill("   ");
      await page.locator("#proposal-description").fill("这是有效的排障说明。");
      await page.locator("#proposal-submit").click();
      await page.locator("#toast-root").getByText("请填写有效的工单标题和详细描述。", { exact: true }).waitFor();
      assert.equal(await page.locator("#proposal-dialog").evaluate((dialog) => dialog.open), true);
      assert.equal(f.calls.filter((call) => call.method !== "GET").length, 0);
      await page.locator("#proposal-title").fill("有效的排障标题");
      await page.locator("#proposal-description").fill("\n   \n");
      await page.locator("#proposal-submit").click();
      await page.waitForFunction(() => [...document.querySelectorAll("#toast-root .toast")]
        .filter((node) => node.textContent === "请填写有效的工单标题和详细描述。").length === 2);
      assert.equal(f.calls.filter((call) => call.method !== "GET").length, 0);
      assert.equal(count(f, "/agent/contexts", "POST"), 0);
      assert.equal(count(f, "/actions/preview", "POST"), 0);
      assert.equal(await page.locator("#proposal-submit").isDisabled(), false);
    });

    await runCase("agent-proposal-canonical-preview-and-reject", async (page, f) => {
      f.agentMode = "proposal";
      await send(page, "请提出一张待确认的排障工单。");
      const card = page.locator(".proposal-card");
      await card.getByRole("button", { name: "拒绝提案", exact: true }).waitFor();
      assert.match(await card.textContent(), /蓝鹤后续排查/);
      assert.doesNotMatch(await card.textContent(), /模型提供的非最终预览/);
      assert.equal(count(f, `/actions/${ACTION_ID}`, "GET"), 1);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 0);
      await card.getByRole("button", { name: "拒绝提案", exact: true }).click();
      await card.getByText("已拒绝", { exact: true }).waitFor();
      assert.equal(count(f, `/actions/${ACTION_ID}/reject`, "POST"), 1);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 0);
    });

    await runCase("expired-http200-is-not-created", async (page, f) => {
      f.confirmMode = "expired";
      const card = await formProposal(page);
      await card.getByRole("button", { name: "确认创建工单", exact: true }).click();
      await card.getByText("已过期", { exact: true }).waitFor();
      assert.doesNotMatch(await card.textContent(), /已创建工单|工单编号：/);
      assert.equal(await card.getByRole("button", { name: "确认创建工单", exact: true }).count(), 0);
    });

    await runCase("uncertain-confirm-query-only-recovery", async (page, f) => {
      f.confirmMode = "ambiguous";
      const card = await formProposal(page);
      await card.getByRole("button", { name: "确认创建工单", exact: true }).click();
      await card.getByRole("button", { name: "查询最新状态", exact: true }).waitFor();
      assert.equal(await card.getByRole("button", { name: "确认创建工单", exact: true }).isDisabled(), true);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 1);
      assert.equal(count(f, `/actions/${ACTION_ID}`, "GET"), 1);
      await card.getByRole("button", { name: "查询最新状态", exact: true }).click();
      await card.getByText("已创建工单", { exact: true }).waitFor();
      assert.equal(count(f, `/actions/${ACTION_ID}`, "GET"), 2);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 1);
    }, { expectedNetworkError: true });

    for (const status of [403, 409]) await runCase(`confirmation-${status}-visible-and-blocked`, async (page, f) => {
      f.confirmStatus = status;
      const card = await formProposal(page);
      await card.getByRole("button", { name: "确认创建工单", exact: true }).click();
      await card.getByText(status === 403 ? "上下文凭据无效或已过期" : "提案指纹不匹配", { exact: true }).waitFor();
      assert.match(await card.textContent(), /请求编号：controlled-request-/);
      assert.equal(await card.getByRole("button", { name: "确认创建工单", exact: true }).isDisabled(), true);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 1);
      assert.doesNotMatch(await card.textContent(), /已创建工单|工单编号：/);
    }, { expectedNetworkError: true });

    await runCase("changed-preview-response-blocked", async (page, f) => {
      f.confirmMode = "changed-payload";
      const card = await formProposal(page);
      await card.getByRole("button", { name: "确认创建工单", exact: true }).click();
      await card.getByText("提案内容与已预览版本不一致，已停止确认。请重新提出操作。", { exact: true }).waitFor();
      assert.equal(await card.getByRole("button", { name: "确认创建工单", exact: true }).isDisabled(), true);
      assert.doesNotMatch(await card.textContent(), /已创建工单|工单编号：/);
      assert.equal(count(f, `/actions/${ACTION_ID}/confirm`, "POST"), 1);
    });

    await runCase("agent-failed-http200-and-recovery", async (page, f) => {
      f.agentMode = "failed";
      await send(page, "请分析一个问题。");
      await page.locator(".message.assistant").getByText("本次处理未完成，可查看运行详情定位原因。", { exact: true }).waitFor();
      f.agentMode = "answer";
      await send(page, "重新提出一个明确问题。");
      assert.equal(f.agentQuestions.length, 2);
      assert.deepEqual(f.agentQuestions[1].history, []);
      assert.equal(count(f, "/agent/contexts", "POST"), 1);
    });

    await runCase("mobile-layout-and-evidence-drawer", async (page, f, output) => {
      const checkNavigationNames = async () => {
        for (const name of ["智能协作", "知识资料", "工单"]) {
          const button = page.locator("#app-nav").getByRole("button", { name, exact: true });
          assert.equal(await button.count(), 1, `Navigation must expose its accessible name: ${name}`);
          assert.equal(await button.isVisible(), true);
        }
      };
      await checkNavigationNames();
      await page.setViewportSize({ width: 900, height: 844 });
      await checkNavigationNames();
      await noHorizontalOverflow(page);
      await page.setViewportSize({ width: 390, height: 844 });
      await checkNavigationNames();
      await noHorizontalOverflow(page);
      await send(page, "请根据蓝鹤资料提供建议。");
      await noHorizontalOverflow(page);
      assert.equal(await page.locator("#evidence-panel").isVisible(), false);
      await page.locator(".source-button").first().click();
      await page.locator("#evidence-panel").waitFor({ state: "visible" });
      assert.equal(await page.locator("#evidence-toggle").getAttribute("aria-expanded"), "true");
      await noHorizontalOverflow(page);
      await page.screenshot({ path: path.join(output, "mobile-evidence.png"), fullPage: true });
      await page.locator("#evidence-close").click();
      await page.locator("#evidence-panel").waitFor({ state: "hidden" });
      await page.locator('[data-view="tickets"]').click();
      await page.locator(".ticket-card").first().waitFor();
      await noHorizontalOverflow(page);
      await page.locator("#new-ticket").click();
      await noHorizontalOverflow(page);
      await page.screenshot({ path: path.join(output, "mobile-proposal-form.png"), fullPage: true });
    }, { mobile: true });
  } finally {
    await browser.close();
  }
  summary.passed = summary.cases.filter((item) => item.status === "passed").length;
  summary.failed = summary.cases.length - summary.passed;
  summary.status = summary.failed ? "failed" : "passed";
  summary.finished_at = timestamp();
  await writeFile(path.join(options.outputDir, "summary.json"), `${JSON.stringify(summary, null, 2)}\n`);
  console.log(JSON.stringify({ status: summary.status, passed: summary.passed, failed: summary.failed,
    cases: summary.cases.map(({ name, status }) => ({ name, status })), output_dir: options.outputDir,
    real_model_calls: 0, real_database_calls: 0 }));
  return summary.failed ? 1 : 0;
}

main().then((code) => { process.exitCode = code; }).catch((error) => {
  console.error(JSON.stringify({ status: "failed", error_type: error.name, message: error.message }));
  process.exitCode = 1;
});
