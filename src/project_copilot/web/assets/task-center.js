// Account-scoped recovery metadata only. Results, questions and credentials never belong here.
import { jobExecution, jobExecutionLabel } from "./api.js";
const ID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export const RECOVERY_LIMIT = 32;
const RETENTION_MS = 7 * 86400000;
const STATES = Object.freeze({ reserved: "等待执行", running: "正在执行", succeeded: "已完成", failed: "已失败", unknown: "结果待核实" });

export function recoveryRecords(value, now = Date.now()) {
  if (!Array.isArray(value)) return [];
  const unique = new Map();
  for (const item of value) {
    if (!item || !ID.test(item.id) || !["agent", "rag"].includes(item.kind) ||
        !Number.isSafeInteger(item.createdAt) || item.createdAt <= now - RETENTION_MS || item.createdAt > now + 60000) continue;
    unique.set(item.id, { id: item.id, kind: item.kind, createdAt: item.createdAt, lastSequence: 0,
      ...(item.hidden === true ? { hidden: true } : {}) });
  }
  return [...unique.values()].sort((a, b) => a.createdAt - b.createdAt).slice(-RECOVERY_LIMIT);
}

export function jobObservation(value, expectedId = null) {
  if (expectedId !== null && (!ID.test(expectedId) || value?.job_id !== expectedId)) return null;
  if (!value || !Object.hasOwn(STATES, value.status)) return null;
  const execution = jobExecution(value.execution);
  if (value.execution !== undefined && value.execution !== null && !execution) return null;
  return Object.freeze({ status: value.status, expired: value.result_expired === true,
    ...(execution ? { execution } : {}) });
}

export function jobStateLabel(observation) {
  if (!observation) return "尚未查询";
  if (observation.expired) return "结果保留期已过";
  if (observation.execution && observation.execution.recovery_status !== "completed") return jobExecutionLabel(observation.execution);
  return STATES[observation.status] || "尚未查询";
}

export function canHideJob(observation) {
  return ["failed", "unknown"].includes(observation?.status) && observation?.execution?.can_resume_waiting !== true;
}

export function setRecoveryHidden(records, id, hidden, observation, now = Date.now()) {
  // Hiding is allowed only after a real GET establishes a terminal failed/unknown state.
  // Restoring a previously hidden local row never needs another execution or state mutation.
  if (hidden && !canHideJob(observation)) return recoveryRecords(records, now);
  return recoveryRecords(records, now).map((record) => {
    if (record.id !== id) return record;
    const { hidden: ignored, ...metadata } = record;
    return hidden ? { ...metadata, hidden: true } : metadata;
  });
}
