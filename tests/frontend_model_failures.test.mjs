import test from 'node:test';
import assert from 'node:assert/strict';
import {
  apiRequest, jobFailureLabel, runPublicJob, safeFailureType,
} from '../src/project_copilot/web/assets/api.js';

const ID = '11111111-1111-4111-8111-111111111111';
const PRIVATE = 'private-provider-body-and-credential';
const labels = {
  model_auth_error: '模型服务认证失败', model_rate_limit: '模型服务侧限流',
  model_provider_error: '模型服务内部异常', model_request_rejected: '模型服务拒绝了请求',
  model_connection_error: '无法连接模型服务', model_response_invalid: '模型服务响应未通过校验',
  model_output_limit: '模型输出达到长度上限', model_input_invalid: '模型请求输入未通过校验',
  model_tool_protocol: '模型工具调用格式未通过校验',
  model_error: '模型服务调用未能完成', model_timeout: '模型调用超时',
};

for (const [failure, label] of Object.entries(labels)) {
  for (const state of ['failed', 'unknown']) {
    test(`${failure} preserves ${state}, fixed label and one submission without error-body leakage`, async () => {
      const calls = [];
      assert.equal(safeFailureType(failure), failure);
      assert.equal(jobFailureLabel(failure), label);
      await assert.rejects(runPublicJob('agent', { question: 'synthetic question' }, {
        createId: () => 'synthetic-idempotency-key',
        request: async (path, options = {}) => {
          calls.push({ path, method: options.method || 'GET' });
          if (options.method === 'POST') return { data: { job_id: ID } };
          return { data: { status: state, failure_type: failure, message: PRIVATE,
            error: { body: PRIVATE }, result: { answer: PRIVATE } } };
        },
      }), (error) => error.kind === 'job' && error.meta.jobId === ID && error.meta.jobStatus === state &&
        error.meta.failureType === failure && error.message.includes(label) &&
        error.message.includes('请勿重复提交') && !error.message.includes(PRIVATE) &&
        !JSON.stringify(error.meta).includes(PRIVATE) &&
        (state !== 'unknown' || error.message.includes('结果待核实')));
      assert.deepEqual(calls, [{ path: '/jobs', method: 'POST' }, { path: `/jobs/${ID}`, method: 'GET' }]);
    });
  }
}

test('model-service rate limiting remains distinct from local task quota rejection', async () => {
  const modelLabel = jobFailureLabel('model_rate_limit');
  assert.match(modelLabel, /模型服务侧限流/);
  for (const [reason, label] of [
    ['personal_demo_limit', '本次个人授权'], ['user_daily', '你的 UTC 每日任务额度'],
    ['global_daily', '全站 UTC 每日任务额度'], ['provider_attempts', '本任务的供应商调用次数'],
  ]) {
    let attempts = 0;
    await assert.rejects(apiRequest('/jobs', { method: 'POST', body: {}, fetchImpl: async () => {
      attempts += 1;
      return new Response(JSON.stringify({ reason, detail: PRIVATE }), {
        status: 429, headers: { 'Content-Type': 'application/json' },
      });
    } }), (error) => error.meta.quotaReason === reason && error.message.includes(label) &&
      !error.message.includes(modelLabel) && !error.message.includes(PRIVATE));
    assert.equal(attempts, 1);
  }
});

test('unapproved diagnostics cannot become labels or reflected error metadata', () => {
  for (const value of [PRIVATE, `model_auth_error: ${PRIVATE}`, null, {}, '__proto__']) {
    assert.equal(safeFailureType(value), undefined);
    assert.equal(jobFailureLabel(value), '处理未能完成');
  }
});
