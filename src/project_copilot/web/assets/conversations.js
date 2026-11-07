// Display-only browser history. Execution contexts and permission-bearing data
// must remain in the caller's memory and never cross this whitelist.
import { safeFailureType } from "./api.js";

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const MODES = new Set(["agent", "rag", "mixed"]);
export const HISTORY_LIMITS = Object.freeze({ conversations: 50, messages: 50,
  retentionMs: 30 * 86400000, conversationBytes: 256 * 1024, accountBytes: 5 * 1024 * 1024 });
const bytes = (value) => new TextEncoder().encode(JSON.stringify(value)).length;
const text = (value, limit) => typeof value === "string" && value.trim() && Array.from(value).length <= limit ? value : null;
const identifier = (value) => typeof value === "string" && /^[a-z0-9:._-]{1,150}$/i.test(value) ? value : null;
const timestamp = (value, now) => Number.isSafeInteger(value) && value > 0 && value <= now + 60000;

export function conversationTitle(messages) {
  const first = Array.isArray(messages) && messages.find((message) => message?.role === "user" && typeof message.content === "string" && message.content.trim());
  return first ? Array.from(first.content.trim().replace(/\s+/g, " ")).slice(0, 20).join("") : "新对话";
}

export function createConversation(mode = "agent", id = globalThis.crypto.randomUUID(), now = Date.now()) {
  if (!UUID.test(id) || !timestamp(now, now)) throw new TypeError("Invalid conversation identifier");
  return { id, title: "新对话", createdAt: now, updatedAt: now,
    mode: MODES.has(mode) ? mode : "agent", messages: [], jobs: [] };
}

export function sanitizeSources(value) {
  if (!Array.isArray(value)) return [];
  const sources = [];
  for (const source of value.slice(0, 12)) {
    if (!source || !["knowledge", "ticket", "proposal"].includes(source.kind)) continue;
    const id = identifier(source.id), data = source.data;
    if (!id || !data || typeof data !== "object") continue;
    if (source.kind === "knowledge") {
      const original = text(data.text, 12000), name = text(data.metadata?.source_name, 250);
      if (!original || !name) continue;
      const metadata = { source_name: name };
      for (const field of ["chunk_id", "document_id"]) {
        const safe = identifier(data.metadata[field]); if (safe) metadata[field] = safe;
      }
      if (data.metadata.page === null || (Number.isSafeInteger(data.metadata.page) && data.metadata.page >= 0)) metadata.page = data.metadata.page;
      sources.push({ id, kind: "knowledge", data: { text: original, metadata } });
    } else if (source.kind === "ticket") {
      const title = text(data.title, 200), description = text(data.description, 10000);
      if (!title) continue;
      const safe = { title };
      if (UUID.test(data.id)) safe.id = data.id;
      if (description) safe.description = description;
      if (["open", "in_progress", "closed"].includes(data.status)) safe.status = data.status;
      sources.push({ id, kind: "ticket", data: safe });
    } else {
      const title = text(data.payload?.title, 200), description = text(data.payload?.description, 10000);
      if (!title) continue;
      // A proposal is an old textual exhibit, not a recovered executable action.
      sources.push({ id, kind: "proposal", data: { payload: { title, ...(description ? { description } : {}) } } });
    }
  }
  return sources;
}

function sanitizeDetails(value) {
  if (!value || typeof value !== "object") return undefined;
  const details = {};
  for (const field of ["runId", "traceId"]) if (identifier(value[field])) details[field] = value[field];
  if (typeof value.traceExported === "boolean") details.traceExported = value.traceExported;
  for (const field of ["elapsed_ms", "model_calls", "tool_calls", "tool_executions", "tool_failures"]) {
    if ((field === "elapsed_ms" ? Number.isFinite(value[field]) : Number.isSafeInteger(value[field])) && value[field] >= 0 && value[field] < 1e12) details[field] = value[field];
  }
  const usage = {};
  for (const field of ["prompt_tokens", "completion_tokens", "total_tokens"]) {
    if (Number.isSafeInteger(value.usage?.[field]) && value.usage[field] >= 0) usage[field] = value.usage[field];
  }
  if (Object.keys(usage).length) details.usage = usage;
  if (safeFailureType(value.failure_type)) details.failure_type = value.failure_type;
  return Object.keys(details).length ? details : undefined;
}

function sanitizeMessage(value) {
  if (!value || !["user", "assistant"].includes(value.role)) return null;
  const content = text(value.content, value.role === "user" ? 2000 : 32000);
  if (!content || (value.role === "assistant" && value.final !== true)) return null;
  const message = { role: value.role, content, mode: value.mode === "rag" ? "rag" : "agent" };
  if (value.role === "assistant") {
    message.final = true; message.sources = sanitizeSources(value.sources);
    const details = sanitizeDetails(value.details); if (details) message.details = details;
  }
  return message;
}

export function sanitizeConversation(value, now = Date.now()) {
  if (!value || !UUID.test(value.id) || !timestamp(value.createdAt, now) || !timestamp(value.updatedAt, now) ||
      value.updatedAt < value.createdAt || value.updatedAt <= now - HISTORY_LIMITS.retentionMs || !Array.isArray(value.messages)) return null;
  const messages = value.messages.map(sanitizeMessage).filter(Boolean).slice(-HISTORY_LIMITS.messages);
  const jobs = [], seen = new Set();
  if (Array.isArray(value.jobs)) for (const job of value.jobs.slice(-32)) {
    if (!job || !UUID.test(job.id) || !["rag", "agent"].includes(job.kind) || !timestamp(job.createdAt, now) || seen.has(job.id)) continue;
    seen.add(job.id); jobs.push({ id: job.id, kind: job.kind, createdAt: job.createdAt });
  }
  if (!messages.length && !jobs.length) return null;
  const title = text(typeof value.title === "string" ? value.title.trim() : null, 60);
  const result = { id: value.id, title: title && title !== "新对话" ? title : conversationTitle(value.messages),
    createdAt: value.createdAt, updatedAt: value.updatedAt, mode: MODES.has(value.mode) ? value.mode : "agent", messages, jobs };
  return bytes(result) <= HISTORY_LIMITS.conversationBytes ? result : null;
}

export function boundedConversations(value, now = Date.now()) {
  if (!Array.isArray(value)) return [];
  const unique = new Map();
  for (const item of value) {
    const clean = sanitizeConversation(item, now);
    if (clean && (!unique.has(clean.id) || clean.updatedAt >= unique.get(clean.id).updatedAt)) unique.set(clean.id, clean);
  }
  const result = []; let total = 0;
  for (const item of [...unique.values()].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, HISTORY_LIMITS.conversations)) {
    const size = bytes(item);
    if (total + size <= HISTORY_LIMITS.accountBytes) { result.push(item); total += size; }
  }
  return result;
}

export class HistoryStorageError extends Error {
  constructor() { super("本机历史存储不可用，当前对话仅保留在本页。"); this.name = "HistoryStorageError"; }
}

export class HistoryConflictError extends Error {
  constructor() { super("其他标签页已更新此账号的本机历史；本页内容暂留内存，请刷新页面后再保存。"); this.name = "HistoryConflictError"; }
}

const revisionOf = (preference) => {
  const revision = preference?.revision ?? 0;
  if (!Number.isSafeInteger(revision) || revision < 0 || revision >= Number.MAX_SAFE_INTEGER) throw new HistoryStorageError();
  return revision;
};

export class HistoryStore {
  constructor(options = {}) {
    this.indexedDB = null;
    let timeoutMs = 2500;
    try {
      // Some private/device policy modes throw even while reading the global
      // getter. Construction must still succeed so the app can use memory.
      const settings = options && typeof options === "object" ? options : {};
      this.indexedDB = Object.hasOwn(settings, "indexedDB") ? settings.indexedDB : globalThis.indexedDB;
      timeoutMs = settings.timeoutMs ?? 2500;
    } catch { this.indexedDB = null; }
    this.timeoutMs = Number.isFinite(timeoutMs) && timeoutMs > 0 ? Math.min(timeoutMs, 10000) : 2500;
  }

  _owner(owner) { if (typeof owner !== "string" || !UUID.test(owner)) throw new HistoryStorageError(); return owner; }

  _open() {
    if (!this.indexedDB) return Promise.reject(new HistoryStorageError());
    return new Promise((resolve, reject) => {
      let request, done = false;
      const fail = () => { if (!done) { done = true; clearTimeout(timer); reject(new HistoryStorageError()); } };
      const timer = setTimeout(fail, this.timeoutMs);
      try {
        if (typeof this.indexedDB.open !== "function") { fail(); return; }
        // Bump the schema so an old deployed tab cannot keep using the v1
        // unconditional writer after a current tab has upgraded the database.
        request = this.indexedDB.open("project-copilot-chat-history-v1", 2);
        request.onupgradeneeded = () => {
          if (done) { try { request.transaction?.abort(); } catch {} return; }
          try {
            const db = request.result;
            if (!db.objectStoreNames.contains("conversations")) db.createObjectStore("conversations", { keyPath: ["owner", "id"] }).createIndex("owner", "owner");
            if (!db.objectStoreNames.contains("preferences")) db.createObjectStore("preferences", { keyPath: "owner" });
          } catch { request.transaction?.abort(); fail(); }
        };
        request.onblocked = request.onerror = fail;
        request.onsuccess = () => {
          if (done) { request.result.close(); return; }
          done = true; clearTimeout(timer);
          request.result.onversionchange = () => request.result.close();
          resolve(request.result);
        };
      } catch { fail(); }
    });
  }

  async _transaction(work) {
    const db = await this._open();
    return new Promise((resolve, reject) => {
      let tx, done = false, result;
      const finish = (error) => {
        if (done) return; done = true; clearTimeout(timer); db.close();
        if (error) reject(error instanceof HistoryConflictError ? error : new HistoryStorageError()); else resolve(result);
      };
      const timer = setTimeout(() => { try { tx?.abort(); } catch {} finish(true); }, this.timeoutMs);
      try {
        tx = db.transaction(["conversations", "preferences"], "readwrite");
        tx.oncomplete = () => finish(false);
        tx.onerror = tx.onabort = () => finish(true);
        const guarded = (handler) => (...args) => {
          if (done) return;
          try { handler(...args); }
          catch (error) { try { tx.abort(); } catch {} finish(error); }
        };
        work(tx, (value) => { result = value; }, guarded);
      } catch { try { tx?.abort(); } catch {} finish(true); }
    });
  }

  async load(owner) {
    this._owner(owner);
    return this._transaction((tx, setResult, guarded) => {
      const rows = tx.objectStore("conversations"), prefs = tx.objectStore("preferences");
      const data = rows.index("owner").getAll(owner), keys = rows.index("owner").getAllKeys(owner), preference = prefs.get(owner);
      let remaining = 3;
      const ready = () => {
        if (--remaining) return;
        const enabled = preference.result?.enabled === true;
        let revision = revisionOf(preference.result);
        const conversations = enabled ? boundedConversations(data.result) : [];
        // Enforce retention and byte limits on the next authenticated browser
        // visit. A read without cleanup must not invalidate another reader.
        const stored = data.result.map(({ owner: ignored, ...item }) => item);
        const ordered = (items) => [...items].sort((a, b) => String(a.id).localeCompare(String(b.id)));
        if (JSON.stringify(ordered(stored)) !== JSON.stringify(ordered(conversations))) {
          for (const key of keys.result) rows.delete(key);
          for (const item of conversations) rows.put({ owner, ...item });
          revision += 1;
        }
        // Existing v1 records have implicit revision 0; retain their opt-in
        // and conversations, and persist the revision on their first read.
        if (revision !== preference.result?.revision) prefs.put({ owner, enabled, revision });
        setResult({ enabled, conversations, revision });
      };
      data.onsuccess = keys.onsuccess = preference.onsuccess = guarded(ready);
    });
  }

  async _write(owner, value, expectedRevision, enabled) {
    this._owner(owner);
    const conversations = enabled === false ? [] : boundedConversations(value);
    return this._transaction((tx, setResult, guarded) => {
      const rows = tx.objectStore("conversations"), prefs = tx.objectStore("preferences");
      const preference = prefs.get(owner);
      preference.onsuccess = guarded(() => {
        const revision = revisionOf(preference.result);
        // This check and the replacement share one readwrite transaction.
        // Automatic saves cannot create or re-enable a persistence preference.
        if (revision !== expectedRevision || (enabled === undefined && preference.result?.enabled !== true)) throw new HistoryConflictError();
        const nextEnabled = enabled ?? preference.result.enabled;
        const keys = rows.index("owner").getAllKeys(owner);
        keys.onsuccess = guarded(() => {
          for (const key of keys.result) rows.delete(key);
          for (const item of conversations) rows.put({ owner, ...item });
          prefs.put({ owner, enabled: nextEnabled, revision: revision + 1 });
          setResult({ enabled: nextEnabled, conversations, revision: revision + 1 });
        });
      });
    });
  }

  async save(owner, value, expectedRevision) {
    return this._write(owner, value, expectedRevision);
  }

  async setPersistence(owner, value, enabled, expectedRevision) {
    if (typeof enabled !== "boolean") throw new HistoryStorageError();
    return this._write(owner, value, expectedRevision, enabled);
  }

  async clear(owner, expectedRevision) {
    // Keep the disabled revision as a tombstone: deleting it would let a
    // stale revision-0 page revive old history after a clear.
    return this.setPersistence(owner, [], false, expectedRevision);
  }
}
