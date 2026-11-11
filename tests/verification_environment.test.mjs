import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const broker = 'amqp://messaging_lab:synthetic-lab-only@127.0.0.1:15672/messaging_lab';
const database = 'postgresql+psycopg://project_copilot_test:local-test-only@127.0.0.1:5434/project_copilot_test';

async function verify(mode, overrides = {}) {
  const temporary = await mkdtemp(path.join(os.tmpdir(), 'copilot-verify-env-'));
  try {
    const executable = path.join(temporary, 'inspect-python.mjs');
    await writeFile(executable, `#!/usr/bin/env node
const names = ['PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED', 'PROJECT_COPILOT_RABBITMQ_URL',
  'PROJECT_COPILOT_JOB_RECOVERY_ENABLED', 'PROJECT_COPILOT_JOB_RECOVERY_ALLOW_MODEL_REPEAT',
  'PROJECT_COPILOT_RABBITMQ_HEARTBEAT_SECONDS', 'PROJECT_COPILOT_TEST_RABBITMQ_URL',
  'PROJECT_COPILOT_TEST_DATABASE_URL', 'PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION'];
console.log('VERIFY_ENV:' + JSON.stringify(Object.fromEntries(names.map(name => [name, process.env[name]]))));
`);
    await chmod(executable, 0o700);
    return spawnSync(process.execPath, ['scripts/verify.mjs', mode], {
      cwd: root, encoding: 'utf8', timeout: 10000,
      env: { ...process.env, PC_TEST_PYTHON: executable,
        PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED: 'true',
        PROJECT_COPILOT_JOB_RECOVERY_ENABLED: 'true',
        PROJECT_COPILOT_JOB_RECOVERY_ALLOW_MODEL_REPEAT: 'true',
        PROJECT_COPILOT_RABBITMQ_URL: 'amqp://synthetic:private-canary@live.invalid:5672/live',
        PROJECT_COPILOT_RABBITMQ_HEARTBEAT_SECONDS: '999',
        PROJECT_COPILOT_TEST_RABBITMQ_URL: broker,
        PROJECT_COPILOT_TEST_DATABASE_URL: database,
        PROJECT_COPILOT_TEST_REDIS_URL: 'redis://127.0.0.1:16379/15',
        PROJECT_COPILOT_QDRANT_URL: 'http://127.0.0.1:16433', ...overrides },
    });
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
}

function captured(result) {
  assert.equal(result.status, 0, result.stderr);
  const line = result.stdout.split('\n').find(value => value.startsWith('VERIFY_ENV:'));
  assert.ok(line, 'verification must launch the controlled Python executable');
  return JSON.parse(line.slice('VERIFY_ENV:'.length));
}

test('offline verification removes inherited broker authorization and native-test opt-in', async () => {
  const environment = captured(await verify('--python'));
  assert.equal(environment.PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED, 'false');
  assert.equal(environment.PROJECT_COPILOT_JOB_RECOVERY_ENABLED, 'false');
  assert.equal(environment.PROJECT_COPILOT_JOB_RECOVERY_ALLOW_MODEL_REPEAT, undefined);
  assert.equal(environment.PROJECT_COPILOT_RABBITMQ_URL, undefined);
  assert.equal(environment.PROJECT_COPILOT_RABBITMQ_HEARTBEAT_SECONDS, undefined);
  assert.equal(environment.PROJECT_COPILOT_TEST_RABBITMQ_URL, undefined);
  assert.equal(environment.PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION, '0');
});

test('service verification opts into only the owned PostgreSQL and synthetic broker', async () => {
  const environment = captured(await verify('--services'));
  assert.equal(environment.PROJECT_COPILOT_MESSAGE_QUEUE_ENABLED, 'false');
  assert.equal(environment.PROJECT_COPILOT_JOB_RECOVERY_ENABLED, 'false');
  assert.equal(environment.PROJECT_COPILOT_RABBITMQ_URL, undefined);
  assert.equal(environment.PROJECT_COPILOT_TEST_RABBITMQ_URL, broker);
  assert.equal(environment.PROJECT_COPILOT_TEST_DATABASE_URL, database);
  assert.equal(environment.PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION, '1');
});

test('service verification rejects another broker or database before launching tests', async () => {
  for (const overrides of [
    { PROJECT_COPILOT_TEST_RABBITMQ_URL: 'amqp://synthetic:private-canary@live.invalid:5672/live' },
    { PROJECT_COPILOT_TEST_DATABASE_URL: 'postgresql+psycopg://synthetic:private-canary@live.invalid:5432/live' },
  ]) {
    const result = await verify('--services', overrides);
    assert.equal(result.status, 2);
    assert.equal(result.stdout.includes('VERIFY_ENV:'), false);
    assert.equal(result.stderr.includes('private-canary'), false);
  }
});
