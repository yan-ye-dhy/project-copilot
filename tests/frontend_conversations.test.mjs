import test from 'node:test';
import assert from 'node:assert/strict';
import {
  HISTORY_LIMITS, createConversation, conversationTitle, sanitizeSources,
  sanitizeConversation, boundedConversations, HistoryStore, HistoryStorageError,
} from '../src/project_copilot/web/assets/conversations.js';

const now = Date.now();
const id = (n = 1) => `11111111-1111-4111-8111-${String(n).padStart(12, '0')}`;
const user = (content = '如何查询真实工单？') => ({ role: 'user', content, mode: 'agent' });
const assistant = (content = '请先核对工单与原文。') => ({ role: 'assistant', content, mode: 'agent', final: true });
const conversation = (n = 1, extra = {}) => ({ ...createConversation('agent', id(n), now - n * 1000),
  messages: [user(), assistant()], ...extra });
const source = () => ({ id: `knowledge:${id()}`, kind: 'knowledge', data: {
  text: '原始资料', metadata: { source_name: '研发.md', chunk_id: id(), document_id: id(2), page: 0 } } });

test('a blank conversation is usable in memory but does not become a history entry', () => {
  const blank = createConversation('rag', id(), now);
  assert.equal(blank.mode, 'rag'); assert.equal(blank.title, '新对话');
  assert.equal(sanitizeConversation(blank, now), null);
  assert.deepEqual(boundedConversations([blank], now), []);
  assert.throws(() => createConversation('agent', 'invalid', now), TypeError);
});

test('automatic title comes from the first user question and counts Unicode codepoints', () => {
  const content = '竹😀'.repeat(16);
  assert.equal(conversationTitle([{ role: 'assistant', content: 'ignore' }, user(content), user('later')]), '竹😀'.repeat(10));
  assert.equal(conversationTitle([user('  查询\n\t真实工单  ')]), '查询 真实工单');
  assert.equal(conversationTitle([]), '新对话');
  assert.equal(sanitizeConversation(conversation(1, { title: '新对话' }), now).title, '如何查询真实工单？');
});

test('renamed titles survive normalization as literal text without executable HTML', () => {
  const result = sanitizeConversation(conversation(1, { title: ' <img src=x> ' }), now);
  assert.equal(result.title, '<img src=x>');
  assert.equal(sanitizeConversation(conversation(1, { title: {} }), now).title, '如何查询真实工单？');
});

test('only final assistant answers are admitted, never progress drafts or system/tool messages', () => {
  const messages = [user(), { role: 'assistant', content: '未校验草稿' }, { role: 'assistant', content: '草稿', final: false },
    { role: 'system', content: 'private system prompt' }, { role: 'tool', content: 'raw tool arguments' }, assistant()];
  assert.deepEqual(sanitizeConversation(conversation(1, { messages }), now).messages, [user(), { ...assistant(), sources: [] }]);
});

test('conversation, message and display metadata discard execution credentials and unknown fields', () => {
  const value = conversation(1, { context_token: 'SECRET_CONTEXT', owner: 'other', bearer: 'SECRET_BEARER',
    messages: [{ ...user(), csrf: 'SECRET_CSRF', context: { token: 'SECRET' } }, { ...assistant(),
      token: 'SECRET', tool_calls: [{ args: 'SECRET' }], generation: { system: 'SECRET' },
      details: { runId: id(), traceId: 'a'.repeat(32), traceExported: true, elapsed_ms: 11.3,
        model_calls: 2, tool_calls: 3, tool_executions: 2, tool_failures: 1,
        usage: { prompt_tokens: 11, completion_tokens: null, total_tokens: 13, private: 'SECRET' },
        failure_type: 'tool_budget', api_key: 'SECRET' } }],
    jobs: [{ id: id(3), kind: 'agent', createdAt: now - 3000, status: 'unknown', context_token: 'SECRET' }] });
  const result = sanitizeConversation(value, now);
  assert.deepEqual(result.jobs, [{ id: id(3), kind: 'agent', createdAt: now - 3000 }]);
  assert.deepEqual(result.messages[1].details.usage, { prompt_tokens: 11, total_tokens: 13 });
  assert.equal(result.messages[1].details.failure_type, 'tool_budget');
  assert.doesNotMatch(JSON.stringify(result), /SECRET|bearer|api_key|context_token|generation|tool_calls.*args|system/);
});

test('unknown or invalid usage remains absent rather than being converted to zero', () => {
  const value = conversation(1, { messages: [user(), { ...assistant(), details: {
    usage: { prompt_tokens: null, total_tokens: -1, completion_tokens: 4.2 }, model_calls: 1.2,
    failure_type: 'private provider error SECRET' } }] });
  assert.equal(sanitizeConversation(value, now).messages[1].details, undefined);
});

test('source copies keep citations but strip permissions, raw tool arguments and arbitrary metadata', () => {
  const knowledge = source(); knowledge.data.metadata.context_token = 'SECRET'; knowledge.data.tool_args = 'SECRET';
  const proposal = { id: `proposal:${id(4)}`, kind: 'proposal', data: { action_id: id(4), status: 'pending',
    payload_fingerprint: 'SECRET', context_token: 'SECRET', payload: { title: '待确认', description: '旧提案说明', tool_args: 'SECRET' } } };
  const ticket = { id: `ticket:${id(5)}`, kind: 'ticket', data: { id: id(5), title: '真实工单', description: '说明',
    status: 'open', owner_id: id(8), token: 'SECRET' } };
  const sources = sanitizeSources([knowledge, proposal, ticket]);
  assert.deepEqual(sources[0], source());
  assert.deepEqual(sources[1], { id: `proposal:${id(4)}`, kind: 'proposal', data: { payload: { title: '待确认', description: '旧提案说明' } } });
  assert.deepEqual(sources[2].data, { id: id(5), title: '真实工单', description: '说明', status: 'open' });
  assert.doesNotMatch(JSON.stringify(sources), /SECRET|owner_id|action_id|fingerprint|context_token|tool_args/);
});

test('invalid source types and oversized source text do not turn into fabricated evidence', () => {
  const tooLarge = source(); tooLarge.data.text = '竹'.repeat(12001);
  assert.deepEqual(sanitizeSources([null, {}, { id: 'system', kind: 'system', data: {} }, tooLarge]), []);
  assert.equal(sanitizeSources(Array.from({ length: 20 }, source)).length, 12);
});

test('malformed timestamps and stale conversations are rejected without throwing', () => {
  for (const extra of [{ id: 'invalid' }, { createdAt: now + 61000 }, { updatedAt: now - 31 * 86400000 },
    { createdAt: now, updatedAt: now - 1 }, { updatedAt: 'today' }, { messages: {} }]) {
    assert.equal(sanitizeConversation(conversation(1, extra), now), null);
  }
  assert.equal(sanitizeConversation(null, now), null);
  assert.equal(sanitizeConversation(conversation(1, { createdAt: now - 60 * 86400000, updatedAt: now }), now).createdAt, now - 60 * 86400000);
});

test('message retention is bounded without changing a manually set or original question title', () => {
  const messages = Array.from({ length: 70 }, (_, i) => user(`问题${i}`));
  const result = sanitizeConversation(conversation(1, { messages }), now);
  assert.equal(result.messages.length, HISTORY_LIMITS.messages);
  assert.equal(result.title, '问题0'); assert.equal(result.messages[0].content, '问题20');
});

test('a pending job identifier remains recoverable without persisting a guessed job state', () => {
  const value = conversation(1, { messages: [], jobs: [{ id: id(3), kind: 'rag', createdAt: now,
    status: 'running', raw_response: 'SECRET' }, { id: id(3), kind: 'rag', createdAt: now }] });
  assert.deepEqual(sanitizeConversation(value, now).jobs, [{ id: id(3), kind: 'rag', createdAt: now }]);
});

test('oversized individual conversations are not persisted as a falsely complete truncated answer', () => {
  const value = conversation(1, { messages: Array.from({ length: 4 }, () => assistant('竹'.repeat(30000))) });
  assert.equal(sanitizeConversation(value, now), null);
  assert.equal(sanitizeConversation(conversation(1, { messages: [user(), assistant('x'.repeat(32001))] }), now).messages.length, 1);
});

test('account count, total byte limit and duplicate identifiers keep the newest bounded snapshots', () => {
  const values = Array.from({ length: 60 }, (_, i) => conversation(i + 1, {
    messages: [user(), ...Array.from({ length: 7 }, () => assistant('a'.repeat(30000)))] }));
  values.push(conversation(1, { title: '新版', updatedAt: now }));
  const results = boundedConversations(values, now);
  assert.ok(results.length <= HISTORY_LIMITS.conversations);
  assert.equal(results[0].title, '新版');
  assert.equal(new Set(results.map((item) => item.id)).size, results.length);
  assert.ok(results.reduce((sum, item) => sum + new TextEncoder().encode(JSON.stringify(item)).length, 0) <= HISTORY_LIMITS.accountBytes);
  assert.deepEqual(boundedConversations({}), []);
});

test('missing IndexedDB degrades through a safe bounded error without using localStorage', async () => {
  const store = new HistoryStore({ indexedDB: null });
  await assert.rejects(store.load(id()), HistoryStorageError);
  await assert.rejects(store.save(id(), [conversation()]), HistoryStorageError);
  await assert.rejects(store.clear(id()), HistoryStorageError);
});

test('a browser policy that throws from the global IndexedDB getter cannot crash application initialization', async () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'indexedDB');
  try {
    Object.defineProperty(globalThis, 'indexedDB', { configurable: true,
      get() { throw new DOMException('SECRET_POLICY_DETAIL', 'SecurityError'); } });
    const store = new HistoryStore();
    await assert.rejects(store.load(id()), HistoryStorageError);
    await assert.rejects(store.save(id(), [conversation()]), HistoryStorageError);
  } finally {
    if (previous) Object.defineProperty(globalThis, 'indexedDB', previous);
    else delete globalThis.indexedDB;
  }
});

test('denied option or open-property getters degrade without leaking their original exception', async () => {
  const deniedOptions = new Proxy({}, { getOwnPropertyDescriptor() { throw Error('SECRET_OPTION'); } });
  await assert.rejects(new HistoryStore(deniedOptions).load(id()), HistoryStorageError);
  const factory = Object.defineProperty({}, 'open', { get() { throw Error('SECRET_OPEN'); } });
  await assert.rejects(new HistoryStore({ indexedDB: factory }).load(id()), HistoryStorageError);
});

test('an unverified or malformed owner never opens browser storage', async () => {
  const store = new HistoryStore({ indexedDB: { open() { assert.fail('must not open'); } } });
  for (const owner of [null, '', '../other', 'alice', {}]) {
    await assert.rejects(store.load(owner), HistoryStorageError);
    await assert.rejects(store.save(owner, []), HistoryStorageError);
    await assert.rejects(store.clear(owner), HistoryStorageError);
  }
});

test('blocked or failed browser storage never exposes raw provider or device errors', async () => {
  for (const event of ['onerror', 'onblocked']) {
    const indexedDB = { open() { const request = { error: new Error('SECRET_DEVICE_ERROR') }; queueMicrotask(() => request[event]?.()); return request; } };
    await assert.rejects(new HistoryStore({ indexedDB }).load(id()), (error) => {
      assert.ok(error instanceof HistoryStorageError); assert.doesNotMatch(error.message, /SECRET/); return true;
    });
  }
});

test('a stalled open times out and closes a connection that succeeds too late', async () => {
  let request, closed = false;
  const indexedDB = { open() { request = {}; return request; } };
  await assert.rejects(new HistoryStore({ indexedDB, timeoutMs: 5 }).load(id()), HistoryStorageError);
  request.result = { close() { closed = true; } }; request.onsuccess();
  assert.equal(closed, true);
});

test('a transaction initialization failure is closed and represented as unavailable storage', async () => {
  let closed = false;
  const db = { close() { closed = true; }, transaction() { throw Error('SECRET_TRANSACTION_ERROR'); } };
  const indexedDB = { open() { const request = { result: db }; queueMicrotask(() => request.onsuccess()); return request; } };
  await assert.rejects(new HistoryStore({ indexedDB }).load(id()), HistoryStorageError);
  assert.equal(closed, true);
});

test('a stalled transaction is aborted before fallback and never treated as a successful save', async () => {
  let aborted = false, closed = false;
  const pendingRequest = () => ({});
  const tx = { abort() { aborted = true; }, objectStore() { return { index() { return { getAll: pendingRequest,
    getAllKeys: pendingRequest }; }, get: pendingRequest }; } };
  const db = { close() { closed = true; }, transaction() { return tx; } };
  const indexedDB = { open() { const request = { result: db }; queueMicrotask(() => request.onsuccess()); return request; } };
  await assert.rejects(new HistoryStore({ indexedDB, timeoutMs: 5 }).load(id()), HistoryStorageError);
  assert.equal(aborted, true); assert.equal(closed, true);
});

test('a synchronous storage quota or write error inside an IDB callback aborts atomically and degrades safely', async () => {
  let aborted = false, closed = false;
  const store = { index() { return { getAllKeys() {
    const request = { result: [[id(), id(2)]] };
    queueMicrotask(() => request.onsuccess()); return request;
  } }; }, delete() { throw Error('SECRET_QUOTA_FAILURE'); } };
  const tx = { abort() { aborted = true; }, objectStore() { return store; } };
  const db = { close() { closed = true; }, transaction() { return tx; } };
  const indexedDB = { open() { const request = { result: db }; queueMicrotask(() => request.onsuccess()); return request; } };
  await assert.rejects(new HistoryStore({ indexedDB }).save(id(), [conversation()]), HistoryStorageError);
  assert.equal(aborted, true); assert.equal(closed, true);
});
