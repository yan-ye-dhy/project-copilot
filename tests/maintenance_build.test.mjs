import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, copyFile, cp, writeFile, rm, symlink, unlink } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const script = path.join(root, "scripts", "build_maintenance.mjs");
const scratch = await mkdtemp(path.join(os.tmpdir(), "pc-maintenance-test-"));
test.after(() => rm(scratch, { recursive: true, force: true }));
const run = (output, options = {}, scriptPath = script) => exec(process.execPath, [scriptPath, "--output-dir", output], { cwd: root, ...options });
const absent = p => assert.rejects(readFile(p), { code: "ENOENT" });
async function listFiles(dir, prefix = "") {
  const result = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const relative = path.posix.join(prefix, entry.name);
    if (entry.isDirectory()) result.push(...await listFiles(path.join(dir, entry.name), relative));
    else result.push(relative);
  }
  return result.sort();
}
async function fixture(name) {
  const dir = path.join(scratch, name);
  await mkdir(path.join(dir, "scripts"), { recursive: true });
  await mkdir(path.join(dir, "src/project_copilot/web"), { recursive: true });
  await copyFile(script, path.join(dir, "scripts/build_maintenance.mjs"));
  await cp(path.join(root, "src/project_copilot/web/assets"), path.join(dir, "src/project_copilot/web/assets"), { recursive: true });
  return dir;
}

test("maintenance requires an explicit output and never defaults to production dist", async () => {
  await assert.rejects(exec(process.execPath, [script], { cwd: scratch }), error => /explicit new output/.test(error.stderr));
  assert.match((await exec(process.execPath, [script, "--help"])).stdout, /Build only/);
  await assert.rejects(exec(process.execPath, [script, "--deploy"], { cwd: scratch }), error => /Usage/.test(error.stderr));
});

test("standalone artifact is static, passive, secret-free and has no Cloud Function routes", async () => {
  const out = path.join(scratch, "maintenance");
  const sentinels = { PROJECT_COPILOT_PROXY_SECRET: "NEVER_PUBLISH_PROXY_SENTINEL", PROJECT_COPILOT_BACKEND_ORIGIN: "https://never-publish-backend.invalid", PROJECT_COPILOT_DEEPSEEK_API_KEY: "NEVER_PUBLISH_MODEL_SENTINEL" };
  const result = await run(out, { env: { ...process.env, ...sentinels } });
  assert.equal(JSON.parse(result.stdout).remote_route_removal, "NEEDS_MANUAL_CHECK");
  const files = await listFiles(out);
  assert.deepEqual(files, ["artifact-manifest.json", "public/assets/bamboo.svg", "public/assets/ink-mountains.svg", "public/assets/maintenance.css", "public/assets/mark.svg", "public/edgeone.json", "public/index.html"]);
  const config = JSON.parse(await readFile(path.join(out, "public/edgeone.json"), "utf8"));
  assert.deepEqual(Object.keys(config), ["headers"]);
  assert.match(config.headers[0].headers.find(h => h.key === "Content-Security-Policy").value, /connect-src 'none'/);
  const html = await readFile(path.join(out, "public/index.html"), "utf8");
  assert.match(html, /Project Copilot 暂停服务/);
  assert.doesNotMatch(html, /<(?:script|form|input|iframe)\b|\bfetch\s*\(|\/api\//i);
  for (const name of files) {
    const data = await readFile(path.join(out, name), "utf8");
    for (const secret of Object.values(sentinels)) assert.equal(data.includes(secret), false);
    assert.doesNotMatch(data, /trycloudflare|cloudFunctions|\/api\/|eo_token|eo_time|\.env|https?:\/\/(?!www\.w3\.org)/i);
    assert.equal(name.endsWith(".js") || name.endsWith(".mjs"), false);
  }
  const manifest = JSON.parse(await readFile(path.join(out, "artifact-manifest.json"), "utf8"));
  assert.equal(manifest.verification, "LOCAL_ARTIFACT_ONLY");
  for (const [name, digest] of Object.entries(manifest.files)) {
    assert.equal(createHash("sha256").update(await readFile(path.join(out, name))).digest("hex"), digest);
  }
});

test("existing output including an empty directory, file, or link is refused without overwrite", async () => {
  const existing = path.join(scratch, "existing"); await mkdir(existing);
  await assert.rejects(run(existing), error => /already exists/.test(error.stderr));
  await writeFile(path.join(existing, "keep"), "keep");
  await assert.rejects(run(existing), error => /already exists/.test(error.stderr));
  assert.equal(await readFile(path.join(existing, "keep"), "utf8"), "keep");
  const file = path.join(scratch, "file"); await writeFile(file, "user content");
  await assert.rejects(run(file), error => /already exists/.test(error.stderr));
  assert.equal(await readFile(file, "utf8"), "user content");
  const link = path.join(scratch, "dangling"); await symlink(path.join(scratch, "missing"), link);
  await assert.rejects(run(link), error => /already exists/.test(error.stderr));
});

test("source paths, repository root and normal production dist are forbidden outputs", async () => {
  for (const target of [root, path.join(root, "dist"), path.join(root, "src/new-maintenance"), path.join(root, "cloud-functions/new-maintenance"), path.join(root, ".git/new-maintenance")]) {
    await assert.rejects(run(target), error => /outside the repository/.test(error.stderr));
  }
});

test("a symlink output parent cannot redirect writes", async () => {
  const actual = path.join(scratch, "actual"); await mkdir(actual);
  const link = path.join(scratch, "parent-link"); await symlink(actual, link);
  await assert.rejects(run(path.join(link, "output")), error => /Symbolic links/.test(error.stderr));
  assert.deepEqual(await readdir(actual), []);
});

test("unsafe or symlink decorative assets fail before creating output", async () => {
  const dir = await fixture("unsafe-source");
  const asset = path.join(dir, "src/project_copilot/web/assets/mark.svg");
  const output = path.join(scratch, "unsafe-out");
  await writeFile(asset, '<svg xmlns="http://www.w3.org/2000/svg"><script>fetch("/api/jobs")</script></svg>');
  await assert.rejects(run(output, {}, path.join(dir, "scripts/build_maintenance.mjs")), error => /passive graphics/.test(error.stderr));
  await absent(output);
  await unlink(asset); await symlink(path.join(root, "src/project_copilot/web/assets/mark.svg"), asset);
  await assert.rejects(run(output, {}, path.join(dir, "scripts/build_maintenance.mjs")), error => /Symbolic links/.test(error.stderr));
  await absent(output);
});
