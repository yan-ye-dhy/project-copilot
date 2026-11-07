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
const DEFAULT_JOB_WAIT_MS = 8 * 60 * 1000;
const MAX_JOB_WAIT_MS = 60 * 60 * 1000;
export const PERSONAL_JOB_WAIT_MS = 50 * 60 * 1000;

const FAILURE_LABELS = Object.freeze({
  model_budget: "模型决策次数达到上限", tool_budget: "工具调用次数达到上限",
  provider_budget: "供应商调用次数达到上限", provider_attempts: "供应商调用次数达到上限",
  total_timeout: "任务达到总执行时限", model_timeout: "模型调用超时", tool_timeout: "工具等待超时",
  repeated_tool_loop: "检测到重复且无进展的工具循环", tool_no_progress: "工具调用没有取得新的进展",
  no_progress: "执行没有取得新的进展", model_error: "模型服务调用未能完成",
  model_auth_error: "模型服务认证失败", model_rate_limit: "模型服务侧限流",
  model_provider_error: "模型服务内部异常", model_request_rejected: "模型服务拒绝了请求",
  model_connection_error: "无法连接模型服务", model_response_invalid: "模型服务响应未通过校验",
  model_output_limit: "模型输出达到长度上限", model_input_invalid: "模型请求输入未通过校验",
  model_tool_protocol: "模型工具调用格式未通过校验",
  model_exception: "模型服务调用未能完成", tool_exception: "工具执行未能完成",
  invalid_final: "最终回答未通过校验", final_output_invalid: "最终回答未通过校验",
  invalid_sources: "回答引用未通过校验", invalid_citation: "回答引用未通过校验",
  invalid_json: "回答格式未通过校验", invalid_schema: "回答结构未通过校验",
  authentication_error: "执行身份或上下文校验失效", knowledge_retrieval_failed: "知识检索未能完成",
  execution_error: "后台执行未能完成", tool_protocol_error: "工具调用格式未通过校验",
  grant_ended: "授权已结束，排队任务未执行；不会自动重试或退款",
  context_budget: "上下文达到处理上限",
  tool_output_invalid: "工具结果未通过校验", tool_output_too_large: "工具结果超过处理上限",
});
const QUOTA_LABELS = Object.freeze({
  personal_demo_limit: "本次个人授权的任务次数已用完。",
  user_daily: "你的 UTC 每日任务额度已用完。",
  global_daily: "全站 UTC 每日任务额度已用完。",
  user_concurrency: "未完成任务已占满个人并发槽，请等待或查询原任务。",
  provider_attempts: "本任务的供应商调用次数已达上限。",
});
export function safeFailureType(value) {
  return typeof value === "string" && Object.hasOwn(FAILURE_LABELS, value) ? value : undefined;
}
export function jobFailureLabel(value) {
  return FAILURE_LABELS[safeFailureType(value)] || "处理未能完成";
}
function quotaReason(value) {
  const reason = value?.reason ?? value?.detail?.reason;
  return typeof reason === "string" && Object.hasOwn(QUOTA_LABELS, reason) ? reason : undefined;
}

// Only these non-secret server fields are used. A countdown describes an
// existing authorization, never grants permission or simulates Agent progress.
export function personalDemoProfile(value, now = Date.now()) {
  if (!value || typeof value !== "object" || Array.isArray(value) || typeof value.expires_at !== "string" ||
      !Number.isFinite(Date.parse(value.expires_at)) || typeof value.worker_enabled !== "boolean") return null;
  const fields = ["job_limit", "jobs_used", "user_daily_limit", "global_daily_limit", "concurrent_limit", "occupied_slots"];
  if (fields.some((key) => !Number.isSafeInteger(value[key]) || value[key] < 0) ||
      value.job_limit < 1 || value.job_limit > 500 || value.jobs_used > value.job_limit ||
      value.user_daily_limit < 1 || value.global_daily_limit < 1 ||
      value.concurrent_limit < 1 || value.concurrent_limit > 3) return null;
  const seconds = value.remaining_seconds === undefined
    ? Math.max(0, (Date.parse(value.expires_at) - now) / 1000) : value.remaining_seconds;
  if (!Number.isFinite(seconds) || seconds < 0 || seconds > 86400) return null;
  return Object.freeze(Object.fromEntries([
    ...fields.map((key) => [key, value[key]]), ["expires_at", value.expires_at],
    ["deadline_ms", Math.min(Date.parse(value.expires_at), now + Math.floor(seconds * 1000))],
    ["worker_enabled", value.worker_enabled],
  ]));
}
export function personalJobWaitOptions(profile) {
  return profile ? { maxWaitMs: PERSONAL_JOB_WAIT_MS } : {};
}

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

// Retry-After changes read cadence only. It never authorizes another write.
function retryAfterMs(headers) {
  const value = headers.get("retry-after");
  if (!value || value.length > 128) return undefined;
  const milliseconds = /^\d+$/.test(value) ? Number(value) * 1000 : Date.parse(value) - Date.now();
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return undefined;
  return Math.min(86400000, milliseconds);
}

function transientRead(error) {
  return [429, 502, 503, 504].includes(error?.status) || ["network", "timeout"].includes(error?.kind);
}

function readBackoff(error, attempt, random) {
  const base = Math.min(30000, 2000 * 2 ** Math.min(attempt - 1, 4));
  const jitter = Math.floor(Math.max(0, Math.min(1, random())) * 500);
  return Math.max(base + jitter, error?.meta?.retryAfterMs || 0);
}

function jobReadError(error, id) {
  const safe = error instanceof ApiError ? error : new ApiError("暂时无法查询任务结果。", { kind: "network" });
  const message = safe.status === 429 ? "本次状态查询被限流，原任务可能仍在执行。请稍后查询原任务，不要重新提交。"
    : "暂时无法查询任务结果，后台任务可能仍在执行。请保留任务编号，不要重新提交。";
  return new ApiError(message, { status: safe.status, kind: safe.kind,
    meta: { ...safe.meta, jobId: id, phase: "status", outcome: "uncertain" } });
}

// Read-only SSE connection. Reconnection never repeats a model or tool request.
export async function readJobEvents(id, { after = 0, signal, onEvent = () => {}, fetchImpl = globalThis.fetch,
  timeoutMs = 27000 } = {}) {
  if (!JOB_ID.test(id) || !Number.isSafeInteger(after) || after < 0) throw new ApiError("任务编号无效。", { kind: "response" });
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > 27000) throw new ApiError("实时连接时限无效。", { kind: "configuration" });
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
      const retry = retryAfterMs(response.headers);
      throw new ApiError("实时进度暂不可用，改用任务状态查询。", { status: response.status, kind: "stream",
        meta: retry === undefined ? {} : { retryAfterMs: retry } });
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
  after = 0, delay = delayJob, maxPolls, isCurrent = () => true,
  eventReader = readJobEvents, useEvents = request === apiRequest,
  maxWaitMs = DEFAULT_JOB_WAIT_MS, now = Date.now, random = Math.random } = {}) {
  if (!JOB_ID.test(id)) throw new ApiError("任务编号无效，请勿重复提交。", { kind: "response" });
  if (!Number.isFinite(maxWaitMs) || maxWaitMs <= 0 || maxWaitMs > MAX_JOB_WAIT_MS) throw new ApiError("等待时限无效。", { kind: "configuration" });
  const polls = maxPolls === undefined ? Math.ceil(maxWaitMs / 2000) + 4 : maxPolls;
  if (!Number.isSafeInteger(polls) || polls < 1 || polls > 2000) throw new ApiError("查询次数无效。", { kind: "configuration" });
  const deadline = now() + maxWaitMs;
  let cursor = after, streamFailures = 0, readFailures = 0;
  for (let i = 0; i < polls; i += 1) {
    jobStopped(signal, isCurrent);
    if (now() >= deadline) break;
    let current;
    try {
      current = await request(`/jobs/${id}`, { timeoutMs: Math.max(1, Math.min(15000, deadline - now())), signal });
      readFailures = 0;
    } catch (error) {
      jobStopped(signal, isCurrent);
      const failure = jobReadError(error, id);
      if (!transientRead(error) || ++readFailures > 5) throw failure;
      const waitMs = readBackoff(error, readFailures, random);
      if (waitMs >= deadline - now()) throw failure;
      onEvent({ id: null, event: "transport", data: { label: error.status === 429
        ? "状态查询暂受限，稍后只查询原任务；后台可能继续执行"
        : "状态连接暂不可用，稍后只查询原任务；后台可能继续执行" } });
      await delay(waitMs, signal);
      continue;
    }
    jobStopped(signal, isCurrent);
    if (current.data?.result_expired === true) {
      throw new ApiError(`任务内容已超过保留期限，请勿重复提交。任务编号：${id}`, { kind: "job", meta: { jobId: id, jobStatus: "expired" } });
    }
    const status = current.data?.status;
    if (status === "succeeded" && current.data.result) return { data: current.data.result, meta: current.meta || {} };
    if (["failed", "unknown"].includes(status)) {
      const failureType = safeFailureType(current.data.failure_type);
      const explanation = failureType ? `；${jobFailureLabel(failureType)}（${failureType}）` : "";
      throw new ApiError(`任务未完成（${status === "unknown" ? "结果待核实" : "处理失败"}${explanation}），请勿重复提交。任务编号：${id}`, {
        kind: "job", meta: { jobId: id, jobStatus: status, ...(failureType ? { failureType } : {}) },
      });
    }
    if (!["reserved", "running"].includes(status)) throw new ApiError("任务状态无效，请勿重复提交。", { kind: "response" });
    if (current.data.progress && typeof current.data.progress.label === "string") onEvent({ id: null, event: "progress", data: current.data.progress });
    else onEvent({ id: null, event: "progress", data: { stage: status, label: status === "reserved" ? "任务已入队，等待执行" : "后台正在执行" } });
    if (useEvents && streamFailures < 2) {
      const connectedAt = now();
      try {
        const stream = await eventReader(id, { after: cursor, signal, timeoutMs: Math.max(1, Math.min(27000, deadline - now())), onEvent: (event) => {
          jobStopped(signal, isCurrent);
          if (event.event === "reset" && event.data.cursor_reset === true) cursor = event.id;
          else if (Number.isSafeInteger(event.id)) cursor = Math.max(cursor, event.id);
          onEvent(event);
        } });
        if (Number.isSafeInteger(stream.after) && stream.after >= 0) cursor = stream.after;
        if (!stream.terminal) {
          // An immediately ended stream must not create a reconnect/query loop.
          if (now() - connectedAt < 1500) streamFailures += 1;
          await delay(2000, signal);
        }
        continue;
      } catch (error) {
        jobStopped(signal, isCurrent);
        if (error.status === 401 || error.status === 403) throw jobReadError(error, id);
        streamFailures += 1;
        onEvent({ id: null, event: "transport", data: { label: "实时进度暂不可用，稍后查询原任务状态" } });
        const waitMs = readBackoff(error, streamFailures, random);
        if (waitMs >= deadline - now()) throw jobReadError(error, id);
        await delay(waitMs, signal);
        continue;
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
  let submitted;
  try {
    submitted = await request(kind === "upload" ? "/jobs/upload" : "/jobs", {
      method: "POST", body: kind === "upload" ? payload : { kind, payload },
      headers: { ...headers, "Idempotency-Key": idempotencyKey }, timeoutMs: 30000 });
  } catch (error) {
    const safe = error instanceof ApiError ? error : new ApiError("提交响应未能确认，请先核对任务状态，不要重复提交。", { kind: "network" });
    const rejected = [400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 431].includes(safe.status);
    throw new ApiError(safe.message, { status: safe.status, kind: safe.kind,
      meta: { ...safe.meta, phase: "submission", outcome: rejected ? "rejected" : "uncertain" } });
  }
  const id = submitted.data?.job_id;
  if (typeof id !== "string" || !JOB_ID.test(id)) throw new ApiError("任务编号无效，请勿重复提交。", { kind: "response" });
  onSubmitted({ id, kind, lastSequence: 0, createdAt: Date.now() });
  return resumePublicJob(id, { ...options, signal, request });
}

function detailMessage(value, status, mutating = false) {
  if (status === 429) {
    const reason = quotaReason(value);
    return `${reason ? QUOTA_LABELS[reason] : "本次请求因限流或额度限制被拒绝，请稍后再操作。"}系统没有自动重试；已有任务请查询原编号。`;
  }
  if ([502, 503, 504].includes(status)) return mutating
    ? "上游服务暂时不可用，本次操作是否已被接受尚未核实。系统没有自动重试，请先查询结果，避免重复提交。"
    : "服务暂时不可用，当前无法读取结果。已有任务可能仍在后台执行。";
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
    const retry = retryAfterMs(response.headers);
    if (retry !== undefined) meta.retryAfterMs = retry;
    if (PUBLIC_BUILD && response.status === 401 && !path.startsWith("/auth/") &&
        requestAuthGeneration === authGeneration && authFailureHandler) {
      authFailureHandler();
    }
    let data;
    try { data = await response.json(); }
    catch { throw new ApiError([429, 502, 503, 504].includes(response.status) ? detailMessage(null, response.status, mutating) : "服务返回了无法识别的结果，请根据请求编号排查。", { status: response.status, meta, kind: "response" }); }
    if (!response.ok) {
      const reason = quotaReason(data);
      if (response.status === 429 && reason) meta.quotaReason = reason;
      throw new ApiError(detailMessage(data, response.status, mutating), { status: response.status, meta });
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
