import test from "node:test";
import assert from "node:assert/strict";

import {
  ApiError,
  apiRequest,
  ContextSession,
  boundHistory,
  validateAction,
} from "../src/project_copilot/web/assets/api.js";

const CONTEXT_ID = "11111111-1111-4111-8111-111111111111";
const ACTION_ID = "22222222-2222-4222-8222-222222222222";
const TICKET_ID = "33333333-3333-4333-8333-333333333333";
const NEXT_CONTEXT_ID = "44444444-4444-4444-8444-444444444444";
const TOKEN = "t".repeat(43);

function response(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function credentials(overrides = {}) {
  return {
    context_id: CONTEXT_ID,
    context_token: TOKEN,
    expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
    ...overrides,
  };
}

function action(overrides = {}) {
  return {
    action_id: ACTION_ID,
    context_id: CONTEXT_ID,
    status: "pending",
    payload: { title: "蓝鹤排障", description: "核对数据库连接配置与服务就绪状态。" },
    payload_fingerprint: "a".repeat(64),
    created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
    ticket_id: null,
    ...overrides,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("JSON requests stay same-origin and preserve the real response trace metadata", async () => {
  const calls = [];
  const result = await apiRequest("/agent/ask", {
    method: "POST",
    body: { question: "如何排查？", history: [] },
    fetchImpl: async (path, options) => {
      calls.push({ path, options });
      return response({ status: "answered", answer: "检查连接配置。" }, 200, {
        "X-Run-ID": "run-123",
        "X-Trace-ID": "trace-456",
        "X-Trace-Exported": "true",
      });
    },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/agent/ask");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].options.body), { question: "如何排查？", history: [] });
  assert.deepEqual(result.meta, { runId: "run-123", traceId: "trace-456", traceExported: true });
  assert.equal(result.data.status, "answered");
});

test("network failure makes one write attempt and reports an unverified result", async () => {
  let calls = 0;
  await assert.rejects(apiRequest("/actions/preview", {
    method: "POST", body: { title: "排障", description: "说明" },
    fetchImpl: async () => { calls += 1; throw new TypeError("private network details"); },
  }), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.kind, "network");
    assert.equal(error.status, 0);
    assert.doesNotMatch(error.message, /private network details/);
    assert.match(error.message, /查询结果/);
    return true;
  });
  assert.equal(calls, 1);
});

test("an HTTP rejection keeps its status and run_id without retrying the action", async () => {
  let calls = 0;
  await assert.rejects(apiRequest(`/actions/${ACTION_ID}/confirm`, {
    method: "POST", body: { payload_fingerprint: "a".repeat(64) },
    fetchImpl: async () => {
      calls += 1;
      return response({ detail: "提案指纹不匹配" }, 409, {
        "X-Run-ID": "conflict-run", "X-Trace-ID": "conflict-trace", "X-Trace-Exported": "false",
      });
    },
  }), (error) => {
    assert.equal(error.status, 409);
    assert.equal(error.kind, "http");
    assert.equal(error.message, "提案指纹不匹配");
    assert.deepEqual(error.meta, { runId: "conflict-run", traceId: "conflict-trace", traceExported: false });
    return true;
  });
  assert.equal(calls, 1);
});

test("validation feedback summarizes messages without echoing submitted private values", async () => {
  await assert.rejects(apiRequest("/agent/ask", {
    fetchImpl: async () => response({ detail: [
      { msg: "Question is too long", input: "PRIVATE-CONTENT" },
      { msg: "History is too long" },
      { msg: "Third diagnostic" },
    ] }, 422),
  }), (error) => {
    assert.equal(error.status, 422);
    assert.equal(error.message, "Question is too long；History is too long");
    assert.doesNotMatch(error.message, /PRIVATE|Third/);
    return true;
  });
});

test("non-JSON service responses retain diagnostic metadata and fail closed", async () => {
  await assert.rejects(apiRequest("/tickets", {
    fetchImpl: async () => new Response("<html>upstream gateway</html>", {
      status: 502, headers: { "X-Run-ID": "bad-response-run" },
    }),
  }), (error) => {
    assert.equal(error.kind, "response");
    assert.equal(error.status, 502);
    assert.equal(error.meta.runId, "bad-response-run");
    assert.doesNotMatch(error.message, /<html>/);
    return true;
  });
});

test("multipart upload leaves the boundary Content-Type to the browser", async () => {
  const body = new FormData();
  body.append("file", new Blob(["中文资料"], { type: "text/markdown" }), "knowledge.md");
  let calls = 0;
  await apiRequest("/documents/upload", {
    method: "POST", body,
    fetchImpl: async (_path, options) => {
      calls += 1;
      assert.strictEqual(options.body, body);
      assert.ok(!Object.keys(options.headers).some((name) => name.toLowerCase() === "content-type"));
      assert.equal(options.headers.Accept, "application/json");
      return response({ status: "indexed" });
    },
  });
  assert.equal(calls, 1);
});

test("external and protocol-relative paths are rejected before sending credentials", async () => {
  let calls = 0;
  for (const path of ["https://example.test/tickets", "//example.test/tickets", "tickets", "/redirect/https://example.test"]) {
    await assert.rejects(apiRequest(path, {
      fetchImpl: async () => { calls += 1; return response({}); },
    }), (error) => error instanceof ApiError && error.kind === "configuration");
  }
  assert.equal(calls, 0);
});

test("request timeout aborts a single attempt and warns that the write may have completed", async () => {
  let calls = 0;
  await assert.rejects(apiRequest(`/actions/${ACTION_ID}/confirm`, {
    method: "POST", body: { payload_fingerprint: "a".repeat(64) }, timeoutMs: 5,
    fetchImpl: async (_path, { signal }) => {
      calls += 1;
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => {
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true }));
    },
  }), (error) => error.kind === "timeout" && /尚未核实/.test(error.message));
  assert.equal(calls, 1);
});

test("caller cancellation propagates without classifying it as a model failure", async () => {
  const controller = new AbortController();
  let calls = 0;
  const pending = apiRequest("/tickets", {
    signal: controller.signal,
    fetchImpl: async (_path, { signal }) => {
      calls += 1;
      return new Promise((_resolve, reject) => signal.addEventListener("abort", () => {
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true }));
    },
  });
  controller.abort();
  await assert.rejects(pending, (error) => error.kind === "aborted");
  assert.equal(calls, 1);
});

test("concurrent context acquisition shares one creation and reuses the valid context", async () => {
  const request = deferred();
  let calls = 0;
  const session = new ContextSession((path, options) => {
    calls += 1;
    assert.equal(path, "/agent/contexts");
    assert.equal(options.method, "POST");
    return request.promise;
  });
  const first = session.ensure();
  const second = session.ensure();
  assert.equal(calls, 1);
  request.resolve({ data: credentials() });
  const [one, two] = await Promise.all([first, second]);
  assert.strictEqual(one, two);
  assert.ok(Object.isFrozen(one));
  assert.strictEqual(await session.ensure(), one);
  assert.equal(calls, 1);
  assert.deepEqual(ContextSession.headers(one), {
    "X-Copilot-Context-ID": CONTEXT_ID,
    "X-Copilot-Context-Token": TOKEN,
  });
});

test("nearly expired context refreshes before another privileged request", async () => {
  let calls = 0;
  const session = new ContextSession(async () => {
    calls += 1;
    return { data: credentials({ context_id: NEXT_CONTEXT_ID }) };
  });
  session.current = credentials({ expires_at: new Date(Date.now() + 10000).toISOString() });
  const refreshed = await session.ensure();
  assert.equal(refreshed.context_id, NEXT_CONTEXT_ID);
  assert.equal(calls, 1);
});

test("clearing the conversation prevents an old in-flight context from replacing the new one", async () => {
  const oldRequest = deferred();
  const newRequest = deferred();
  let calls = 0;
  const session = new ContextSession(() => (++calls === 1 ? oldRequest.promise : newRequest.promise));
  const old = session.ensure();
  session.clear();
  const current = session.ensure();
  oldRequest.resolve({ data: credentials() });
  await old;
  assert.equal(session.current, null);
  const alsoCurrent = session.ensure();
  assert.equal(calls, 2);
  newRequest.resolve({ data: credentials({ context_id: NEXT_CONTEXT_ID }) });
  const [one, two] = await Promise.all([current, alsoCurrent]);
  assert.strictEqual(one, two);
  assert.strictEqual(session.current, one);
  assert.equal(one.context_id, NEXT_CONTEXT_ID);
});

test("context failure does not retry automatically and a later explicit acquisition can recover", async () => {
  let calls = 0;
  const session = new ContextSession(async () => {
    calls += 1;
    if (calls === 1) throw new ApiError("数据库暂不可用", { status: 503 });
    return { data: credentials() };
  });
  await assert.rejects(session.ensure(), (error) => error.status === 503);
  assert.equal(calls, 1);
  assert.equal(session.current, null);
  assert.equal(session.pending, null);
  assert.equal((await session.ensure()).context_id, CONTEXT_ID);
  assert.equal(calls, 2);
});

test("malformed context data cannot become the active capability", async () => {
  for (const data of [null, {}, credentials({ context_token: "" }), credentials({ expires_at: "not-a-date" })]) {
    const session = new ContextSession(async () => ({ data }));
    await assert.rejects(session.ensure(), (error) => error instanceof ApiError && error.kind === "response");
    assert.equal(session.current, null);
    assert.equal(session.pending, null);
  }
});

test("context capabilities never access browser localStorage or sessionStorage", async () => {
  const descriptors = new Map();
  let accesses = 0;
  for (const key of ["localStorage", "sessionStorage"]) {
    descriptors.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, {
      configurable: true,
      get() { accesses += 1; throw new Error("Capability must remain in memory"); },
    });
  }
  try {
    const session = new ContextSession(async () => ({ data: credentials() }));
    const context = await session.ensure();
    assert.equal(ContextSession.headers(context)["X-Copilot-Context-Token"], TOKEN);
    session.clear();
    assert.equal(session.current, null);
    assert.equal(accesses, 0);
  } finally {
    for (const [key, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});

test("history excludes unsupported roles, empty input, and non-text values without mutating messages", () => {
  const messages = [
    { role: "system", content: "untrusted instructions" },
    { role: "tool", content: "fabricated tool result" },
    { role: "user", content: "  如何排查？  " },
    { role: "assistant", content: "  查看证据。  " },
    { role: "user", content: "  " },
    { role: "assistant", content: 42 },
  ];
  const original = structuredClone(messages);
  assert.deepEqual(boundHistory(messages), [
    { role: "user", content: "如何排查？" },
    { role: "assistant", content: "查看证据。" },
  ]);
  assert.deepEqual(messages, original);
});

test("history keeps only the latest eight supported messages", () => {
  const messages = Array.from({ length: 12 }, (_, index) => ({
    role: index % 2 === 0 ? "user" : "assistant", content: `message-${index}`,
  }));
  const result = boundHistory(messages);
  assert.equal(result.length, 8);
  assert.equal(result[0].content, "message-4");
  assert.equal(result.at(-1).content, "message-11");
});

test("history bounds individual and total text while keeping the latest user turn", () => {
  const result = boundHistory([
    { role: "user", content: "a".repeat(6000) },
    { role: "assistant", content: "b".repeat(6000) },
    { role: "user", content: "c".repeat(5000) },
    { role: "assistant", content: "d".repeat(2000) },
  ]);
  assert.ok(result.every((item) => item.content.length <= 4000));
  assert.ok(result.reduce((sum, item) => sum + item.content.length, 0) <= 10000);
  assert.equal(result[0].role, "user");
  assert.equal(result[0].content, "c".repeat(4000));
  assert.equal(result.at(-1).content, "d".repeat(2000));
});

test("history omits a detached assistant response at the beginning", () => {
  assert.deepEqual(boundHistory([
    { role: "assistant", content: "旧回答" },
    { role: "user", content: "新问题" },
  ]), [{ role: "user", content: "新问题" }]);
});

test("history counts Unicode codepoints and never cuts an emoji surrogate pair", () => {
  const truncated = boundHistory([{ role: "user", content: `${"中".repeat(3999)}😀末` }]);
  assert.equal(Array.from(truncated[0].content).length, 4000);
  assert.ok(truncated[0].content.endsWith("😀"));
  assert.equal(truncated[0].content.isWellFormed(), true);
  assert.equal(new TextDecoder().decode(new TextEncoder().encode(truncated[0].content)), truncated[0].content);

  // Python/Pydantic counts 7,500 characters here; UTF-16 length is 15,000.
  // All three turns fit the backend's 10,000-character aggregate budget.
  const messages = [
    { role: "user", content: "😀".repeat(2500) },
    { role: "assistant", content: "😃".repeat(2500) },
    { role: "user", content: "😄".repeat(2500) },
  ];
  const preserved = boundHistory(messages);
  assert.deepEqual(preserved, messages);
  assert.equal(preserved.reduce((sum, item) => sum + Array.from(item.content).length, 0), 7500);
});

test("validated proposals are immutable snapshots of the backend preview", () => {
  const input = action();
  const result = validateAction(input, credentials());
  assert.notStrictEqual(result, input);
  assert.notStrictEqual(result.payload, input.payload);
  assert.ok(Object.isFrozen(result));
  assert.ok(Object.isFrozen(result.payload));
  assert.throws(() => { result.payload.title = "tampered"; }, TypeError);
  assert.equal(input.payload.title, "蓝鹤排障");
});

test("proposals from a different context cannot be confirmed", () => {
  assert.throws(() => validateAction(action({ context_id: NEXT_CONTEXT_ID }), credentials()),
    (error) => error instanceof ApiError && error.kind === "response");
});

test("a proposal requires the backend fingerprint and an allowed terminal or pending state", () => {
  for (const invalid of ["", "A".repeat(64), "a".repeat(63), "not-a-fingerprint"]) {
    assert.throws(() => validateAction(action({ payload_fingerprint: invalid }), credentials()), ApiError);
  }
  assert.throws(() => validateAction(action({ status: "approved" }), credentials()), ApiError);
  for (const status of ["pending", "cancelled", "expired"]) {
    assert.equal(validateAction(action({ status }), credentials()).status, status);
  }
});

test("invalid proposal identity, expiry, or empty preview prevents confirmation", () => {
  for (const invalid of [
    action({ action_id: "" }),
    action({ action_id: "not-an-action-id" }),
    action({ expires_at: "not-a-date" }),
    action({ payload: { title: " ", description: "说明" } }),
    action({ payload: { title: "标题", description: " " } }),
    null,
  ]) {
    assert.throws(() => validateAction(invalid, credentials()), ApiError);
  }
});

test("completed means a real ticket UUID is returned, rather than a successful HTTP response alone", () => {
  for (const ticket_id of [undefined, null, "", "created-successfully", 123]) {
    assert.throws(() => validateAction(action({ status: "completed", ticket_id }), credentials()), ApiError);
  }
  const completed = validateAction(action({ status: "completed", ticket_id: TICKET_ID }), credentials());
  assert.equal(completed.ticket_id, TICKET_ID);
});
