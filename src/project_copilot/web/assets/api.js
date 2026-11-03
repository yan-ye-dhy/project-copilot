/* Same-origin HTTP client. Every user action makes at most one write attempt. */
// The static EdgeOne build changes only this constant; local FastAPI uses root routes.
const API_PREFIX = ""; // EDGEONE_API_PREFIX
export const PUBLIC_BUILD = Boolean(API_PREFIX);

// CSRF tokens are derived from an HttpOnly session and live only in memory.
let csrfToken = null;
let authFailureHandler = null;
let authGeneration = 0;

export function setCsrfToken(token) {
  if (token !== null && (typeof token !== "string" || !/^[a-f0-9]{64}$/.test(token))) {
    throw new TypeError("Invalid CSRF token");
  }
  csrfToken = token;
  authGeneration += 1;
}

export function onAuthenticationFailure(handler) {
  if (handler !== null && typeof handler !== "function") throw new TypeError("Invalid authentication callback");
  authFailureHandler = handler;
}

export class ApiError extends Error {
  constructor(message, { status = 0, meta = {}, kind = "http" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.meta = meta;
    this.kind = kind;
  }
}

// Only the first POST creates work. Polls are read-only and never repeat it.
export async function runPublicJob(kind, payload, { headers = {}, request = apiRequest,
  delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms)), maxPolls = 240,
  createId = () => globalThis.crypto.randomUUID(), isCurrent = () => true } = {}) {
  const idempotencyKey = createId();
  const submitted = await request(kind === "upload" ? "/jobs/upload" : "/jobs", {
    method: "POST", body: kind === "upload" ? payload : { kind, payload },
    headers: { ...headers, "Idempotency-Key": idempotencyKey }, timeoutMs: 30000 });
  const id = submitted.data?.job_id;
  if (typeof id !== "string" || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id)) {
    throw new ApiError("任务编号无效，请勿重复提交。", { kind: "response" });
  }
  for (let i = 0; i < maxPolls; i += 1) {
    if (!isCurrent()) throw new ApiError("会话已切换。", { kind: "aborted" });
    const current = await request(`/jobs/${id}`, { timeoutMs: 15000 });
    if (current.data?.result_expired === true) {
      throw new ApiError(`任务内容已超过保留期限，请勿重复提交。任务编号：${id}`, { kind: "job" });
    }
    if (current.data?.status === "succeeded" && current.data.result) return { data: current.data.result, meta: current.meta };
    if (["failed", "unknown"].includes(current.data?.status)) {
      throw new ApiError(`任务未完成（${current.data.status === "unknown" ? "结果待核实" : "处理失败"}），请勿重复提交。任务编号：${id}`, { kind: "job" });
    }
    if (!["reserved", "running"].includes(current.data?.status)) throw new ApiError("任务状态无效，请勿重复提交。", { kind: "response" });
    await delay(2000);
  }
  throw new ApiError(`任务仍在处理，请勿重复提交。任务编号：${id}`, { kind: "timeout" });
}

function detailMessage(value, status) {
  if (typeof value?.detail === "string") return value.detail;
  if (Array.isArray(value?.detail)) {
    return value.detail.slice(0, 2).map((item) => item.msg || "请检查填写内容").join("；");
  }
  return ({
    403: "会话凭据已失效，请开始新对话后重新提出操作。",
    404: "未找到这条记录。",
    409: "当前操作发生冲突，请重新查看提案状态。",
    413: "文件超过允许的大小。",
    422: "请检查填写内容。",
    502: "模型或检索服务暂时未能完成请求。",
    503: "服务配置或存储暂时不可用。",
  })[status] || "请求未能完成，请稍后查看服务状态。";
}

export async function apiRequest(path, {
  method = "GET", body, headers = {}, signal, timeoutMs = 180000,
  fetchImpl = globalThis.fetch,
} = {}) {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//") ||
      path.includes("://") || /[\\\x00-\x1f\x7f]/.test(path)) {
    throw new ApiError("请求地址必须属于当前服务。", { kind: "configuration" });
  }
  const requestPath = `${API_PREFIX}${path}`;
  // A late 401 from a previous session must not log out a different user.
  const requestAuthGeneration = authGeneration;
  const parsed = new URL(requestPath, "https://frontend.invalid");
  if (parsed.origin !== "https://frontend.invalid" ||
      (API_PREFIX && !parsed.pathname.startsWith(`${API_PREFIX}/`))) {
    throw new ApiError("请求地址必须属于当前服务。", { kind: "configuration" });
  }
  const mutating = !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase());
  const authenticationEntry = ["/auth/login", "/auth/register"].includes(path.split("?", 1)[0]);
  if (PUBLIC_BUILD && mutating && !authenticationEntry && !csrfToken) {
    throw new ApiError("请先登录后再操作。", { status: 401, kind: "authentication" });
  }
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const forwardAbort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", forwardAbort, { once: true });
  try {
    const isForm = typeof FormData !== "undefined" && body instanceof FormData;
    const requestHeaders = { Accept: "application/json", ...headers };
    if (PUBLIC_BUILD && mutating && !authenticationEntry) {
      for (const name of Object.keys(requestHeaders)) if (name.toLowerCase() === "x-csrf-token") delete requestHeaders[name];
      requestHeaders["X-CSRF-Token"] = csrfToken;
    }
    if (body !== undefined && !isForm) requestHeaders["Content-Type"] = "application/json";
    const response = await fetchImpl(requestPath, {
      method, headers: requestHeaders, credentials: "same-origin", redirect: "error",
      body: body === undefined ? undefined : isForm ? body : JSON.stringify(body),
      signal: controller.signal,
    });
    const meta = {
      runId: response.headers.get("x-run-id"),
      traceId: response.headers.get("x-trace-id"),
      traceExported: response.headers.get("x-trace-exported") === "true",
    };
    if (PUBLIC_BUILD && response.status === 401 && !path.startsWith("/auth/") &&
        requestAuthGeneration === authGeneration && authFailureHandler) {
      authFailureHandler();
    }
    let data;
    try { data = await response.json(); }
    catch { throw new ApiError("服务返回了无法识别的结果，请根据请求编号排查。", { status: response.status, meta, kind: "response" }); }
    if (!response.ok) {
      throw new ApiError(detailMessage(data, response.status), { status: response.status, meta });
    }
    return { data, meta };
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (timedOut) throw new ApiError("等待服务响应超时。操作结果尚未核实，请查询状态后再决定下一步。", { kind: "timeout" });
    if (signal?.aborted) throw new ApiError("请求已取消。", { kind: "aborted" });
    throw new ApiError("无法连接服务。若刚才进行了确认或上传，请先查询结果，避免重复操作。", { kind: "network" });
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forwardAbort);
  }
}

// Credentials exist only in this page's memory; they never enter chat history,
// localStorage, sessionStorage, DOM or diagnostic output.
export class ContextSession {
  constructor(request = apiRequest) {
    this.request = request;
    this.current = null;
    this.pending = null;
    this.generation = 0;
  }

  clear() {
    this.generation += 1;
    this.current = null;
    this.pending = null;
  }

  async ensure() {
    if (this.current && Date.parse(this.current.expires_at) > Date.now() + 30000) return this.current;
    if (this.pending) return this.pending;
    const generation = this.generation;
    const pending = this.request("/agent/contexts", { method: "POST", timeoutMs: 15000 })
      .then(({ data }) => {
        if (typeof data?.context_id !== "string" || typeof data?.context_token !== "string" ||
            !data.context_token || !Number.isFinite(Date.parse(data.expires_at))) {
          throw new ApiError("会话创建结果无效。", { kind: "response" });
        }
        const context = Object.freeze({ ...data });
        if (generation === this.generation) this.current = context;
        return context;
      }).finally(() => { if (this.pending === pending) this.pending = null; });
    this.pending = pending;
    return pending;
  }

  static headers(context) {
    return {
      "X-Copilot-Context-ID": context.context_id,
      "X-Copilot-Context-Token": context.context_token,
    };
  }
}

export function boundHistory(messages) {
  const history = messages
    .filter((item) => ["user", "assistant"].includes(item.role) && typeof item.content === "string" && item.content.trim())
    .map((item) => ({ role: item.role, content: Array.from(item.content.trim()).slice(0, 4000).join("") }))
    .slice(-8);
  while (history.reduce((sum, item) => sum + Array.from(item.content).length, 0) > 10000) history.shift();
  if (history[0]?.role === "assistant") history.shift();
  return history;
}

export function validateAction(value, context) {
  const statuses = ["pending", "completed", "cancelled", "expired"];
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!value || !uuid.test(value.action_id || "") || !uuid.test(value.context_id || "") || value.context_id !== context.context_id ||
      !statuses.includes(value.status) || !/^[a-f0-9]{64}$/.test(value.payload_fingerprint) ||
      !Number.isFinite(Date.parse(value.expires_at)) ||
      typeof value.payload?.title !== "string" || !value.payload.title.trim() ||
      typeof value.payload?.description !== "string" || !value.payload.description.trim() ||
      (value.status === "completed" && !uuid.test(value.ticket_id || ""))) {
    throw new ApiError("提案结果未通过校验，已停止确认。", { kind: "response" });
  }
  return Object.freeze({ ...value, payload: Object.freeze({ ...value.payload }) });
}
