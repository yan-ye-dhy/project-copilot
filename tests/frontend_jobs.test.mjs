import test from "node:test";
import assert from "node:assert/strict";
import { runPublicJob } from "../src/project_copilot/web/assets/api.js";

test("expired retained result never submits a replacement request", async () => {
  let posts = 0;
  const request = async (path, options = {}) => {
    if (options.method === "POST") {
      posts += 1;
      return { data: { job_id: "00000000-0000-0000-0000-000000000001", status: "succeeded" } };
    }
    return { data: { status: "succeeded", result: null, result_expired: true } };
  };
  await assert.rejects(runPublicJob("rag", { question: "q" }, { request, createId: () => "fixed-key", delay: async () => {} }), /保留期限/);
  assert.equal(posts, 1);
});

const id = "00000000-0000-4000-8000-000000000001";
const opts = { createId: () => "a-valid-idempotency-key", delay: async () => {}, maxPolls: 3 };

test("job posts once, polls without repeating, returns the actual result", async () => {
  const calls = [];
  const request = async (path, options = {}) => {
    calls.push([path, options]);
    if (path === "/jobs") return { data: { job_id: id }, meta: {} };
    return { data: calls.length < 3 ? { status: "running" } : { status: "succeeded", result: { answer: "ok" } }, meta: {} };
  };
  const out = await runPublicJob("rag", { question: "q" }, { ...opts, request });
  assert.equal(out.data.answer, "ok");
  assert.equal(calls.filter(([path]) => path === "/jobs").length, 1);
  assert.equal(calls[0][1].headers["Idempotency-Key"], "a-valid-idempotency-key");
});

test("unknown state is reported without new submission", async () => {
  let posts = 0;
  const request = async (path) => path === "/jobs" ? (posts++, { data: { job_id: id } }) : { data: { status: "unknown" } };
  await assert.rejects(runPublicJob("agent", {}, { ...opts, request }), /待核实/);
  assert.equal(posts, 1);
});

test("switching user aborts polling before old result can be shown", async () => {
  let polls = 0;
  const request = async (path) => path === "/jobs" ? { data: { job_id: id } } : (polls++, { data: { status: "succeeded", result: {} } });
  await assert.rejects(runPublicJob("rag", {}, { ...opts, request, isCurrent: () => false }), /会话已切换/);
  assert.equal(polls, 0);
});
