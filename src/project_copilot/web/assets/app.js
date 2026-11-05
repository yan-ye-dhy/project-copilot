import { apiRequest, ApiError, ContextSession, boundHistory, validateAction, runPublicJob, resumePublicJob,
  PUBLIC_BUILD, setCsrfToken, onAuthenticationFailure } from "./api.js";

import { renderMarkdown } from "./markdown.js";

const $ = (id) => document.getElementById(id);
const session = new ContextSession();
const state = {
  view: "chat", busy: false, activeWait: null, jobContexts: new Map(), proposalBusy: false, history: [], selectedFile: null,
  uploading: false, uploadLimit: null, ticketOffset: 0, ticketLimit: 10,
  ticketsLoaded: false, ticketsBusy: false, ticketsRequest: 0, detailRequest: 0, proposals: new Map(), epoch: 0,
};
const auth = { user: null, busy: false };
const views = {
  chat: ["智能协作", "从一个问题开始，让资料与业务连接起来。"],
  documents: ["知识资料", "上传研发资料，让每一次回答都有据可查。"],
  tickets: ["工单", "查看真实处理记录，让问题有迹可循。"],
};
const statusLabels = { open: "待处理", in_progress: "处理中", closed: "已关闭", pending: "待你确认", completed: "已创建工单", cancelled: "已拒绝", expired: "已过期" };

function unresolvedProposals() {
  return [...state.proposals.values()].some((record) => record.uncertain || !record.action ||
    (record.action.status === "pending" && Date.parse(record.action.expires_at) > Date.now()));
}

function showAuth(mode = "login", message = "", success = false) {
  $("app-shell").hidden = true; $("workspace-skip").hidden = true;
  $("auth-screen").hidden = false;
  $("login-form").hidden = mode !== "login"; $("register-form").hidden = mode !== "register";
  $("auth-title").textContent = mode === "login" ? "登录工作空间" : "创建账号";
  $("auth-show-login").classList.toggle("active", mode === "login");
  $("auth-show-register").classList.toggle("active", mode === "register");
  $("auth-show-login").setAttribute("aria-pressed", String(mode === "login"));
  $("auth-show-register").setAttribute("aria-pressed", String(mode === "register"));
  $("auth-feedback").textContent = message;
  $("auth-feedback").classList.toggle("success", success);
}

function clearCredentialInputs() {
  $("login-password").value = "";
  $("register-password").value = "";
  $("register-invitation").value = "";
}

function resetWorkspace() {
  // Late responses from a previous account must not populate the next one.
  state.activeWait?.controller.abort(); state.activeWait = null;
  clearSavedJobs();
  state.epoch += 1; state.ticketsRequest += 1; state.detailRequest += 1;
  session.clear(); setCsrfToken(null); auth.user = null;
  state.history = []; state.proposals.clear(); state.jobContexts.clear(); state.selectedFile = null;
  state.busy = false; state.proposalBusy = false; state.uploading = false;
  state.ticketsLoaded = false; state.ticketsBusy = false; state.ticketOffset = 0;
  $("chat-messages").querySelectorAll(".message").forEach((node) => node.remove());
  $("chat-welcome").hidden = false; $("question-input").value = ""; $("chat-mode").value = "agent";
  $("upload-records").replaceChildren(element("div", "empty-state", "本页暂无上传记录。"));
  $("upload-feedback").replaceChildren(); $("file-input").value = "";
  $("tickets-list").replaceChildren(element("div", "empty-state", "准备查询工单。"));
  $("tickets-feedback").replaceChildren(); $("ticket-query").value = ""; $("ticket-status").value = "";
  $("tickets-page").textContent = "第 1 页"; $("tickets-prev").disabled = $("tickets-next").disabled = true;
  $("proposal-form").reset(); $("proposal-form").querySelector(".proposal-feedback")?.remove();
  $("proposal-submit").disabled = false; $("detail-content").replaceChildren();
  for (const id of ["proposal-dialog", "detail-dialog"]) if ($(id).open) $(id).close();
  $("account-name").textContent = ""; $("account-role").textContent = "";
  $("account-panel").hidden = true; $("toast-root").replaceChildren();
  $("public-documents-list").replaceChildren();
  showEvidence([]); toggleEvidence(false); setView("chat"); updateComposer(); renderFile();
}

function setAuthenticationBusy(busy) {
  auth.busy = busy;
  for (const id of ["login-submit", "register-submit", "auth-show-login", "auth-show-register", "logout-button"]) $(id).disabled = busy;
}

function authenticationError(error) {
  if (error.status === 401) return "账号、密码或会话无效，请检查后重试。";
  if (error.status === 400 || error.status === 422) return "账号、密码或邀请码不符合要求。";
  if (error.status === 403) return "请求未通过安全校验，请刷新页面后重试。";
  if (error.status === 429) return "请求过于频繁，请稍后再试。";
  return "认证服务暂不可用，请检查网络或稍后刷新页面。";
}

function activateUser(data) {
  const user = data?.user;
  if (!user || typeof user.id !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(user.id) ||
      typeof user.username !== "string" || !user.username.trim() || typeof data.csrf_token !== "string" ||
      !["user", "admin"].includes(user.role)) throw new ApiError("登录结果无效。", { kind: "response" });
  try { setCsrfToken(data.csrf_token); }
  catch { throw new ApiError("登录结果无效。", { kind: "response" }); }
  auth.user = Object.freeze({ id: user.id, username: user.username, role: user.role });
  $("account-name").textContent = user.username;
  $("account-role").textContent = user.role === "admin" ? "管理员" : "普通用户";
  $("account-panel").hidden = false;
  const canUpload = user.role === "admin";
  $("document-admin-controls").hidden = !canUpload;
  $("upload-records-heading").hidden = !canUpload;
  $("upload-records").hidden = !canUpload;
  $("upload-public-info").hidden = canUpload;
  $("upload-public-info").textContent = "公共知识资料由管理员维护。你可以在智能协作中使用“知识问答”查询。";
  $("public-documents").hidden = false;
  $("documents-description").textContent = "使用公共知识库提问，并查看答案引用的原文。";
  $("file-input").accept = ".txt,.md";
  $("auth-screen").hidden = true; $("app-shell").hidden = false; $("workspace-skip").hidden = false;
  clearCredentialInputs();
  initializeServices(); renderRecovery();
}

async function submitAuthentication(event, register) {
  event.preventDefault();
  if (auth.busy) return;
  const prefix = register ? "register" : "login";
  const username = $(`${prefix}-username`).value.trim();
  const password = $(`${prefix}-password`).value;
  const invitation = register ? $("register-invitation").value : "";
  const body = { username, password, ...(register && invitation ? { invitation } : {}) };
  clearCredentialInputs(); setAuthenticationBusy(true); $("auth-feedback").textContent = "正在处理…";
  try {
    const { data } = await apiRequest(register ? "/auth/register" : "/auth/login", { method: "POST", body, timeoutMs: 30000 });
    if (register) {
      if (typeof data?.id !== "string" || typeof data.username !== "string" || data.role !== "user") throw new ApiError("注册结果无效。", { kind: "response" });
      $("register-form").reset(); $("login-username").value = data.username;
      showAuth("login", "账号已创建，请登录。", true); $("login-password").focus();
    } else activateUser(data);
  } catch (error) { $("auth-feedback").textContent = authenticationError(error); $("auth-feedback").classList.remove("success"); }
  finally { clearCredentialInputs(); setAuthenticationBusy(false); }
}

async function logout() {
  if (auth.busy) return;
  if (state.busy || state.proposalBusy || state.uploading || state.ticketsBusy || [...state.proposals.values()].some((record) => record.busy)) {
    notify("请等待当前操作完成后再退出。", "error"); return;
  }
  if (unresolvedProposals() && !window.confirm("当前有待处理或结果未确认的提案。退出会清除本页上下文，可能无法从本页恢复该提案；请先核实工单状态。仍要退出吗？")) return;
  setAuthenticationBusy(true);
  let message = "已退出登录。";
  try {
    await apiRequest("/auth/logout", { method: "POST", timeoutMs: 15000 });
  } catch (error) {
    message = error.status === 401 ? "会话已失效，请重新登录。" : "退出结果尚未确认，本页数据已清除；请关闭浏览器或稍后核对登录状态。";
  } finally {
    resetWorkspace(); $("login-form").reset(); $("register-form").reset();
    clearCredentialInputs(); setAuthenticationBusy(false); showAuth("login", message);
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function actionButton(label, className, handler) {
  const button = element("button", className, label);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function badge(status) {
  return element("span", `status-badge status-${status}`, statusLabels[status] || status);
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date) : "时间未知";
}

function notify(text, kind = "info") {
  const toast = element("div", `toast toast-${kind}`, text);
  $("toast-root").append(toast);
  setTimeout(() => toast.remove(), 6000);
}

function lineIcon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.65", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" })) svg.setAttribute(name, value);
  const paths = kind === "user" ? ["M20 21a8 8 0 0 0-16 0", "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0"]
    : kind === "progress" ? ["M12 8v4l2.5 1.5", "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0"]
    : ["M12 8v4", "M12 16h.01", "M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0"];
  for (const d of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d); svg.append(path);
  }
  return svg;
}

function inlineNotice(text, { tone = "muted", className = "", role = "status" } = {}) {
  const box = element("div", `inline-notice notice-${tone}${className ? ` ${className}` : ""}`);
  box.setAttribute("role", role);
  box.setAttribute("aria-live", role === "alert" ? "assertive" : "polite");
  const icon = lineIcon(tone === "progress" ? "progress" : "info"); icon.classList.add("notice-icon");
  const content = element("div", "notice-content");
  content.append(element("p", "notice-text", text)); box.append(icon, content);
  return box;
}

function errorNode(error) {
  const uncertain = error.meta?.outcome === "uncertain" || error.meta?.jobStatus === "unknown";
  const box = inlineNotice(error.message || "操作未能完成。", {
    tone: uncertain ? "warning" : "error", className: "error-note", role: "alert",
  });
  const content = box.querySelector(".notice-content");
  if (error.meta?.runId) content.append(element("p", "request-id", `请求编号：${error.meta.runId}`));
  if (error.meta?.jobId && !error.message?.includes(error.meta.jobId)) {
    content.append(element("p", "request-id", `任务编号：${error.meta.jobId}`));
  }
  return box;
}

function openDetail(title, content) {
  const requestId = ++state.detailRequest;
  $("detail-title").textContent = title;
  $("detail-content").replaceChildren(content);
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  return requestId;
}

function setView(view) {
  if (!views[view]) return;
  state.view = view;
  document.querySelectorAll(".view").forEach((node) => { node.hidden = node.id !== `view-${view}`; });
  document.querySelectorAll("[data-view]").forEach((node) => {
    const active = node.dataset.view === view;
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page"); else node.removeAttribute("aria-current");
  });
  $("view-title").textContent = views[view][0];
  $("view-description").textContent = views[view][1];
  $("evidence-panel").hidden = view !== "chat";
  $("evidence-toggle").hidden = view !== "chat";
  if (view === "tickets" && !state.ticketsLoaded) loadTickets();
  if (view === "documents" && PUBLIC_BUILD && auth.user) loadDocuments();
}

async function loadDocuments() {
  const epoch = state.epoch;
  const target = $("public-documents-list");
  target.replaceChildren(element("div", "empty-state", "正在读取资料…"));
  try {
    const { data } = await apiRequest("/documents", { timeoutMs: 15000 });
    if (epoch !== state.epoch) return;
    if (!Array.isArray(data.documents)) throw new ApiError("资料列表格式无效。", { kind: "response" });
    target.replaceChildren();
    if (!data.documents.length) target.append(element("div", "empty-state", "知识库暂无资料。"));
    for (const doc of data.documents) {
      if (typeof doc.source_name !== "string" || !/^[a-f0-9-]{36}$/i.test(doc.document_id || "") || !Number.isSafeInteger(doc.chunk_count)) {
        throw new ApiError("资料列表格式无效。", { kind: "response" });
      }
      const row = element("article", "upload-record");
      row.append(element("strong", "", doc.source_name), element("span", "", `${doc.chunk_count} 个片段`));
      if (auth.user?.role === "admin") {
        const remove = actionButton("删除资料", "button button-quiet", async () => {
          if (!window.confirm(`确认删除“${doc.source_name}”及其索引？`)) return;
          remove.disabled = true;
          try {
            await apiRequest(`/documents/${doc.document_id}`, { method: "DELETE", timeoutMs: 90000 });
            if (epoch === state.epoch) await loadDocuments();
          } catch (error) {
            if (epoch === state.epoch) { notify(error.message || "删除未完成，请检查后再试。", "error"); remove.disabled = false; }
          }
        });
        row.append(remove);
      }
      target.append(row);
    }
  } catch (error) { if (epoch === state.epoch) target.replaceChildren(errorNode(error)); }
}

function toggleEvidence(open) {
  const panel = $("evidence-panel");
  const shouldOpen = open ?? !panel.classList.contains("is-open");
  panel.classList.toggle("is-open", shouldOpen);
  panel.classList.toggle("mobile-open", shouldOpen);
  $("evidence-toggle").setAttribute("aria-expanded", String(shouldOpen));
}

function scrollChat(force = false) {
  const chat = $("chat-messages");
  if (force || chat.scrollHeight - chat.clientHeight - chat.scrollTop < 180) chat.scrollTop = chat.scrollHeight;
}

function addMessage(role, content, pending = false) {
  $("chat-welcome").hidden = true;
  const article = element("article", `message ${role}`);
  article.setAttribute("aria-label", role === "user" ? "你的消息" : "知行助手的消息");
  const avatar = element("div", `message-avatar${role === "assistant" ? " assistant-seal" : ""}`);
  avatar.setAttribute("aria-hidden", "true");
  avatar.append(role === "user" ? lineIcon("user") : element("span", "seal-character", "知"));
  const block = element("div", "message-block");
  // User identity is already conveyed by the round icon and alignment, without a second label.
  if (role === "assistant") {
    const label = element("div", "message-label");
    label.append(element("strong", "assistant-name", "知行助手"), element("small", "assistant-product", "Project Copilot"));
    block.append(label);
  }
  // Assistant prose and system hints share the chat paper, never a user-style bubble.
  const body = element("div", role === "assistant" ? "message-body" : "message-bubble");
  const meta = element("div", "message-meta");
  if (pending) {
    body.append(inlineNotice("正在整理资料与处理结果，请稍候…", { tone: "progress" }));
  } else body.append(role === "assistant" ? renderMarkdown(content) : element("div", "message-text", content));
  block.append(body, meta); article.append(avatar, block);
  $("chat-messages").append(article); scrollChat(true);
  return { article, body, meta };
}

function renderRunDetails(data, meta) {
  const details = element("details", "run-details");
  details.append(element("summary", "", "运行详情"));
  const list = element("dl", "detail-list");
  const rows = [];
  if (meta.runId) rows.push(["请求编号", meta.runId]);
  if (meta.traceId) rows.push(["追踪编号", meta.traceId], ["追踪导出", meta.traceExported ? "已接收" : "未确认"]);
  if (typeof data.elapsed_ms === "number") rows.push(["处理耗时", `${(data.elapsed_ms / 1000).toFixed(1)} 秒`]);
  if (typeof data.model_calls === "number") rows.push(["模型调用", `${data.model_calls} 次`]);
  const usage = data.usage || data.generation?.usage;
  if (usage && Number.isInteger(usage.total_tokens)) rows.push(["Token 用量", `${usage.total_tokens}`]);
  if (data.failure_type) rows.push(["失败类型", data.failure_type]);
  for (const [key, value] of rows) list.append(element("dt", "", key), element("dd", "", value));
  details.append(list);
  return details;
}

function sourcesFor(data, mode) {
  if (mode === "rag") return (data.citations || []).map((item) => ({ kind: "knowledge", id: `knowledge:${item.metadata?.chunk_id || item.number}`, data: item }));
  return Array.isArray(data.sources) ? data.sources : [];
}

function sourceName(source) {
  if (source.kind === "knowledge") return source.data?.metadata?.source_name || "知识片段";
  if (source.kind === "ticket") return source.data?.title || "工单记录";
  return source.data?.payload?.title || "工单提案";
}

function showSource(source) {
  const content = element("div", "source-detail");
  const data = source.data || {};
  if (source.kind === "knowledge") {
    content.append(element("div", "detail-eyebrow", data.metadata?.page != null ? `原文 · 第 ${data.metadata.page + 1} 页` : "知识原文"));
    content.append(element("div", "source-original", data.text || "未返回原文。"));
  } else if (source.kind === "ticket") {
    content.append(element("div", "source-original", data.description || "未返回描述。"));
    if (typeof data.id === "string") content.append(actionButton("查看工单详情", "button button-secondary", () => openTicket(data.id)));
  } else {
    content.append(element("div", "source-original", data.payload?.description || "请在对话中的提案卡片查看并确认。"));
  }
  openDetail(sourceName(source), content);
}

function showEvidence(sources, data = {}, meta = {}) {
  $("evidence-title").textContent = sources.length ? `本次回答依据 · ${sources.length}` : "回答依据";
  const target = $("evidence-content");
  target.replaceChildren();
  if (!sources.length) {
    target.append(element("div", "empty-state", "本次未返回可用依据。你可以补充资料，或提供更具体的工单线索。"));
  }
  sources.forEach((source, index) => {
    const card = actionButton("", "evidence-card", () => showSource(source));
    card.append(element("span", "source-kind", `${String(index + 1).padStart(2, "0")} · ${({ knowledge: "知识资料", ticket: "真实工单", proposal: "待确认提案" })[source.kind] || "来源"}`));
    card.append(element("strong", "", sourceName(source)));
    card.append(element("span", "source-excerpt", (source.data?.text || source.data?.description || source.data?.payload?.description || "查看来源详情").slice(0, 160)));
    target.append(card);
  });
  if (Array.isArray(data.events) && data.events.length) {
    const group = element("div", "tool-events");
    group.append(element("h3", "", "本次处理"));
    const names = { get_ticket: "查看工单", search_tickets: "查询工单", search_knowledge: "检索知识资料", propose_ticket: "生成待确认提案" };
    data.events.forEach((event) => group.append(element("div", `tool-event ${event.status === "ok" ? "ok" : "error"}`, `${event.status === "ok" ? "✓" : "!"} ${names[event.name] || "工具调用"} · ${event.status === "ok" ? "已完成" : "未完成"}`)));
    target.append(group);
  }
  if (meta.runId || meta.traceId) target.append(renderRunDetails(data, meta));
}

function updateComposer() {
  $("stop-wait").hidden = !state.busy;
  $("send-button").hidden = state.busy;
  $("send-button").disabled = state.busy || !$("question-input").value.trim();
  $("question-input").disabled = state.busy;
  $("chat-mode").disabled = state.busy;
  $("new-chat").disabled = state.busy || state.proposalBusy;
  $("input-count").textContent = `${$("question-input").value.length} / 2000`;
}

// Browser persistence contains identifiers only; never questions, answers or bearer credentials.
function recoveryKey() { return PUBLIC_BUILD && auth.user ? `pc.jobs.v1.${auth.user.id}` : null; }
function savedJobs() {
  try {
    const key = recoveryKey(); if (!key) return [];
    const parsed = JSON.parse(localStorage.getItem(key) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(item.id) &&
      ["agent", "rag"].includes(item.kind) && Number.isSafeInteger(item.createdAt) && item.createdAt > Date.now() - 7 * 86400000).slice(-8) : [];
  } catch { return []; }
}
function saveJob(record) {
  try {
    const key = recoveryKey(); if (!key) return;
    const list = savedJobs().filter((item) => item.id !== record.id);
    // Do not persist streamed text, context tokens or an unverified answer.
    list.push({ id: record.id, kind: record.kind, createdAt: record.createdAt, lastSequence: 0 });
    localStorage.setItem(key, JSON.stringify(list.slice(-8)));
  } catch { /* Private browsing can disable storage; the current page still works. */ }
  renderRecovery();
}
function removeSavedJob(id) {
  try {
    const key = recoveryKey(); if (!key) return;
    const list = savedJobs().filter((record) => record.id !== id);
    if (list.length) localStorage.setItem(key, JSON.stringify(list)); else localStorage.removeItem(key);
  } catch {}
  renderRecovery();
}
function clearSavedJobs() { try { const key = recoveryKey(); if (key) localStorage.removeItem(key); } catch {} }
function renderRecovery() {
  const target = $("job-recovery"); if (!target) return;
  target.replaceChildren(); const jobs = savedJobs(); target.hidden = !jobs.length;
  if (!jobs.length) return;
  const summary = inlineNotice("停止等待或刷新不会重新执行。以下操作只查询原任务。", { className: "recovery-notice" });
  summary.querySelector(".notice-content").prepend(element("strong", "", "可恢复的任务"));
  target.append(summary);
  for (const record of jobs) {
    const row = element("div", "job-recovery-row");
    row.append(element("span", "job-identifier", `${record.kind === "rag" ? "知识问答" : "智能协作"} · ${record.id}`));
    const resume = actionButton("查询 / 恢复等待", "button button-quiet", () => resumeWaiting(record));
    resume.disabled = state.busy; row.append(resume); target.append(row);
  }
}
function startWaiting(message, mode) {
  const wait = { controller: new AbortController(), message, mode, sequence: 0, draft: "" };
  state.activeWait = wait; state.busy = true; updateComposer(); renderRecovery();
  message.body.replaceChildren();
  wait.progress = inlineNotice("任务正在提交…", { tone: "progress", className: "job-progress" });
  wait.progressText = wait.progress.querySelector(".notice-text");
  wait.preview = element("div", "stream-preview");
  wait.preview.hidden = true;
  message.body.append(wait.progress, wait.preview);
  return wait;
}
function updateJobEvent(wait, event) {
  if (state.activeWait !== wait || wait.controller.signal.aborted) return;
  if (Number.isSafeInteger(event.id)) {
    if (event.id <= wait.sequence && !(event.event === "reset" && event.data.cursor_reset === true)) return;
    wait.sequence = event.id;
  }
  if (["progress", "transport"].includes(event.event) && typeof event.data.label === "string") wait.progressText.textContent = event.data.label;
  if (["reset", "snapshot"].includes(event.event) && event.data.cursor_reset) {
    wait.draft = ""; wait.preview.replaceChildren(); wait.preview.hidden = true;
    wait.progressText.textContent = "部分实时内容已过期，继续查询最终结果";
  }
  if (event.event === "delta" && wait.mode === "rag" && typeof event.data.text === "string") {
    // Plain text while incomplete: no reparsing partial Markdown on each token.
    wait.draft = (wait.draft + event.data.text).slice(0, 100000);
    wait.preview.hidden = false; wait.preview.textContent = wait.draft;
    wait.progressText.textContent = "正在生成回答草稿 · 最终校验尚未完成";
    scrollChat();
  }
  if (event.event === "terminal") wait.progressText.textContent = "正在核对最终任务结果…";
}
function stopWaiting() {
  const wait = state.activeWait; if (!wait) return;
  wait.controller.abort(); state.activeWait = null; state.busy = false;
  wait.message.body.replaceChildren(inlineNotice("已停止等待，后台任务可能继续执行。请从“可恢复的任务”查询原任务，不要重复提交。", { className: "waiting-stopped" }));
  updateComposer(); renderRecovery();
}
async function displayAnswer(message, data, meta, mode, context, question, epoch) {
    if (typeof data.answer !== "string" || typeof data.status !== "string") throw new ApiError("服务未返回可识别的回答。", { meta, kind: "response" });
    message.body.replaceChildren(renderMarkdown(data.answer));
    if (["failed", "budget_exhausted"].includes(data.status)) {
      message.body.append(inlineNotice(data.status === "budget_exhausted" ? "本次处理达到执行预算，未完成的操作不会被视为成功。" : "本次处理未完成，可查看运行详情定位原因。", { tone: "warning", className: "error-note", role: "alert" }));
    } else {
      if (question) state.history.push({ role: "user", content: question }, { role: "assistant", content: data.answer });
      state.history = boundHistory(state.history);
    }
    const sources = sourcesFor(data, mode);
    const sourceControl = actionButton(sources.length ? `查看 ${sources.length} 条依据` : "查看处理详情", "source-button", () => {
      showEvidence(sources, data, meta); toggleEvidence(true);
    });
    message.meta.append(sourceControl);
    if (typeof data.elapsed_ms === "number") message.meta.append(element("span", "", `${(data.elapsed_ms / 1000).toFixed(1)} 秒`));
    showEvidence(sources, data, meta);
    if (!context && sources.some((item) => item.kind === "proposal")) message.body.append(element("p", "proposal-note", "此回答包含待确认提案。刷新后上下文凭据已清除，请先核对工单与提案状态；系统不会自动确认或重新创建。"));
    if (context) for (const source of sources.filter((item) => item.kind === "proposal")) {
      await attachProposal(message.body, source.data, context);
      if (epoch !== state.epoch) return;
    }
    if (meta.runId) message.meta.append(renderRunDetails(data, meta));
}
async function resumeWaiting(record) {
  if (state.busy) return;
  const epoch = state.epoch;
  const message = addMessage("assistant", "", true), wait = startWaiting(message, record.kind);
  try {
    const { data, meta } = await resumePublicJob(record.id, { signal: wait.controller.signal,
      isCurrent: () => epoch === state.epoch, onEvent: (event) => updateJobEvent(wait, event) });
    if (state.activeWait !== wait || epoch !== state.epoch) return;
    await displayAnswer(message, data, meta, record.kind, state.jobContexts.get(record.id) || null, null, epoch);
    state.jobContexts.delete(record.id);
    removeSavedJob(record.id);
  } catch (error) {
    if (state.activeWait === wait && epoch === state.epoch) message.body.replaceChildren(errorNode(error));
  } finally {
    if (state.activeWait === wait && epoch === state.epoch) { state.activeWait = null; state.busy = false; updateComposer(); renderRecovery(); scrollChat(); }
  }
}
async function sendQuestion(event) {
  event.preventDefault();
  const epoch = state.epoch;
  const question = $("question-input").value.trim();
  if (!question || state.busy || question.length > 2000) return;
  const mode = $("chat-mode").value, history = boundHistory(state.history);
  addMessage("user", question);
  const message = addMessage("assistant", "", true), wait = startWaiting(message, mode);
  $("question-input").value = ""; updateComposer();
  let context = null, submitted = null;
  try {
    if (mode === "agent") context = await session.ensure();
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    const payload = mode === "agent" ? { question, history } : { question };
    const headers = context ? ContextSession.headers(context) : {};
    const { data, meta } = PUBLIC_BUILD
      ? await runPublicJob(mode, payload, { headers, signal: wait.controller.signal,
          isCurrent: () => epoch === state.epoch,
          onSubmitted: (record) => { if (epoch === state.epoch) { submitted = record; if (context) state.jobContexts.set(record.id, context); saveJob(record); } },
          onEvent: (event) => updateJobEvent(wait, event) })
      : await apiRequest(mode === "agent" ? "/agent/ask" : "/rag/ask", {
          method: "POST", body: payload, headers, signal: wait.controller.signal, timeoutMs: mode === "agent" ? 330000 : 180000,
        });
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    await displayAnswer(message, data, meta, mode, context, question, epoch);
    if (submitted) { removeSavedJob(submitted.id); state.jobContexts.delete(submitted.id); }
  } catch (error) {
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    message.body.replaceChildren(errorNode(error));
    if (error.status === 403) session.clear();
  } finally {
    if (epoch === state.epoch && state.activeWait === wait) {
      state.activeWait = null; state.busy = false; updateComposer(); renderRecovery(); scrollChat(); $("question-input").focus();
    }
  }
}

function applyAction(record, value) {
  const action = validateAction(value, record.context);
  if (record.action && (action.action_id !== record.action.action_id || action.payload_fingerprint !== record.action.payload_fingerprint ||
      action.payload.title !== record.action.payload.title || action.payload.description !== record.action.payload.description)) {
    record.blocked = true;
    throw new ApiError("提案内容与已预览版本不一致，已停止确认。请重新提出操作。", { kind: "response" });
  }
  record.action = action;
  record.uncertain = false;
}

async function attachProposal(container, source, context) {
  if (typeof source?.action_id !== "string") throw new ApiError("未返回有效提案编号。", { kind: "response" });
  const existing = state.proposals.get(source.action_id);
  if (existing) { container.append(existing.node); return; }
  const record = { id: source.action_id, context, action: null, busy: true, error: null, uncertain: false, blocked: false, node: element("section", "proposal-card") };
  state.proposals.set(record.id, record); container.append(record.node); renderProposal(record);
  await refreshProposal(record);
}

async function refreshProposal(record) {
  const epoch = state.epoch;
  record.busy = true; record.error = null; renderProposal(record);
  try {
    const { data } = await apiRequest(`/actions/${encodeURIComponent(record.id)}`, { headers: ContextSession.headers(record.context), timeoutMs: 15000 });
    if (epoch !== state.epoch) return;
    if (data.action_id !== record.id) throw new ApiError("提案编号与请求不一致。", { kind: "response" });
    applyAction(record, data);
    if (data.status === "completed") state.ticketsLoaded = false;
  } catch (error) {
    if (epoch !== state.epoch) return;
    record.error = error;
    if (error.status === 403 || error.kind === "response") record.blocked = true;
  } finally { if (epoch === state.epoch) { record.busy = false; renderProposal(record); } }
}

async function decideProposal(record, approve) {
  const epoch = state.epoch;
  if (record.busy || record.blocked || record.uncertain || record.action?.status !== "pending" || Date.parse(record.action.expires_at) <= Date.now()) return;
  record.busy = true; record.error = null; renderProposal(record);
  try {
    const { data } = await apiRequest(`/actions/${encodeURIComponent(record.id)}/${approve ? "confirm" : "reject"}`, {
      method: "POST", headers: ContextSession.headers(record.context),
      body: { payload_fingerprint: record.action.payload_fingerprint }, timeoutMs: 20000,
    });
    if (epoch !== state.epoch) return;
    applyAction(record, data);
    if (data.status === "completed") {
      state.ticketsLoaded = false;
      notify("工单已创建，可以查看实际记录。", "success");
    } else notify(statusLabels[data.status] || "已更新提案状态。");
  } catch (error) {
    if (epoch !== state.epoch) return;
    record.error = error;
    record.uncertain = ["network", "timeout", "response"].includes(error.kind);
    if (error.status === 403 || error.status === 409 || error.kind === "response") record.blocked = true;
  } finally { if (epoch === state.epoch) { record.busy = false; renderProposal(record); } }
}

function renderProposal(record) {
  const card = record.node;
  card.replaceChildren();
  const action = record.action;
  const expired = action?.status === "pending" && Date.parse(action.expires_at) <= Date.now();
  const effectiveStatus = expired ? "expired" : action?.status;
  const heading = element("div", "proposal-heading");
  heading.append(element("strong", "", "工单提案"));
  if (effectiveStatus) heading.append(badge(effectiveStatus));
  card.append(heading);
  if (action) {
    card.append(element("h3", "proposal-title", action.payload.title), element("div", "proposal-description", action.payload.description));
    if (effectiveStatus === "pending") card.append(element("p", "proposal-note", `确认后才会创建工单 · 有效至 ${formatDate(action.expires_at)}`));
    if (effectiveStatus === "expired") card.append(element("p", "proposal-note", "此提案已过期。请重新提出工单，旧提案不会创建记录。"));
    if (effectiveStatus === "cancelled") card.append(element("p", "proposal-note", "你已拒绝此提案，未通过此提案创建工单。"));
    if (effectiveStatus === "completed") {
      card.append(element("p", "ticket-identifier", `工单编号：${action.ticket_id}`));
      card.append(actionButton("查看已创建工单", "button button-secondary", () => openTicket(action.ticket_id)));
    }
  }
  if (record.busy) card.append(element("p", "loading", action ? "正在核对操作结果…" : "正在读取后端提案预览…"));
  if (record.error) card.append(errorNode(record.error));
  const actions = element("div", "proposal-actions");
  if (effectiveStatus === "pending") {
    const confirm = actionButton("确认创建工单", "button button-primary", () => decideProposal(record, true));
    const reject = actionButton("拒绝提案", "button button-secondary", () => decideProposal(record, false));
    confirm.disabled = reject.disabled = record.busy || record.uncertain || record.blocked;
    actions.append(confirm, reject);
  }
  if (record.error || !action) {
    const refresh = actionButton("查询最新状态", "button button-secondary", () => refreshProposal(record));
    refresh.disabled = record.busy || record.error?.status === 403 || record.error?.kind === "response";
    actions.append(refresh);
  }
  if (actions.childElementCount) card.append(actions);
}

function setFile(file) {
  if (PUBLIC_BUILD && auth.user?.role !== "admin") return;
  if (state.uploading) return;
  if (!file) { state.selectedFile = null; renderFile(); return; }
  const suffix = file.name.split(".").pop()?.toLowerCase();
  if (PUBLIC_BUILD && suffix === "pdf") { notify("公开 PDF 上传尚未完成安全验收，请先使用 TXT 或 Markdown。", "error"); return; }
  if (!["txt", "md", "pdf"].includes(suffix)) { notify("请选择 TXT、Markdown 或 PDF 文件。", "error"); return; }
  if (!file.size) { notify("文件内容不能为空。", "error"); return; }
  if (state.uploadLimit && file.size > state.uploadLimit) { notify("文件超过当前服务允许的大小。", "error"); return; }
  state.selectedFile = file; $("upload-feedback").replaceChildren(); renderFile();
}

function renderFile() {
  $("selected-file").hidden = !state.selectedFile;
  $("selected-file").textContent = state.selectedFile ? `${state.selectedFile.name} · ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(state.selectedFile.size / 1024)} KB` : "";
  $("clear-file").hidden = !state.selectedFile;
  $("clear-file").disabled = state.uploading;
  $("upload-button").disabled = state.uploading || !state.selectedFile;
  $("upload-button").textContent = state.uploading ? "正在上传并整理资料…" : "上传并加入知识库";
  $("choose-file").disabled = state.uploading;
}

async function uploadFile() {
  const epoch = state.epoch;
  if (PUBLIC_BUILD && auth.user?.role !== "admin") return;
  if (!state.selectedFile || state.uploading) return;
  state.uploading = true; renderFile(); $("upload-feedback").replaceChildren();
  const file = state.selectedFile;
  const form = new FormData(); form.append("file", file);
  try {
    const { data } = PUBLIC_BUILD
      ? await runPublicJob("upload", form, { isCurrent: () => epoch === state.epoch })
      : await apiRequest("/documents/upload", { method: "POST", body: form, timeoutMs: 300000 });
    if (epoch !== state.epoch) return;
    if (data.status !== "indexed" || !data.document?.document_id || data.mode !== "HYBRID_RERANK") throw new ApiError("入库结果未通过校验，请核对服务状态。", { kind: "response" });
    $("upload-records").querySelector(".empty-state")?.remove();
    const record = element("article", "upload-record");
    record.append(element("strong", "", data.document.source_name || file.name), element("span", "", `已加入知识库 · ${data.chunk_count} 个片段`));
    record.append(actionButton("围绕这份资料提问", "source-button", () => {
      setView("chat"); $("chat-mode").value = "rag"; $("question-input").value = `请根据《${data.document.source_name || file.name}》概括主要内容。`.slice(0, 2000); updateComposer(); $("question-input").focus();
    }));
    $("upload-records").prepend(record);
    $("upload-feedback").append(element("p", "success-note", "资料已入库，现在可以在智能协作中提问。"));
    if (PUBLIC_BUILD) await loadDocuments();
    state.selectedFile = null; $("file-input").value = "";
    notify("资料已加入知识库。", "success");
  } catch (error) { if (epoch === state.epoch) $("upload-feedback").append(errorNode(error)); }
  finally { if (epoch === state.epoch) { state.uploading = false; renderFile(); } }
}

async function loadTickets() {
  const requestNumber = ++state.ticketsRequest;
  state.ticketsBusy = true; $("tickets-feedback").replaceChildren();
  $("tickets-list").replaceChildren(element("div", "empty-state loading", "正在读取工单…"));
  $("tickets-prev").disabled = $("tickets-next").disabled = true;
  const params = new URLSearchParams({ limit: String(state.ticketLimit), offset: String(state.ticketOffset) });
  const query = $("ticket-query").value.trim();
  if (query) params.set("q", query);
  if ($("ticket-status").value) params.set("status", $("ticket-status").value);
  try {
    const { data } = await apiRequest(`/tickets?${params}`, { timeoutMs: 15000 });
    if (requestNumber !== state.ticketsRequest) return;
    if (!Array.isArray(data.items)) throw new ApiError("工单列表结果无效。", { kind: "response" });
    $("tickets-list").replaceChildren();
    if (!data.items.length) $("tickets-list").append(element("div", "empty-state", "没有找到匹配的工单。试试其他关键词，或提出一张新工单。"));
    data.items.forEach((ticket) => {
      const card = actionButton("", "ticket-card", () => openTicket(ticket.id));
      const head = element("div", "ticket-card-heading"); head.append(element("strong", "", ticket.title), badge(ticket.status));
      card.append(head, element("p", "ticket-excerpt", ticket.description));
      const meta = element("div", "ticket-meta"); meta.append(element("span", "", `#${String(ticket.id).slice(0, 8)}`), element("span", "", `创建于 ${formatDate(ticket.created_at)}`));
      card.append(meta); $("tickets-list").append(card);
    });
    state.ticketsLoaded = true;
    $("tickets-page").textContent = `第 ${Math.floor(state.ticketOffset / state.ticketLimit) + 1} 页 · 本页 ${data.items.length} 条`;
    $("tickets-prev").disabled = state.ticketOffset === 0;
    $("tickets-next").disabled = data.items.length < state.ticketLimit || state.ticketOffset + state.ticketLimit > 10000;
  } catch (error) {
    if (requestNumber !== state.ticketsRequest) return;
    $("tickets-list").replaceChildren(); $("tickets-feedback").append(errorNode(error));
    $("tickets-page").textContent = "暂未读取到工单";
    state.ticketsLoaded = false;
  } finally { if (requestNumber === state.ticketsRequest) state.ticketsBusy = false; }
}

async function openTicket(id) {
  const content = element("div", "loading", "正在读取工单详情…");
  const requestId = openDetail("工单详情", content);
  try {
    const { data: ticket } = await apiRequest(`/tickets/${encodeURIComponent(id)}`, { timeoutMs: 15000 });
    if (state.detailRequest !== requestId || !$("detail-dialog").open) return;
    const detail = element("div", "ticket-detail");
    detail.append(badge(ticket.status), element("h3", "", ticket.title), element("div", "source-original", ticket.description));
    const list = element("dl", "detail-list");
    for (const [key, value] of [["工单编号", ticket.id], ["创建时间", formatDate(ticket.created_at)], ["更新时间", formatDate(ticket.updated_at)]]) list.append(element("dt", "", key), element("dd", "", value));
    detail.append(list);
    $("detail-content").replaceChildren(detail);
  } catch (error) {
    if (state.detailRequest === requestId && $("detail-dialog").open) $("detail-content").replaceChildren(errorNode(error));
  }
}

async function submitProposal(event) {
  event.preventDefault();
  const epoch = state.epoch;
  if (state.proposalBusy || !$("proposal-form").reportValidity()) return;
  const payload = { title: $("proposal-title").value.trim(), description: $("proposal-description").value.trim() };
  if (!payload.title || !payload.description) { notify("请填写有效的工单标题和详细描述。", "error"); return; }
  state.proposalBusy = true; $("proposal-submit").disabled = true; updateComposer();
  let feedback = $("proposal-form").querySelector(".proposal-feedback");
  if (!feedback) { feedback = element("div", "proposal-feedback"); $("proposal-form").append(feedback); }
  feedback.replaceChildren();
  try {
    const context = await session.ensure();
    if (epoch !== state.epoch) return;
    const { data, meta } = await apiRequest("/actions/preview", { method: "POST", body: payload, headers: ContextSession.headers(context), timeoutMs: 15000 });
    if (epoch !== state.epoch) return;
    validateAction(data, context);
    $("proposal-dialog").close(); $("proposal-form").reset(); setView("chat");
    addMessage("user", `提出工单：${payload.title}`);
    const message = addMessage("assistant", "工单提案已准备好，请核对内容后决定是否创建。");
    await attachProposal(message.body, data, context);
    if (epoch !== state.epoch) return;
    message.meta.append(renderRunDetails({}, meta));
    scrollChat();
  } catch (error) { if (epoch === state.epoch) { feedback.append(errorNode(error)); if (error.status === 403) session.clear(); } }
  finally { if (epoch === state.epoch) { state.proposalBusy = false; $("proposal-submit").disabled = false; updateComposer(); } }
}

async function checkService() {
  const epoch = state.epoch;
  try {
    const { data } = await apiRequest("/health", { timeoutMs: 10000 });
    if (epoch !== state.epoch) return;
    if (data.status !== "ok") throw new Error();
    $("service-status").textContent = "服务已连接";
    $("service-status").classList.add("connected");
  } catch {
    if (epoch !== state.epoch) return;
    $("service-status").textContent = "服务暂未连接";
    $("service-status").classList.remove("connected");
  }
}

function bindEvents() {
  $("auth-show-login").addEventListener("click", () => { if (!auth.busy) { clearCredentialInputs(); showAuth("login"); } });
  $("auth-show-register").addEventListener("click", () => { if (!auth.busy) { clearCredentialInputs(); showAuth("register"); } });
  $("login-form").addEventListener("submit", (event) => submitAuthentication(event, false));
  $("register-form").addEventListener("submit", (event) => submitAuthentication(event, true));
  $("logout-button").addEventListener("click", logout);
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("refresh-documents").addEventListener("click", loadDocuments);
  document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => {
    if (state.busy) return;
    $("question-input").value = button.dataset.prompt; updateComposer(); $("question-input").focus();
  }));
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => $(button.dataset.closeDialog).close()));
  $("composer-form").addEventListener("submit", sendQuestion);
  $("stop-wait").addEventListener("click", stopWaiting);
  $("question-input").addEventListener("input", updateComposer);
  $("question-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); $("composer-form").requestSubmit(); }
  });
  $("new-chat").addEventListener("click", () => {
    if (state.busy || state.proposalBusy) return;
    const unresolved = [...state.proposals.values()].some((record) => record.action?.status === "pending" && Date.parse(record.action.expires_at) > Date.now());
    if (unresolved) { notify("请先确认或拒绝当前提案，再开始新对话。"); return; }
    $("chat-messages").querySelectorAll(".message").forEach((node) => node.remove());
    state.history = []; state.proposals.clear(); session.clear(); $("chat-welcome").hidden = false;
    showEvidence([]); setView("chat"); $("question-input").focus();
  });
  $("evidence-toggle").addEventListener("click", () => toggleEvidence());
  $("evidence-close")?.addEventListener("click", () => toggleEvidence(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") toggleEvidence(false); });
  $("choose-file").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (event) => setFile(event.target.files[0]));
  $("clear-file").addEventListener("click", () => { $("file-input").value = ""; setFile(null); });
  $("upload-button").addEventListener("click", uploadFile);
  $("upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); $("upload-zone").classList.add("dragging"); });
  $("upload-zone").addEventListener("dragleave", () => $("upload-zone").classList.remove("dragging"));
  $("upload-zone").addEventListener("drop", (event) => {
    event.preventDefault(); $("upload-zone").classList.remove("dragging");
    if (event.dataTransfer.files.length !== 1) { notify("每次请选择一个文件。", "error"); return; }
    setFile(event.dataTransfer.files[0]);
  });
  $("tickets-form").addEventListener("submit", (event) => { event.preventDefault(); state.ticketOffset = 0; loadTickets(); });
  $("ticket-status").addEventListener("change", () => { state.ticketOffset = 0; loadTickets(); });
  $("tickets-prev").addEventListener("click", () => { state.ticketOffset = Math.max(0, state.ticketOffset - state.ticketLimit); loadTickets(); });
  $("tickets-next").addEventListener("click", () => { state.ticketOffset += state.ticketLimit; loadTickets(); });
  $("new-ticket").addEventListener("click", () => { $("proposal-dialog").showModal(); $("proposal-title").focus(); });
  $("proposal-form").addEventListener("submit", submitProposal);
  $("service-status").addEventListener("click", checkService);
  setInterval(() => {
    for (const record of state.proposals.values()) if (record.action?.status === "pending" && Date.parse(record.action.expires_at) <= Date.now()) renderProposal(record);
  }, 15000);
}

async function initializeServices() {
  const epoch = state.epoch;
  checkService();
  try {
    const { data } = await apiRequest("/ui/config", { timeoutMs: 10000 });
    if (epoch !== state.epoch) return;
    if (Number.isInteger(data.upload_max_bytes) && data.upload_max_bytes > 0) {
      state.uploadLimit = PUBLIC_BUILD ? Math.min(data.upload_max_bytes, 4 * 1024 * 1024) : data.upload_max_bytes;
      $("upload-limit").textContent = `支持 ${PUBLIC_BUILD ? "TXT、Markdown" : "TXT、Markdown、PDF"} · 单文件最多 ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(state.uploadLimit / 1024 / 1024)} MB`;
    }
  } catch { if (epoch === state.epoch) $("upload-limit").textContent = `支持 ${PUBLIC_BUILD ? "TXT、Markdown" : "TXT、Markdown、PDF"} · 文件大小由服务校验`; }
}

async function start() {
  bindEvents(); updateComposer(); renderFile();
  if (!PUBLIC_BUILD) {
    $("app-shell").hidden = false; $("workspace-skip").hidden = false;
    initializeServices();
    return;
  }
  $("api-docs-link").hidden = true;
  $("workspace-local-footer").hidden = true;
  onAuthenticationFailure(() => {
    if (!auth.user) return;
    const unresolved = unresolvedProposals();
    resetWorkspace(); clearCredentialInputs();
    showAuth("login", unresolved ?
      "登录已失效。本页提案已清除，请重新登录后核对工单状态；系统没有自动重复确认。" :
      "登录已失效，请重新登录。");
  });
  showAuth("login", "正在检查登录状态…"); setAuthenticationBusy(true);
  try {
    const { data } = await apiRequest("/auth/me", { timeoutMs: 15000 });
    activateUser(data);
  } catch (error) {
    setCsrfToken(null);
    showAuth("login", error.status === 401 ? "请登录或注册后继续。" : authenticationError(error));
  } finally { clearCredentialInputs(); setAuthenticationBusy(false); }
}

start();
