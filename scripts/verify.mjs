#!/usr/bin/env node
/** One local/CI entry point. No model credentials or production service startup. */
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
if (args.length > 1 || (args.length && !['--frontend', '--python', '--services'].includes(args[0]))) {
  console.error('Usage: npm run verify -- [--frontend|--python|--services]');
  process.exit(2);
}
const mode = args[0] || '--all';
if (mode === '--services' && process.env.PROJECT_COPILOT_QDRANT_URL &&
    process.env.PROJECT_COPILOT_QDRANT_URL !== 'http://127.0.0.1:16433') {
  console.error('Service checks require the dedicated test Qdrant at http://127.0.0.1:16433.');
  process.exit(2);
}
const env = { ...process.env, PYTHONPATH: root + path.delimiter + path.join(root, 'src'),
  PROJECT_COPILOT_QDRANT_URL: 'http://127.0.0.1:16433',
  HF_HUB_OFFLINE: '1', OMP_NUM_THREADS: '2', MKL_NUM_THREADS: '2',
  PROJECT_COPILOT_RUN_FINAL_GPU: '0', PROJECT_COPILOT_RUN_INGRESS_DOCKER: '0',
  PROJECT_COPILOT_RUN_POSTGRES_INTEGRATION: mode === '--services' ? '1' : '0',
  PROJECT_COPILOT_RUN_QDRANT_INTEGRATION: mode === '--services' ? '1' : '0',
  PROJECT_COPILOT_RUN_FINAL_QDRANT: mode === '--services' ? '1' : '0',
  PC_TEST_WEB_QDRANT: mode === '--services' ? '1' : '0' };

async function run(command, argv) {
  console.log(`\n> ${command} ${argv.join(' ')}`);
  await new Promise((resolve, reject) => {
    const child = spawn(command, argv, { cwd: root, env, stdio: 'inherit' });
    child.once('error', reject);
    child.once('exit', (code, signal) => code === 0 ? resolve() : reject(new Error(`Check failed (${signal || code})`)));
  });
}

try {
  await mkdir(path.join(root, 'test-results'), { recursive: true });
  const output = await mkdtemp(path.join(root, 'test-results', 'verify-'));
  if (mode === '--all' || mode === '--frontend') {
    await run(process.execPath, ['scripts/build_edgeone.mjs', '--output-dir', path.join(output, 'dist')]);
    const files = (await readdir(path.join(root, 'tests'))).filter(name => name.endsWith('.test.mjs')).sort();
    await run(process.execPath, ['--test', '--test-isolation=none', ...files.map(name => `tests/${name}`)]);
  }
  if (mode !== '--frontend') {
    const pytestArgs = ['-m', 'pytest', '-q', '-ra', '--junitxml', path.join(output, 'pytest.xml')];
    if (process.env.PC_TEST_PYTHON) await run(process.env.PC_TEST_PYTHON, pytestArgs);
    else await run('uv', ['run', '--frozen', '--no-default-groups', '--group', 'dev', 'python', ...pytestArgs]);
  }
  console.log(`\nCompleted ${mode}; artifacts: ${path.relative(root, output)}`);
  console.log('GPU, real providers and public-cloud E2E are NOT verified by this command. See docs/VERIFICATION.md.');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
