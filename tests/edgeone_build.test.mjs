import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, mkdir, readdir, readFile, writeFile, copyFile, cp, rename, rm, symlink, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const exec = promisify(execFile);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const script = path.join(root, "scripts", "build_edgeone.mjs");
const source = path.join(root, "src", "project_copilot", "web");
const scratch = await mkdtemp(path.join(os.tmpdir(), "project-copilot-build-test-"));
test.after(async () => { await rm(scratch, { recursive: true, force: true }); });
const run = (scriptPath, output, options = {}) => exec(process.execPath,
  [scriptPath, "--output-dir", output], { cwd: root, ...options });
const absent = async (p) => assert.rejects(readFile(p), { code: "ENOENT" });
async function fixture(name) {
  const dir = path.join(scratch, name);
  await mkdir(path.join(dir, "scripts"), { recursive: true });
  await mkdir(path.join(dir, "src", "project_copilot"), { recursive: true });
  await copyFile(script, path.join(dir, "scripts", "build_edgeone.mjs"));
  await cp(source, path.join(dir, "src", "project_copilot", "web"), { recursive: true });
  return dir;
}

test("static build ships complete assets and sends same-origin API requests through /api", async () => {
  const output = path.join(scratch, "site");
  const sentinels = { PROJECT_COPILOT_PROXY_SECRET: "secret-build-sentinel-2938", PROJECT_COPILOT_BACKEND_ORIGIN: "https://private-origin.invalid" };
  await run(script, output, { env: { ...process.env, ...sentinels } });
  assert.deepEqual((await readdir(output)).sort(), ["index.html", "ui-assets"]);
  const assets = (await readdir(path.join(output, "ui-assets"))).sort();
  assert.deepEqual(assets, ["api.js", "app.js", "avatar-swordsman.webp", "bamboo.svg", "ink-mountains.svg", "mark.svg", "markdown.js", "styles.css", "task-center.js"]);
  const index = await readFile(path.join(output, "index.html"), "utf8");
  assert.match(index, /\/ui-assets\/styles\.css/);
  assert.match(index, /\/ui-assets\/app\.js/);
  assert.match(index, /\/ui-assets\/avatar-swordsman\.webp/);
  const code = await readFile(path.join(output, "ui-assets", "api.js"), "utf8");
  assert.match(code, /const API_PREFIX = "\/api";/);
  assert.match(await readFile(path.join(output, "ui-assets", "app.js"), "utf8"), /from "\.\/api\.js"/);
  assert.match(await readFile(path.join(output, "ui-assets", "app.js"), "utf8"), /from "\.\/task-center\.js"/);
  for (const name of ["index.html", ...assets.map((asset) => `ui-assets/${asset}`)]) {
    const contents = await readFile(path.join(output, name));
    for (const secret of Object.values(sentinels)) assert.equal(contents.includes(Buffer.from(secret)), false);
  }
  const module = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
  module.setCsrfToken("a".repeat(64));
  const calls = [];
  const result = await module.apiRequest("/agent/ask?preview=1", {
    method: "POST", body: { question: "你好" },
    fetchImpl: async (url, options) => { calls.push({ url, options }); return new Response('{"status":"ok"}', { status: 200 }); },
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/agent/ask?preview=1");
  assert.equal(calls[0].options.credentials, "same-origin");
  assert.equal(result.data.status, "ok");
  await assert.rejects(module.apiRequest("/../tickets", { fetchImpl: () => assert.fail("must not fetch") }), /当前服务/);
  await assert.rejects(module.apiRequest("/\\example.invalid", { fetchImpl: () => assert.fail("must not fetch") }), /当前服务/);
});

test("local swordsman portrait is an optimized WebP and copied byte-for-byte", async () => {
  const output = path.join(scratch, "portrait-site");
  await run(script, output);
  const portrait = await readFile(path.join(source, "assets", "avatar-swordsman.webp"));
  assert.equal(portrait.subarray(0, 4).toString("ascii"), "RIFF");
  assert.equal(portrait.subarray(8, 12).toString("ascii"), "WEBP");
  assert.ok(portrait.length > 1024 && portrait.length < 100 * 1024, "portrait remains under 100 KiB");
  assert.deepEqual(await readFile(path.join(output, "ui-assets", "avatar-swordsman.webp")), portrait);
  assert.equal(portrait.includes(Buffer.from("Exif")), false, "generated asset has no EXIF payload");
});

test("missing portrait fails the allowlisted build before producing a partial site", async () => {
  const dir = await fixture("missing-portrait-project");
  await unlink(path.join(dir, "src/project_copilot/web/assets/avatar-swordsman.webp"));
  const output = path.join(scratch, "missing-portrait-output");
  await assert.rejects(run(path.join(dir, "scripts/build_edgeone.mjs"), output),
    (error) => /Missing asset: avatar-swordsman\.webp/.test(error.stderr));
  await absent(output);
});

test("existing output directory is refused without touching its contents", async () => {
  const output = path.join(scratch, "existing"); await mkdir(output);
  await writeFile(path.join(output, "user.txt"), "keep this file");
  await assert.rejects(run(script, output), (error) => /Output already exists/.test(error.stderr));
  assert.equal(await readFile(path.join(output, "user.txt"), "utf8"), "keep this file");
});

test("existing regular output file is never overwritten", async () => {
  const output = path.join(scratch, "existing-file"); await writeFile(output, "user data");
  await assert.rejects(run(script, output), (error) => /Output already exists/.test(error.stderr));
  assert.equal(await readFile(output, "utf8"), "user data");
});

test("output symlink, including a dangling link, is refused", async () => {
  const target = path.join(scratch, "target"); await mkdir(target);
  const link = path.join(scratch, "output-link"); await symlink(target, link);
  await assert.rejects(run(script, link), (error) => /Output already exists/.test(error.stderr));
  const dangling = path.join(scratch, "dangling-link"); await symlink(path.join(scratch, "missing-target"), dangling);
  await assert.rejects(run(script, dangling), (error) => /Output already exists/.test(error.stderr));
  assert.deepEqual(await readdir(target), []);
});

test("symlink in output parent is refused", async () => {
  const target = path.join(scratch, "output-parent"); await mkdir(target);
  const link = path.join(scratch, "parent-link"); await symlink(target, link);
  await assert.rejects(run(script, path.join(link, "site")), (error) => /Symbolic links are not allowed/.test(error.stderr));
  assert.deepEqual(await readdir(target), []);
});

test("source asset symlink is rejected before creating output", async () => {
  const dir = await fixture("asset-link-project");
  const asset = path.join(dir, "src/project_copilot/web/assets/mark.svg");
  await unlink(asset); await symlink(path.join(scratch, "existing-file"), asset);
  const out = path.join(scratch, "asset-link-output");
  await assert.rejects(run(path.join(dir, "scripts/build_edgeone.mjs"), out), (error) => /Symbolic links are not allowed/.test(error.stderr));
  await absent(out);
});

test("source assets directory symlink is rejected", async () => {
  const dir = await fixture("assets-directory-project");
  const assetDir = path.join(dir, "src/project_copilot/web/assets");
  await rename(assetDir, `${assetDir}-real`); await symlink(`${assetDir}-real`, assetDir);
  const out = path.join(scratch, "assets-directory-output");
  await assert.rejects(run(path.join(dir, "scripts/build_edgeone.mjs"), out), (error) => /Symbolic links are not allowed/.test(error.stderr));
  await absent(out);
});

test("missing required asset or ambiguous build marker fails before output creation", async () => {
  const missing = await fixture("missing-project");
  await unlink(path.join(missing, "src/project_copilot/web/assets/bamboo.svg"));
  const out1 = path.join(scratch, "missing-output");
  await assert.rejects(run(path.join(missing, "scripts/build_edgeone.mjs"), out1), (error) => /Missing asset/.test(error.stderr));
  await absent(out1);
  const bad = await fixture("marker-project");
  const api = path.join(bad, "src/project_copilot/web/assets/api.js");
  await writeFile(api, (await readFile(api, "utf8")) + '\nconst API_PREFIX = ""; // EDGEONE_API_PREFIX\n');
  const out2 = path.join(scratch, "marker-output");
  await assert.rejects(run(path.join(bad, "scripts/build_edgeone.mjs"), out2), (error) => /exactly once/.test(error.stderr));
  await absent(out2);
});

test("only allowlisted assets are copied, including when other files look like frontend assets", async () => {
  const dir = await fixture("private-assets-project");
  const assets = path.join(dir, "src/project_copilot/web/assets");
  await writeFile(path.join(assets, ".env"), "SECRET_SENTINEL_1109");
  await writeFile(path.join(assets, "private.txt"), "PRIVATE_SENTINEL_1109");
  await writeFile(path.join(assets, "private.js"), "PRIVATE_JS_SENTINEL_1109");
  await writeFile(path.join(assets, "private.svg"), "PRIVATE_SVG_SENTINEL_1109");
  const out = path.join(scratch, "private-output");
  await run(path.join(dir, "scripts/build_edgeone.mjs"), out);
  assert.equal((await readdir(path.join(out, "ui-assets"))).includes(".env"), false);
  assert.equal((await readdir(path.join(out, "ui-assets"))).includes("private.txt"), false);
  assert.equal((await readdir(path.join(out, "ui-assets"))).includes("private.js"), false);
  assert.equal((await readdir(path.join(out, "ui-assets"))).includes("private.svg"), false);
});

test("output cannot be placed inside source tree", async () => {
  const out = path.join(source, "do-not-create");
  await assert.rejects(run(script, out), (error) => /outside the frontend source/.test(error.stderr));
  await absent(out);
});
