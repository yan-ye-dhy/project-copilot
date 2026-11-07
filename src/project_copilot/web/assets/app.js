import { apiRequest, ApiError, ContextSession, boundHistory, validateAction, runPublicJob, resumePublicJob,
  PUBLIC_BUILD, setCsrfToken, onAuthenticationFailure, personalDemoProfile, personalJobWaitOptions,
  safeFailureType, jobFailureLabel } from "./api.js";

import { renderMarkdown } from "./markdown.js";
import { recoveryRecords, jobObservation, jobStateLabel, canHideJob, setRecoveryHidden } from "./task-center.js";
import { createConversation, conversationTitle, sanitizeConversation, sanitizeSources, boundedConversations, HistoryStore, HistoryConflictError, HISTORY_LIMITS } from "./conversations.js";

const $ = (id) => document.getElementById(id);
const session = new ContextSession();
const historyStore = new HistoryStore();
let historyWrites = Promise.resolve();
const state = {
  view: "chat", busy: false, activeWait: null, jobContexts: new Map(), proposalBusy: false, history: [], selectedFile: null,
  uploading: false, uploadLimit: null, ticketOffset: 0, ticketLimit: 10,
  ticketsLoaded: false, ticketsBusy: false, ticketsRequest: 0, detailRequest: 0, proposals: new Map(), epoch: 0,
  personalDemo: null, profileStale: false, configPending: null,
  jobObservations: new Map(), jobReadErrors: new Set(), taskCenterReads: null, showHiddenJobs: false,
  conversations: [], conversation: createConversation(), historyEnabled: false, historyLoading: false,
  historySaving: false, historyMessage: "", historyRevision: null, historyConflict: false, submissionInFlight: 0,
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
  closeTaskCenter(); state.taskCenterReads?.abort(); state.taskCenterReads = null;
  closeHistory();
  state.conversations = []; state.conversation = createConversation(); state.historyEnabled = false;
  state.historyLoading = false; state.historySaving = false; state.historyMessage = ""; state.submissionInFlight = 0;
  state.historyRevision = null; state.historyConflict = false;
  state.jobObservations.clear(); state.jobReadErrors.clear(); state.showHiddenJobs = false;
  for (const id of ["account-panel", "personal-demo-meter"]) if ($(id)?.tagName === "DETAILS") $(id).open = false;
  state.epoch += 1; state.ticketsRequest += 1; state.detailRequest += 1;
  session.clear(); setCsrfToken(null); auth.user = null;
  state.history = []; state.proposals.clear(); state.jobContexts.clear(); state.selectedFile = null;
  state.personalDemo = null; state.profileStale = false; state.configPending = null; renderPersonalDemo();
  state.busy = false; state.proposalBusy = false; state.uploading = false;
  state.ticketsLoaded = false; state.ticketsBusy = false; state.ticketOffset = 0;
  $("chat-messages").querySelectorAll(".message,.history-readonly-note").forEach((node) => node.remove());
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
  $("history-search").value = "";
  showEvidence([]); toggleEvidence(false); setView("chat"); updateComposer(); renderFile(); renderHistory(); renderRecovery();
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
  initializeServices(); renderRecovery(); initializeConversations();
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
  if (state.busy || state.submissionInFlight || state.proposalBusy || state.uploading || state.ticketsBusy || [...state.proposals.values()].some((record) => record.busy)) {
    notify("请等待当前操作完成后再退出。", "error"); return;
  }
  if ((unresolvedProposals() || state.conversation.uncertainSubmission) && !window.confirm("当前有待处理提案或结果未确认的提交。退出会清除本页上下文，可能无法从本页恢复审批；请先核实原请求和工单状态。仍要退出吗？")) return;
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

function conversationSnapshots() { return boundedConversations(state.conversations); }

function touchConversation() {
  const chat = state.conversation;
  chat.updatedAt = Date.now();
  if (chat.title === "新对话") chat.title = conversationTitle(chat.messages);
  const modes = new Set(chat.messages.map((message) => message.mode));
  chat.mode = modes.size > 1 ? "mixed" : modes.values().next().value || chat.mode;
  if (chat.messages.length > HISTORY_LIMITS.messages) chat.messages.splice(0, chat.messages.length - HISTORY_LIMITS.messages);
  chat.jobs = chat.jobs.slice(-32);
  // Keep complete final messages rather than silently truncating their text.
  while (!sanitizeConversation(chat) && chat.messages.length > 1) {
    chat.messages.shift(); state.historyMessage = "已达到历史容量上限，仅保留最近的完整消息；后台记录不受影响。";
  }
  if (!state.conversations.some((item) => item.id === chat.id) && (chat.messages.length || chat.jobs.length)) state.conversations.push(chat);
  state.conversations.sort((a, b) => b.updatedAt - a.updatedAt || (a.id === chat.id ? -1 : b.id === chat.id ? 1 : 0));
  const retained = new Set(conversationSnapshots().map((item) => item.id));
  state.conversations = state.conversations.filter((item) => retained.has(item.id));
  renderHistory(); persistConversations();
}

function queueHistoryWrite(work) {
  historyWrites = historyWrites.catch(() => {}).then(work);
  return historyWrites;
}

function handleHistoryConflict(error) {
  if (!(error instanceof HistoryConflictError)) return false;
  state.historyConflict = true; state.historyEnabled = false;
  state.historyMessage = error.message;
  notify(error.message, "error"); renderHistory();
  return true;
}

function persistConversations() {
  if (!state.historyEnabled || state.historyConflict || !auth.user) return;
  const owner = auth.user.id, epoch = state.epoch, snapshot = conversationSnapshots();
  queueHistoryWrite(async () => {
    if (epoch !== state.epoch || owner !== auth.user?.id || !state.historyEnabled || state.historyConflict) return;
    try {
      const saved = await historyStore.save(owner, snapshot, state.historyRevision);
      if (epoch === state.epoch && owner === auth.user?.id) state.historyRevision = saved.revision;
    } catch (error) {
      if (epoch === state.epoch && owner === auth.user?.id) {
        if (handleHistoryConflict(error)) return;
        state.historyEnabled = false;
        state.historyMessage = "本机历史保存失败，已降级为仅本页历史；此前存储是否清理尚未核实。";
        renderHistory();
      }
    }
  });
}

async function initializeConversations() {
  const owner = auth.user?.id, epoch = state.epoch;
  state.historyMessage = "";
  if (!PUBLIC_BUILD || !owner) { renderHistory(); return; }
  state.historyLoading = true; updateComposer(); renderHistory();
  try {
    // A previous account's late result can never populate this account's page.
    await historyWrites.catch(() => {});
    const loaded = await historyStore.load(owner);
    if (epoch !== state.epoch || owner !== auth.user?.id) return;
    state.conversations = loaded.conversations; state.historyEnabled = loaded.enabled;
    state.historyRevision = loaded.revision; state.historyConflict = false;
  } catch {
    if (epoch === state.epoch) state.historyMessage = "IndexedDB不可用，安全降级为仅本页历史；刷新后不会保留正文。";
  } finally {
    if (epoch === state.epoch) { state.historyLoading = false; renderHistory(); updateComposer(); }
  }
}

async function changeHistoryPersistence() {
  const requested = $("history-persist").checked, owner = auth.user?.id, epoch = state.epoch;
  if (!owner || state.historyLoading || state.historySaving || state.historyConflict) { renderHistory(); return; }
  const warning = requested
    ? "将在此浏览器的IndexedDB保存普通对话正文和引用。内容并非端到端加密，共用设备不建议开启，请勿在对话中粘贴密钥。是否开启？"
    : "关闭保存并删除此账号在本浏览器的持久历史？本页内存历史仍保留，后台任务和费用不受影响。";
  if (!window.confirm(warning)) { renderHistory(); return; }
  state.historySaving = true; state.historyEnabled = requested; renderHistory();
  const snapshot = conversationSnapshots();
  try {
    await queueHistoryWrite(async () => {
      if (epoch !== state.epoch || owner !== auth.user?.id) return;
      if (state.historyConflict) throw new HistoryConflictError();
      const saved = await historyStore.setPersistence(owner, snapshot, requested, state.historyRevision);
      if (epoch === state.epoch && owner === auth.user?.id) state.historyRevision = saved.revision;
    });
    if (epoch !== state.epoch || owner !== auth.user?.id) return;
    state.historyMessage = requested ? "已开启本浏览器保存，仅记录展示内容。" : "已关闭并清除持久历史，本页历史仍可切换。";
  } catch (error) {
    if (epoch === state.epoch) {
      if (handleHistoryConflict(error)) return;
      state.historyEnabled = false;
      state.historyMessage = "IndexedDB操作失败，当前仅保留内存历史；未确认清除的旧数据请在浏览器网站数据中管理。";
    }
  } finally { if (epoch === state.epoch) { state.historySaving = false; renderHistory(); } }
}

async function clearPersistentHistory() {
  const owner = auth.user?.id, epoch = state.epoch;
  if (!owner || state.historySaving || state.historyLoading || state.historyConflict || !window.confirm("清空此账号在本浏览器保存的历史并关闭保存？本页会话仍保留，后台任务、工单及其他账号记录不变。")) return;
  state.historyEnabled = false; state.historySaving = true; renderHistory();
  try {
    await queueHistoryWrite(async () => {
      if (epoch !== state.epoch || owner !== auth.user?.id) return;
      if (state.historyConflict) throw new HistoryConflictError();
      const cleared = await historyStore.clear(owner, state.historyRevision);
      if (epoch === state.epoch && owner === auth.user?.id) state.historyRevision = cleared.revision;
    });
    if (epoch === state.epoch) state.historyMessage = "本机持久历史已清空；本页会话暂留内存，刷新后消失。";
  } catch (error) {
    if (epoch === state.epoch && !handleHistoryConflict(error)) state.historyMessage = "清空未能确认，请使用浏览器网站数据管理；当前不会继续保存。";
  } finally { if (epoch === state.epoch) { state.historySaving = false; renderHistory(); } }
}

let historyReturnFocus = null;
function closeHistory() { if ($("history-dialog")?.open) $("history-dialog").close(); }
function openHistory() {
  const dialog = $("history-dialog"); if (!dialog || dialog.open) return;
  closeTaskCenter(); historyReturnFocus = document.activeElement;
  renderHistory(); dialog.showModal(); $("history-toggle").setAttribute("aria-expanded", "true"); $("history-close").focus();
}

function canLeaveConversation() {
  if (state.historyLoading || state.proposalBusy || state.submissionInFlight || [...state.proposals.values()].some((record) => record.busy)) {
    notify("正在核对提交或业务操作结果，请稍候再切换，避免丢失原编号。", "error"); return false;
  }
  if (state.busy && (!PUBLIC_BUILD || !state.activeWait?.jobId)) {
    notify("正在确认任务提交结果，请稍候；不会重复提交。", "error"); return false;
  }
  const risks = [];
  if (state.busy) risks.push(`切换只停止本页等待，后台任务可能继续执行。原任务编号：${state.activeWait.jobId}。可在任务中心只读查询。`);
  else if (state.conversation.jobs.some((job) => !["succeeded", "failed"].includes(state.jobObservations.get(job.id)?.status))) risks.push("此对话仍有执行结果未核实的任务。切换不会取消或重跑任务，原编号将保留以便只读查询。");
  if (state.conversation.uncertainSubmission) risks.push("此前有提交响应未确认，不能断言后台没有执行。请先核实原请求结果，不要重复提交相同操作。");
  if (unresolvedProposals()) risks.push("存在待确认提案或结果不确定的业务操作。切换将清除本页审批上下文，旧提案只读展示，不恢复确认权限；请先核实工单状态。");
  return !risks.length || window.confirm(`${risks.join("\n")}\n仍要切换对话吗？`);
}

function switchConversation(id = null, { approved = false } = {}) {
  if (id === state.conversation.id) { closeHistory(); return true; }
  const target = id ? state.conversations.find((chat) => chat.id === id) : null;
  if (id && !target) return false;
  if (!approved && !canLeaveConversation()) return false;
  if (state.activeWait) stopWaiting();
  // Display history is deliberately independent from permission-bearing runtime state.
  session.clear(); state.jobContexts.clear(); state.proposals.clear(); state.history = [];
  state.detailRequest += 1;
  if ($("detail-dialog").open) $("detail-dialog").close();
  state.conversation = target || createConversation($("chat-mode").value);
  $("question-input").value = "";
  $("chat-mode").value = state.conversation.mode === "rag" ? "rag" : "agent";
  closeHistory(); closeTaskCenter(); setView("chat"); renderConversation(); updateComposer();
  $("question-input").focus(); renderHistory();
  return true;
}

function deleteConversation(id) {
  const chat = state.conversations.find((item) => item.id === id); if (!chat) return;
  if (!window.confirm(`删除对话“${chat.title}”？仅移除此账号的本机历史，不取消后台任务、不删除工单或退还额度。`)) return;
  if (id === state.conversation.id && !canLeaveConversation()) return;
  if (id === state.conversation.id) switchConversation(null, { approved: true });
  state.conversations = state.conversations.filter((item) => item.id !== id);
  persistConversations(); renderHistory();
}

function renderHistory() {
  const list = $("history-list"); if (!list) return;
  $("history-persist").checked = state.historyEnabled;
  $("history-persist").disabled = !auth.user || state.historyLoading || state.historySaving || state.historyConflict;
  $("history-clear").disabled = !auth.user || state.historyLoading || state.historySaving || state.historyConflict;
  $("history-status").textContent = state.historyLoading ? "正在读取此账号的本机历史…" : state.historyMessage || (state.historyEnabled ? "已开启本机保存；普通正文仅供展示，不恢复审批授权。" : "仅本页内存历史；刷新后消失。最多50个会话、每会话50条消息、30天、每账号5MiB。");
  const query = $("history-search").value.trim().toLocaleLowerCase();
  const chats = conversationSnapshots().filter((chat) => !query || chat.title.toLocaleLowerCase().includes(query));
  const focused = document.activeElement, focusId = focused?.dataset?.conversationId, focusAction = focused?.dataset?.historyAction;
  list.replaceChildren();
  if (!chats.length) list.append(element("p", "history-empty", query ? "没有匹配的对话。" : "暂无历史对话。发送问题后即可在这里返回查看。"));
  for (const chat of chats) {
    const row = element("article", "history-row"); row.dataset.conversationId = chat.id;
    if (chat.id === state.conversation.id) row.setAttribute("aria-current", "true");
    const open = actionButton("", "history-open", () => switchConversation(chat.id));
    if (chat.id === state.conversation.id) open.setAttribute("aria-current", "page");
    open.dataset.conversationId = chat.id; open.dataset.historyAction = "open";
    const title = element("strong", "history-title", chat.title); title.title = chat.title; open.append(title);
    open.setAttribute("aria-label", `打开对话：${chat.title}`); row.append(open);
    row.append(element("p", "history-meta", `${({ agent: "Agent", rag: "RAG", mixed: "Agent / RAG" })[chat.mode]} · 创建 ${formatDate(chat.createdAt)} · 更新 ${formatDate(chat.updatedAt)}${chat.id === state.conversation.id ? " · 当前" : ""}`));
    const actions = element("div", "history-actions");
    const rename = actionButton("重命名", "button button-quiet", () => {
      const value = window.prompt("重命名对话（最多60字）", chat.title);
      if (value === null) return;
      const title = value.trim().replace(/\s+/g, " ");
      if (!title || Array.from(title).length > 60) { notify("标题应为1至60个字。", "error"); return; }
      const original = state.conversations.find((item) => item.id === chat.id);
      if (original) { original.title = title; original.updatedAt = Date.now(); persistConversations(); renderHistory(); }
    });
    rename.dataset.conversationId = chat.id; rename.dataset.historyAction = "rename";
    const remove = actionButton("删除对话", "button button-quiet history-delete", () => deleteConversation(chat.id));
    remove.dataset.conversationId = chat.id; remove.dataset.historyAction = "delete";
    actions.append(rename, remove); row.append(actions); list.append(row);
  }
  if (focusId && focusAction && $("history-dialog").open) [...list.querySelectorAll("button")].find((button) => button.dataset.conversationId === focusId && button.dataset.historyAction === focusAction)?.focus();
}

function historicalProposal(source) {
  const card = element("section", "proposal-card history-proposal");
  card.append(element("strong", "", "历史提案 · 仅供查看"), element("h3", "proposal-title", source.data.payload.title), element("div", "proposal-description", source.data.payload.description || ""), element("p", "proposal-note", "原审批上下文已清除，此记录不可直接确认或重放。需要操作时请在新的安全上下文重新提出。"));
  return card;
}

function renderConversation() {
  $("chat-messages").querySelectorAll(".message,.history-readonly-note").forEach((node) => node.remove());
  $("chat-welcome").hidden = !(!state.conversation.messages.length && !state.conversation.jobs.length);
  showEvidence([]);
  if (!$("chat-welcome").hidden) return;
  $("chat-messages").append(element("p", "history-readonly-note", "历史记录仅供阅读。旧提案不会恢复审批权限，继续发送将建立新的安全执行上下文。"));
  for (const item of state.conversation.messages) {
    const message = addMessage(item.role, item.content, false, { record: false, mode: item.mode });
    message.display = item; message.article.historyDisplay = item;
    if (item.role !== "assistant") continue;
    const sources = sanitizeSources(item.sources), details = item.details || {};
    message.meta.append(actionButton(sources.length ? `查看 ${sources.length} 条依据` : "查看处理详情", "source-button", () => { showEvidence(sources, details, details); toggleEvidence(true); }));
    for (const source of sources.filter((source) => source.kind === "proposal")) message.body.append(historicalProposal(source));
    if (details.runId) message.meta.append(renderRunDetails(details, details));
  }
  state.history = boundHistory(state.conversation.messages.map(({ role, content }) => ({ role, content })));
  for (const job of state.conversation.jobs) {
    const message = addMessage("assistant", "", false, { record: false });
    const note = inlineNotice(`原任务编号：${job.id}。状态需要查询核实，历史记录不会重新执行。`);
    note.querySelector(".notice-content").append(actionButton("查询原任务", "button button-quiet", () => resumeWaiting(job)));
    message.body.replaceChildren(note);
  }
  scrollChat(true);
}

function finishSubmission(wait) {
  if (!wait.submitting) return;
  wait.submitting = false;
  if (wait.epoch === state.epoch) { state.submissionInFlight = Math.max(0, state.submissionInFlight - 1); updateComposer(); }
}

function rememberFinalMessage(message, data, meta, mode) {
  if (!["answered", "refused", "pending_confirmation"].includes(data.status)) return;
  const chat = state.conversations.find((item) => item.id === message.conversationId) || state.conversation;
  if (chat.id !== message.conversationId) return;
  const snapshot = sanitizeConversation({ ...chat, jobs: [], messages: [{ role: "assistant", content: data.answer,
    final: true, mode, sources: sourcesFor(data, mode), details: { ...meta,
      elapsed_ms: data.elapsed_ms, model_calls: data.model_calls, tool_calls: data.tool_calls,
      tool_executions: data.tool_executions, tool_failures: data.tool_failures,
      usage: data.usage || data.generation?.usage, failure_type: data.failure_type } }] });
  if (!snapshot?.messages.length) { notify("此回答超过历史保存上限，仅在当前页面显示。", "error"); return; }
  message.display = snapshot.messages[0]; message.article.historyDisplay = message.display;
  chat.messages.push(message.display); touchConversation();
}

function completeConversationJob(id) {
  const chat = state.conversation;
  if (!chat.jobs.some((job) => job.id === id)) return;
  chat.jobs = chat.jobs.filter((job) => job.id !== id); touchConversation();
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

function addMessage(role, content, pending = false, { record = true, mode = $("chat-mode").value } = {}) {
  $("chat-welcome").hidden = true;
  const article = element("article", `message ${role}`);
  article.setAttribute("aria-label", role === "user" ? "你的消息" : "知行助手的消息");
  const avatar = element("div", `message-avatar${role === "assistant" ? " assistant-seal" : ""}`);
  avatar.setAttribute("aria-hidden", "true");
  if (role === "user") {
    const portrait = element("img", "swordsman-avatar");
    portrait.src = "/ui-assets/avatar-swordsman.webp"; portrait.alt = ""; portrait.width = 48; portrait.height = 48;
    avatar.append(portrait);
  } else avatar.append(element("span", "seal-character", "知"));
  const block = element("div", "message-block");
  // The local swordsman portrait and alignment convey identity without a duplicate user label.
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
  const message = { article, body, meta, conversationId: state.conversation.id };
  if (record && content && !pending) {
    const display = role === "user" ? { role, content, mode } : { role, content, mode, final: true, sources: [] };
    state.conversation.messages.push(display); message.display = display; article.historyDisplay = display;
    touchConversation();
  }
  return message;
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
  if (safeFailureType(data.failure_type)) rows.push(["失败类型", `${jobFailureLabel(data.failure_type)}（${data.failure_type}）`]);
  if (typeof data.tool_calls === "number") rows.push(["工具尝试", `${data.tool_calls} 次`]);
  if (typeof data.tool_executions === "number") rows.push(["工具执行", `${data.tool_executions} 次`]);
  if (typeof data.tool_failures === "number") rows.push(["工具失败", `${data.tool_failures} 次`]);
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

function resizeComposer() {
  const input = $("question-input");
  const style = getComputedStyle(input);
  const maximum = Number.parseFloat(style.getPropertyValue("--composer-max-height")) || 176;
  input.style.height = "auto";
  const height = Math.max(52, Math.min(maximum, input.scrollHeight));
  input.style.height = `${height}px`;
  input.style.overflowY = input.scrollHeight > maximum ? "auto" : "hidden";
}

function updateComposer() {
  $("stop-wait").hidden = !state.busy;
  $("send-button").hidden = state.busy;
  $("send-button").disabled = state.busy || !$("question-input").value.trim();
  $("question-input").disabled = state.busy;
  $("chat-mode").disabled = state.busy;
  $("new-chat").disabled = state.proposalBusy || state.historyLoading || state.submissionInFlight > 0;
  $("input-count").textContent = `${$("question-input").value.length} / 2000`;
  resizeComposer();
}

// Browser persistence contains identifiers only; never questions, answers or bearer credentials.
function recoveryKey() { return PUBLIC_BUILD && auth.user ? `pc.jobs.v1.${auth.user.id}` : null; }
function savedJobs() {
  try {
    const key = recoveryKey(); if (!key) return [];
    return recoveryRecords(JSON.parse(localStorage.getItem(key) || "[]"));
  } catch { return []; }
}
function storeJobs(records) {
  try {
    const key = recoveryKey(); if (!key) return;
    const clean = recoveryRecords(records);
    if (clean.length) localStorage.setItem(key, JSON.stringify(clean)); else localStorage.removeItem(key);
  } catch { /* Storage can be unavailable; the current page still works. */ }
}
function saveJob(record) {
  storeJobs([...savedJobs().filter((item) => item.id !== record.id), record]);
  renderRecovery();
}
function removeSavedJob(id) {
  storeJobs(savedJobs().filter((record) => record.id !== id));
  state.jobObservations.delete(id); state.jobReadErrors.delete(id);
  renderRecovery();
}
function observeJob(id, data) {
  const observation = jobObservation(data, id);
  if (!observation) return;
  state.jobObservations.set(id, observation); state.jobReadErrors.delete(id);
  renderRecovery();
}
function jobRequest(epoch) {
  return async (path, options = {}) => {
    const response = await apiRequest(path, options);
    const match = /^\/jobs\/([a-f0-9-]{36})$/i.exec(path);
    if (match && !options.method && epoch === state.epoch) {
      if (!jobObservation(response.data, match[1])) throw new ApiError("任务状态与原编号不匹配，请勿重复提交。", { kind: "response", meta: { jobId: match[1] } });
      observeJob(match[1], response.data);
    }
    return response;
  };
}
async function queryJobState(record, { signal, epoch = state.epoch } = {}) {
  try {
    const response = await apiRequest(`/jobs/${record.id}`, { timeoutMs: 10000, signal });
    if (epoch !== state.epoch || signal?.aborted) return;
    if (!jobObservation(response.data, record.id)) throw new ApiError("任务状态格式无效。", { kind: "response" });
    observeJob(record.id, response.data);
  } catch {
    if (epoch === state.epoch && !signal?.aborted) {
      state.jobReadErrors.add(record.id); renderRecovery();
    }
  }
}
let taskCenterReturnFocus = null;
function closeTaskCenter() {
  const dialog = $("task-center-dialog");
  if (dialog?.open) dialog.close();
}
async function openTaskCenter() {
  const dialog = $("task-center-dialog");
  if (!dialog || dialog.open) return;
  taskCenterReturnFocus = document.activeElement;
  renderRecovery(); dialog.showModal();
  $("task-center-toggle")?.setAttribute("aria-expanded", "true");
  $("task-center-close")?.focus();
  const epoch = state.epoch;
  state.taskCenterReads?.abort();
  const controller = new AbortController(); state.taskCenterReads = controller;
  // One bounded, sequential read pass when opened. No background polling or POST here.
  for (const record of savedJobs().filter((item) => state.showHiddenJobs || !item.hidden).reverse().slice(0, 8)) {
    if (!dialog.open || controller.signal.aborted || epoch !== state.epoch) break;
    await queryJobState(record, { signal: controller.signal, epoch });
    if (!dialog.open || controller.signal.aborted || epoch !== state.epoch) break;
    await new Promise((resolve) => setTimeout(resolve, 180));
  }
  if (state.taskCenterReads === controller) state.taskCenterReads = null;
}
function renderRecovery() {
  const target = $("job-recovery"); if (!target) return;
  const jobs = savedJobs(), shown = jobs.filter((record) => !record.hidden), hidden = jobs.length - shown.length;
  const trigger = $("task-center-toggle"), count = $("task-center-count");
  if (count) count.textContent = String(shown.length);
  if (trigger) trigger.setAttribute("aria-label", `任务中心，${shown.length} 条记录${hidden ? `，另有 ${hidden} 条已隐藏` : ""}`);
  const focusControl = target.contains(document.activeElement) ? document.activeElement.id : null;
  const focusId = document.activeElement?.dataset?.jobId;
  const focusAction = document.activeElement?.dataset?.jobAction;
  target.hidden = false; target.replaceChildren();
  const controls = element("div", "task-center-controls");
  controls.append(element("p", "task-center-count-label", `${shown.length} 条可见 · ${hidden} 条已隐藏`));
  const toggle = actionButton(state.showHiddenJobs ? "收起已隐藏" : `查看已隐藏（${hidden}）`, "button button-quiet", () => {
    state.showHiddenJobs = !state.showHiddenJobs; renderRecovery(); $("task-center-show-hidden")?.focus();
  });
  toggle.id = "task-center-show-hidden"; toggle.setAttribute("aria-pressed", String(state.showHiddenJobs));
  toggle.disabled = hidden === 0 && !state.showHiddenJobs; controls.append(toggle); target.append(controls);
  const visible = jobs.filter((record) => state.showHiddenJobs || !record.hidden).reverse();
  if (!visible.length) target.append(element("p", "task-center-empty", hidden ? "没有可见任务。已隐藏的记录仍可查看和恢复。" : "暂无可恢复任务。停止等待或刷新后，可在这里查询原任务。"));
  for (const record of visible) {
    const observation = state.jobObservations.get(record.id);
    const row = element("article", `job-recovery-row${record.hidden ? " is-hidden-record" : ""}`);
    const heading = element("div", "job-recovery-heading");
    const identity = element("strong", "job-kind", record.kind === "rag" ? "知识问答" : "智能协作");
    const status = element("span", "job-state", jobStateLabel(observation));
    if (observation) status.dataset.state = observation.status;
    heading.append(identity, status); row.append(heading);
    const metadata = element("div", "job-recovery-meta");
    const submitted = element("time", "job-submitted", `提交于 ${formatDate(record.createdAt)}`);
    submitted.dateTime = new Date(record.createdAt).toISOString();
    const identifier = element("span", "job-identifier", record.id.slice(0, 8));
    identifier.title = record.id; identifier.setAttribute("aria-label", `任务编号 ${record.id}`);
    metadata.append(submitted, identifier); row.append(metadata);
    if (record.hidden) row.append(element("p", "job-local-note", "已在本浏览器隐藏；后台状态与额度未改变。"));
    if (state.jobReadErrors.has(record.id)) row.append(element("p", "job-local-note", "本次状态查询未成功；上次结果仅供参考，可稍后查询原任务。"));
    const actions = element("div", "job-recovery-actions");
    const bindAction = (label, action, handler) => {
      const button = actionButton(label, "button button-quiet", handler);
      button.dataset.jobId = record.id; button.dataset.jobAction = action;
      actions.append(button); return button;
    };
    bindAction("查询状态", "query", () => queryJobState(record));
    const resume = bindAction("查询 / 恢复等待", "resume", () => { closeTaskCenter(); setView("chat"); resumeWaiting(record); });
    resume.disabled = state.busy;
    if (record.hidden || canHideJob(observation)) bindAction(record.hidden ? "取消隐藏" : "隐藏此记录", "hide", () => {
      storeJobs(setRecoveryHidden(savedJobs(), record.id, !record.hidden, observation));
      renderRecovery();
      if (!record.hidden) $("task-center-show-hidden")?.focus();
    });
    if (canHideJob(observation)) {
      const remove = bindAction("删除本地记录", "delete", () => {
        if (!canHideJob(state.jobObservations.get(record.id))) return;
        if (!window.confirm(`删除本地恢复记录？\n任务编号：${record.id}\n仅移除此账号的浏览器恢复索引；不会取消任务、删除后台记录、工单或退还费用和额度。`)) return;
        removeSavedJob(record.id); $("task-center-close").focus();
      });
      remove.classList.add("task-record-delete");
    }
    row.append(actions); target.append(row);
  }
  // A status read must not steal keyboard focus while replacing that row.
  if (focusControl && $("task-center-dialog")?.open) $(focusControl)?.focus();
  if (focusId && focusAction && $("task-center-dialog")?.open) {
    [...target.querySelectorAll("button[data-job-id]")].find((button) => button.dataset.jobId === focusId && button.dataset.jobAction === focusAction)?.focus();
  }
}
function startWaiting(message, mode) {
  const wait = { controller: new AbortController(), message, mode, sequence: 0, draft: "", epoch: state.epoch };
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
  wait.message.body.replaceChildren(inlineNotice("已停止等待，后台任务可能继续执行。请从“任务中心”查询原任务，不要重复提交。", { className: "waiting-stopped" }));
  updateComposer(); renderRecovery();
}
async function displayAnswer(message, data, meta, mode, context, question, epoch) {
    if (typeof data.answer !== "string" || typeof data.status !== "string") throw new ApiError("服务未返回可识别的回答。", { meta, kind: "response" });
    rememberFinalMessage(message, data, meta, mode);
    message.body.replaceChildren(renderMarkdown(data.answer));
    if (["failed", "budget_exhausted"].includes(data.status)) {
      const reason = safeFailureType(data.failure_type);
      const label = reason ? `${jobFailureLabel(reason)}（${reason}）。` : "";
      message.body.append(inlineNotice(`${label}${data.status === "budget_exhausted" ? "本次处理达到执行预算，未完成的操作不会被视为成功。" : "本次处理未完成，可查看运行详情定位原因。"}`, { tone: "warning", className: "error-note", role: "alert" }));
    } else state.history = boundHistory(state.conversation.messages.map(({ role, content }) => ({ role, content })));
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
  const original = state.conversations.find((chat) => chat.jobs.some((job) => job.id === record.id));
  if (original && original.id !== state.conversation.id) {
    if (!switchConversation(original.id)) return;
  } else if (!original) {
    // A legacy recovery index has no transcript binding. Isolate it instead of
    // injecting an unrelated answer into whichever conversation is visible.
    if (!switchConversation()) return;
    state.conversation.title = `恢复${record.kind === "rag" ? "知识问答" : "Agent"} ${record.id.slice(0, 8)}`;
    state.conversation.jobs.push({ id: record.id, kind: record.kind, createdAt: record.createdAt });
    touchConversation();
  }
  const epoch = state.epoch;
  const message = addMessage("assistant", "", true), wait = startWaiting(message, record.kind);
  wait.jobId = record.id;
  try {
    if (PUBLIC_BUILD) await refreshUiConfig();
    if (state.activeWait !== wait || epoch !== state.epoch) return;
    const { data, meta } = await resumePublicJob(record.id, { signal: wait.controller.signal, request: jobRequest(epoch), useEvents: true,
      ...personalJobWaitOptions(state.personalDemo), isCurrent: () => epoch === state.epoch,
      onEvent: (event) => updateJobEvent(wait, event) });
    if (state.activeWait !== wait || epoch !== state.epoch) return;
    await displayAnswer(message, data, meta, record.kind, state.jobContexts.get(record.id) || null, null, epoch);
    state.jobContexts.delete(record.id);
    removeSavedJob(record.id);
    completeConversationJob(record.id);
  } catch (error) {
    if (state.activeWait === wait && epoch === state.epoch) message.body.replaceChildren(errorNode(error));
  } finally {
    if (state.activeWait === wait && epoch === state.epoch) { state.activeWait = null; state.busy = false; updateComposer(); renderRecovery(); scrollChat(); if (PUBLIC_BUILD) refreshUiConfig(); }
  }
}
async function sendQuestion(event) {
  event.preventDefault();
  const epoch = state.epoch;
  const question = $("question-input").value.trim();
  if (!question || state.busy || state.historyLoading || state.submissionInFlight || question.length > 2000) return;
  const mode = $("chat-mode").value, history = boundHistory(state.conversation.messages.map(({ role, content }) => ({ role, content })));
  addMessage("user", question);
  const message = addMessage("assistant", "", true), wait = startWaiting(message, mode);
  $("question-input").value = ""; updateComposer();
  let context = null, submitted = null;
  try {
    if (PUBLIC_BUILD) await refreshUiConfig();
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    if (mode === "agent") context = await session.ensure();
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    const payload = mode === "agent" ? { question, history } : { question };
    const headers = context ? ContextSession.headers(context) : {};
    if (PUBLIC_BUILD) { wait.submitting = true; state.submissionInFlight += 1; updateComposer(); }
    const { data, meta } = PUBLIC_BUILD
      ? await runPublicJob(mode, payload, { headers, signal: wait.controller.signal, request: jobRequest(epoch), useEvents: true,
          ...personalJobWaitOptions(state.personalDemo),
          isCurrent: () => epoch === state.epoch,
          onSubmitted: (record) => {
            finishSubmission(wait);
            if (epoch === state.epoch) {
              submitted = record; wait.jobId = record.id;
              if (context && state.activeWait === wait) state.jobContexts.set(record.id, context);
              const chat = state.conversations.find((chat) => chat.id === message.conversationId);
              if (chat && !chat.jobs.some((job) => job.id === record.id)) { chat.jobs.push({ id: record.id, kind: record.kind, createdAt: record.createdAt }); touchConversation(); }
              saveJob(record); refreshUiConfig();
            }
          },
          onEvent: (event) => updateJobEvent(wait, event) })
      : await apiRequest(mode === "agent" ? "/agent/ask" : "/rag/ask", {
          method: "POST", body: payload, headers, signal: wait.controller.signal, timeoutMs: mode === "agent" ? 330000 : 180000,
        });
    if (epoch !== state.epoch || state.activeWait !== wait) return;
    await displayAnswer(message, data, meta, mode, context, question, epoch);
    if (submitted) { removeSavedJob(submitted.id); state.jobContexts.delete(submitted.id); completeConversationJob(submitted.id); }
  } catch (error) {
    if (epoch !== state.epoch) return;
    if (error.meta?.outcome === "uncertain" || (!PUBLIC_BUILD && ["network", "timeout"].includes(error.kind))) {
      const chat = state.conversations.find((item) => item.id === message.conversationId);
      if (chat) chat.uncertainSubmission = true;
    }
    if (state.activeWait !== wait) return;
    message.body.replaceChildren(errorNode(error));
    if (error.status === 403) session.clear();
  } finally {
    finishSubmission(wait);
    if (epoch === state.epoch && state.activeWait === wait) {
      state.activeWait = null; state.busy = false; updateComposer(); renderRecovery(); scrollChat(); $("question-input").focus();
      if (PUBLIC_BUILD) refreshUiConfig();
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
  const record = { id: source.action_id, context, conversationId: state.conversation.id, action: null, busy: true, error: null, uncertain: false, blocked: false, node: element("section", "proposal-card") };
  state.proposals.set(record.id, record); container.append(record.node); renderProposal(record);
  await refreshProposal(record);
}

async function refreshProposal(record) {
  const epoch = state.epoch;
  record.busy = true; record.error = null; renderProposal(record);
  try {
    const { data } = await apiRequest(`/actions/${encodeURIComponent(record.id)}`, { headers: ContextSession.headers(record.context), timeoutMs: 15000 });
    if (epoch !== state.epoch || record.conversationId !== state.conversation.id) return;
    if (data.action_id !== record.id) throw new ApiError("提案编号与请求不一致。", { kind: "response" });
    applyAction(record, data);
    const display = record.node.closest(".message")?.historyDisplay;
    if (display) {
      display.sources = sanitizeSources([...(display.sources || []).filter((source) => source.id !== `proposal:${record.id}`), { id: `proposal:${record.id}`, kind: "proposal", data }]);
      touchConversation();
    }
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
    if (epoch !== state.epoch || record.conversationId !== state.conversation.id) return;
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
    if (PUBLIC_BUILD) await refreshUiConfig();
    if (epoch !== state.epoch) return;
    const { data } = PUBLIC_BUILD
      ? await runPublicJob("upload", form, { ...personalJobWaitOptions(state.personalDemo),
          isCurrent: () => epoch === state.epoch, onSubmitted: () => refreshUiConfig() })
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
  finally { if (epoch === state.epoch) { state.uploading = false; renderFile(); if (PUBLIC_BUILD) refreshUiConfig(); } }
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
  const epoch = state.epoch, origin = state.conversation;
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
  } catch (error) { if (epoch === state.epoch) {
    if (["network", "timeout", "response"].includes(error.kind) || error.status >= 500) origin.uncertainSubmission = true;
    feedback.append(errorNode(error)); if (error.status === 403) session.clear();
  } }
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
  $("history-toggle")?.addEventListener("click", openHistory);
  $("history-close")?.addEventListener("click", closeHistory);
  $("history-search")?.addEventListener("input", renderHistory);
  $("history-persist")?.addEventListener("change", changeHistoryPersistence);
  $("history-clear")?.addEventListener("click", clearPersistentHistory);
  const historyDialog = $("history-dialog");
  historyDialog?.addEventListener("close", () => {
    $("history-toggle")?.setAttribute("aria-expanded", "false");
    if (historyReturnFocus?.isConnected && !$("app-shell").hidden) historyReturnFocus.focus();
    historyReturnFocus = null;
  });
  historyDialog?.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); closeHistory(); return; }
    if (event.key !== "Tab" || !historyDialog.open) return;
    const focusable = [...historyDialog.querySelectorAll("button,input,select,textarea,a[href],[tabindex]")]
      .filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !historyDialog.contains(document.activeElement))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (document.activeElement === last || !historyDialog.contains(document.activeElement))) { event.preventDefault(); first.focus(); }
  });
  historyDialog?.addEventListener("click", (event) => {
    if (event.target !== historyDialog) return;
    const box = historyDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeHistory();
  });
  $("task-center-toggle")?.addEventListener("click", openTaskCenter);
  $("task-center-close")?.addEventListener("click", closeTaskCenter);
  const taskDialog = $("task-center-dialog");
  taskDialog?.addEventListener("close", () => {
    state.taskCenterReads?.abort(); state.taskCenterReads = null;
    $("task-center-toggle")?.setAttribute("aria-expanded", "false");
    if (taskCenterReturnFocus?.isConnected && !$("app-shell").hidden) taskCenterReturnFocus.focus();
    taskCenterReturnFocus = null;
  });
  taskDialog?.addEventListener("keydown", (event) => {
    if (event.key !== "Tab" || !taskDialog.open) return;
    const focusable = [...taskDialog.querySelectorAll("button, a[href], input, select, textarea, [tabindex]")]
      .filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const first = focusable[0], last = focusable.at(-1);
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || !taskDialog.contains(document.activeElement))) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !taskDialog.contains(document.activeElement))) {
      event.preventDefault(); first.focus();
    }
  });
  taskDialog?.addEventListener("click", (event) => {
    if (event.target !== taskDialog) return;
    const box = taskDialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) closeTaskCenter();
  });
  document.addEventListener("click", (event) => {
    for (const id of ["account-panel", "personal-demo-meter"]) {
      const menu = $(id);
      if (menu?.tagName === "DETAILS" && menu.open && !menu.contains(event.target)) menu.open = false;
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    for (const id of ["account-panel", "personal-demo-meter"]) {
      const menu = $(id);
      if (menu?.tagName === "DETAILS" && menu.open) { menu.open = false; menu.querySelector("summary")?.focus(); }
    }
  });
  window.addEventListener("resize", resizeComposer);
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
  $("new-chat").addEventListener("click", () => switchConversation());
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
  setInterval(renderPersonalDemo, 1000); // Existing expiry only; never an execution-progress timer.
  setInterval(() => {
    if (PUBLIC_BUILD && auth.user && state.personalDemo && !document.hidden) refreshUiConfig();
  }, 60000);
}

function renderPersonalDemo() {
  const target = $("personal-demo-meter");
  if (!target) return;
  const profile = state.personalDemo;
  target.hidden = !profile;
  if (!profile) return;
  const seconds = Math.max(0, Math.ceil((profile.deadline_ms - Date.now()) / 1000));
  const time = [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60]
    .map((value) => String(value).padStart(2, "0")).join(":");
  $("personal-demo-time").textContent = seconds ? time : "已到期";
  $("personal-demo-time").dateTime = profile.expires_at;
  $("personal-demo-tasks").textContent = `${profile.jobs_used} / ${profile.job_limit}`;
  $("personal-demo-tasks").setAttribute("aria-label", `本次授权已用 ${profile.jobs_used} / ${profile.job_limit} 次任务`);
  $("personal-demo-slots").textContent = `未完成 ${profile.occupied_slots} / ${profile.concurrent_limit}`;
  const note = seconds === 0 ? "授权已到期；原任务仍可保留编号核查" : state.profileStale
    ? "额度信息暂未更新" : profile.worker_enabled ? "后台按顺序处理队列" : "后台执行器已暂停";
  if ($("personal-demo-note").textContent !== note) $("personal-demo-note").textContent = note;
  target.classList.toggle("expired", seconds === 0);
}

async function refreshUiConfig() {
  if (state.configPending) return state.configPending;
  const epoch = state.epoch;
  const pending = (async () => {
    try {
      const { data } = await apiRequest("/ui/config", { timeoutMs: 10000 });
      if (epoch !== state.epoch) return;
      state.personalDemo = PUBLIC_BUILD ? personalDemoProfile(data.personal_demo) : null;
      state.profileStale = false; renderPersonalDemo();
      if (Number.isInteger(data.upload_max_bytes) && data.upload_max_bytes > 0) {
        state.uploadLimit = PUBLIC_BUILD ? Math.min(data.upload_max_bytes, 4 * 1024 * 1024) : data.upload_max_bytes;
        $("upload-limit").textContent = `支持 ${PUBLIC_BUILD ? "TXT、Markdown" : "TXT、Markdown、PDF"} · 单文件最多 ${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(state.uploadLimit / 1024 / 1024)} MB`;
      }
    } catch {
      if (epoch === state.epoch) {
        state.profileStale = true; renderPersonalDemo();
        $("upload-limit").textContent = `支持 ${PUBLIC_BUILD ? "TXT、Markdown" : "TXT、Markdown、PDF"} · 文件大小由服务校验`;
      }
    } finally {
      if (state.configPending === pending) state.configPending = null;
    }
  })();
  state.configPending = pending;
  return pending;
}

async function initializeServices() {
  checkService();
  await refreshUiConfig();
}

async function start() {
  bindEvents(); updateComposer(); renderFile();
  if (!PUBLIC_BUILD) {
    $("app-shell").hidden = false; $("workspace-skip").hidden = false;
    initializeServices(); initializeConversations();
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
