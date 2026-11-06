import test from 'node:test';
import assert from 'node:assert/strict';
import {
  RECOVERY_LIMIT, recoveryRecords, jobObservation, jobStateLabel, canHideJob, setRecoveryHidden,
} from '../src/project_copilot/web/assets/task-center.js';

const now = 1793940000000;
const id = (n = 1) => `11111111-1111-4111-8111-${String(n).padStart(12, '0')}`;
const record = (n = 1, extra = {}) => ({ id: id(n), kind: n % 2 ? 'agent' : 'rag', createdAt: now - n * 1000, ...extra });

test('local recovery metadata strips all content, credentials, state and stream cursors', () => {
  const result = recoveryRecords([record(1, {
    question: 'must-not-store', answer: 'private answer', cookie: 'cookie-value', context_token: 'secret',
    csrf: 'secret', status: 'running', lastSequence: 400, hidden: true,
  })], now);
  assert.deepEqual(result, [{ id: id(), kind: 'agent', createdAt: now - 1000, lastSequence: 0, hidden: true }]);
  assert.doesNotMatch(JSON.stringify(result), /secret|cookie|private|question|answer|status/);
});

test('invalid, expired, future and duplicate records cannot grow the bounded local list', () => {
  const values = [null, {}, record(1, { id: 'invalid' }), record(2, { kind: 'upload' }),
    record(3, { createdAt: now - 8 * 86400000 }), record(4, { createdAt: now + 61000 }),
    record(5, { createdAt: 4.3 }), ...Array.from({ length: 50 }, (_, i) => record(i + 10)), record(10)];
  const result = recoveryRecords(values, now);
  assert.equal(result.length, RECOVERY_LIMIT);
  assert.equal(new Set(result.map((r) => r.id)).size, RECOVERY_LIMIT);
  assert.ok(result.every((r, i) => !i || r.createdAt >= result[i - 1].createdAt));
  assert.deepEqual(recoveryRecords({}, now), []);
});

test('elapsed submission time never manufactures running, failed, unknown or expired status', () => {
  const old = recoveryRecords([record(1, { createdAt: now - 6 * 86400000, status: 'unknown' })], now)[0];
  assert.equal(old.status, undefined);
  assert.equal(jobStateLabel(null), '尚未查询');
  for (const value of [{}, { status: 'timed_out' }, { status: '<script>alert(1)</script>' }, null]) {
    assert.equal(jobObservation(value), null);
  }
});

for (const [status, label] of Object.entries({ reserved: '等待执行', running: '正在执行', succeeded: '已完成', failed: '已失败', unknown: '结果待核实' })) {
  test(`a GET response with ${status} has an explicit safe label and no response content`, () => {
    const value = jobObservation({ status, result: { answer: 'private' }, context_token: 'secret' });
    assert.deepEqual(value, { status, expired: false });
    assert.equal(jobStateLabel(value), label);
    assert.ok(Object.isFrozen(value));
    assert.equal(canHideJob(value), ['failed', 'unknown'].includes(status));
  });
}

test('only server result_expired marks an expired result, without inferring a failed execution', () => {
  const observation = jobObservation({ status: 'succeeded', result_expired: true });
  assert.equal(jobStateLabel(observation), '结果保留期已过');
  assert.equal(canHideJob(observation), false);
});

test('hiding requires failed or unknown proof, only changes a local flag, and is reversible', () => {
  const original = [record()];
  for (const observation of [null, jobObservation({ status: 'running' }), jobObservation({ status: 'reserved' })]) {
    assert.equal(setRecoveryHidden(original, id(), true, observation, now)[0].hidden, undefined);
  }
  const hidden = setRecoveryHidden(original, id(), true, jobObservation({ status: 'unknown' }), now);
  assert.equal(hidden[0].hidden, true);
  assert.equal(original[0].hidden, undefined);
  assert.deepEqual(setRecoveryHidden(hidden, id(), false, null, now), recoveryRecords(original, now));
  assert.deepEqual(setRecoveryHidden(hidden, id(), true, jobObservation({ status: 'unknown' }), now), hidden);
});

test('a hidden row remains recoverable after reload without persisting terminal state', () => {
  const hidden = setRecoveryHidden([record()], id(), true, jobObservation({ status: 'failed' }), now);
  const reloaded = recoveryRecords(JSON.parse(JSON.stringify(hidden)), now);
  assert.equal(reloaded[0].hidden, true);
  assert.equal(reloaded[0].id, id());
  assert.equal(reloaded[0].status, undefined);
  assert.equal(reloaded[0].kind, 'agent');
});


test('a response for a different job cannot become the displayed task state', () => {
  assert.equal(jobObservation({ job_id: id(2), status: 'unknown' }, id()), null);
  assert.equal(jobObservation({ status: 'unknown' }, id()), null);
  assert.equal(jobObservation({ job_id: id(), status: 'running' }, id()).status, 'running');
});
