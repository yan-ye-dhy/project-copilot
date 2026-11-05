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

const JOB_ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;

function jobStopped(signal, isCurrent) {
  if (!isCurrent()) throw new ApiError("会话已切换。", { kind: "aborted" });
  if (signal?.aborted) throw new ApiError("已停止等待，后台任务可能继续执行。请查询原任务状态。", { kind: "aborted" });
}

function delayJob(ms, signal) {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(new ApiError("已停止等待，后台任务可能继续执行。", { kind: "aborted" })); };
    const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, ms);
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
  });
}

// Read-only SSE connection. Reconnection never repeats a model or tool request.
export async function readJobEvents(id, { after = 0, signal, onEvent = () => {}, fetchImpl = globalThis.fetch,
  timeoutMs = 27000 } = {}) {
  if (!JOB_ID.test(id) || !Number.isSafeInteger(after) || after < 0) throw new ApiError("任务编号无效。", { kind: "response" });
  const controller = new AbortController();
  const abort = () => controller.abort();
  const timer = setTimeout(abort, timeoutMs);
  const generation = authGeneration;
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  let reader;
  try {
    const response = await fetchImpl(`${API_PREFIX}/jobs/${id}/events?after=${after}`, {
      headers: { Accept: "text/event-stream", ...(after ? { "Last-Event-ID": String(after) } : {}) },
      credentials: "same-origin", redirect: "error", signal: controller.signal,
    });
    if (PUBLIC_BUILD && response.status === 401 && generation === authGeneration) authFailureHandler?.();
    if (!response.ok || !response.headers.get("content-type")?.toLowerCase().startsWith("text/event-stream") || !response.body?.getReader) {
      throw new ApiError("实时进度暂不可用，改用任务状态查询。", { status: response.status, kind: "stream" });
    }
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "", last = after, terminal = false;
    while (true) {
      const { done, value } = await reader.read();
      if (signal?.aborted) throw new ApiError("已停止等待，后台任务可能继续执行。", { kind: "aborted" });
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 131072) throw new ApiError("实时事件超过限制。", { kind: "stream" });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        let event = "message", sequence = null; const data = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("id:")) sequence = Number(line.slice(3).trim());
          if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
        }
        if (!data.length) continue;
        let payload;
        try { payload = JSON.parse(data.join("\n")); } catch { throw new ApiError("实时事件格式无效。", { kind: "stream" }); }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new ApiError("实时事件格式无效。", { kind: "stream" });
        if (event === "snapshot" && ["succeeded", "failed", "unknown"].includes(payload.status)) {
          terminal = true; break; // Canonical GET is authoritative even without a journal event.
        }
        if (!Number.isSafeInteger(sequence) || sequence < 0) continue;
        if (event === "reset" && payload.cursor_reset === true) {
          last = sequence; onEvent({ id: sequence, event, data: payload }); continue;
        }
        if (sequence <= last) continue;
        last = sequence;
        if (["progress", "delta", "terminal", "reset", "snapshot"].includes(event)) onEvent({ id: sequence, event, data: payload });
        if (event === "terminal") { terminal = true; break; }
      }
      if (terminal) break;
    }
    return { after: last, terminal };
  } catch (error) {
    if (signal?.aborted) throw new ApiError("已停止等待，后台任务可能继续执行。", { kind: "aborted" });
    if (error instanceof ApiError) throw error;
    throw new ApiError("实时连接已断开，继续查询原任务。", { kind: "stream" });
  } finally {
    clearTimeout(timer); signal?.removeEventListener("abort", abort);
    if (reader) { try { await reader.cancel(); } catch {} reader.releaseLock(); }
  }
}

// Reattach by identifier after stop/reload. This function cannot create work.
export async function resumePublicJob(id, { request = apiRequest, signal, onEvent = () => {},
  after = 0, delay = delayJob, maxPolls = 240, isCurrent = () => true,
  eventReader = readJobEvents, useEvents = request === apiRequest,
  maxWaitMs = 8 * 60 * 1000, now = Date.now } = {}) {
  if (!JOB_ID.test(id)) throw new ApiError("任务编号无效，请勿重复提交。", { kind: "response" });
  if (!Number.isFinite(maxWaitMs) || maxWaitMs <= 0 || maxWaitMs > 8 * 60 * 1000) throw new ApiError("等待时限无效。", { kind: "configuration" });
  const deadline = now() + maxWaitMs;
  let cursor = after, streamFailures = 0;
  for (let i = 0; i < maxPolls; i += 1) {
    jobStopped(signal, isCurrent);
    if (now() >= deadline) break;
    const current = await request(`/jobs/${id}`, { timeoutMs: 15000, signal });
    jobStopped(signal, isCurrent);
    if (current.data?.result_expired === true) {
      throw new ApiError(`任务内容已超过保留期限，请勿重复提交。任务编号：${id}`, { kind: "job", meta: { jobId: id, jobStatus: "expired" } });
    }
    const status = current.data?.status;
    if (status === "succeeded" && current.data.result) return { data: current.data.result, meta: current.meta || {} };
    if (["failed", "unknown"].includes(status)) {
      throw new ApiError(`任务未完成（${status === "unknown" ? "结果待核实" : "处理失败"}），请勿重复提交。任务编号：${id}`, { kind: "job", meta: { jobId: id, jobStatus: status } });
    }
    if (!["reserved", "running"].includes(status)) throw new ApiError("任务状态无效，请勿重复提交。", { kind: "response" });
    if (current.data.progress && typeof current.data.progress.label === "string") onEvent({ id: null, event: "progress", data: current.data.progress });
    else onEvent({ id: null, event: "progress", data: { stage: status, label: status === "reserved" ? "任务已入队，等待执行" : "后台正在执行" } });
    if (useEvents && streamFailures < 2) {
      try {
        const stream = await eventReader(id, { after: cursor, signal, timeoutMs: Math.max(1, Math.min(27000, deadline - now())), onEvent: (event) => {
          jobStopped(signal, isCurrent);
          if (event.event === "reset" && event.data.cursor_reset === true) cursor = event.id;
          else if (Number.isSafeInteger(event.id)) cursor = Math.max(cursor, event.id);
          onEvent(event);
        } });
        if (Number.isSafeInteger(stream.after) && stream.after >= 0) cursor = stream.after;
        if (!stream.terminal) await delay(300, signal);
        continue;
      } catch (error) {
        jobStopped(signal, isCurrent);
        if (error.status === 401 || error.status === 403) throw error;
        streamFailures += 1;
        onEvent({ id: null, event: "transport", data: { label: "实时进度暂不可用，正在查询原任务状态" } });
      }
    }
    await delay(2000, signal);
  }
  throw new ApiError(`任务仍在处理，请勿重复提交。任务编号：${id}`, { kind: "timeout", meta: { jobId: id } });
}

// Only the first POST creates work; even failed SSE cannot repeat it.
export async function runPublicJob(kind, payload, { headers = {}, request = apiRequest,
  createId = () => globalThis.crypto.randomUUID(), onSubmitted = () => {}, signal, ...options } = {}) {
  const idempotencyKey = createId();
  // Stopping the wait cannot cancel/repeat a write that may already have committed.
  const submitted = await request(kind === "upload" ? "/jobs/upload" : "/jobs", {
    method: "POST", body: kind === "upload" ? payload : { kind, payload },
    headers: { ...headers, "Idempotency-Key": idempotencyKey }, timeoutMs: 30000 });
  const id = submitted.data?.job_id;
  if (typeof id !== "string" || !JOB_ID.test(id)) throw new ApiError("任务编号无效，请勿重复提交。", { kind: "response" });
  onSubmitted({ id, kind, lastSequence: 0, createdAt: Date.now() });
  return resumePublicJob(id, { ...options, signal, request });
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
