#!/usr/bin/env node
/** Build a separate static-only maintenance package. Never deploy or overwrite. */
import { constants } from "node:fs";
import { createHash } from "node:crypto";
import { lstat, mkdir, open, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const assets = path.join(root, "src", "project_copilot", "web", "assets");
const args = process.argv.slice(2);
const usage = "Usage: node scripts/build_maintenance.mjs --output-dir NEW_DIRECTORY";
if (args.length === 1 && args[0] === "--help") {
  process.stdout.write(`${usage}\nBuild only; no deployment, API or Cloud Function files.\n`);
  process.exit(0);
}
if (args.length !== 2 || args[0] !== "--output-dir" || !args[1]) {
  process.stderr.write(`${usage}\nAn explicit new output directory is required.\n`);
  process.exit(1);
}
const output = path.resolve(args[1]);
const permittedLocal = path.join(root, "data", "maintenance-builds") + path.sep;

async function rejectSymlinks(filename) {
  const absolute = path.resolve(filename);
  const parsed = path.parse(absolute);
  let current = parsed.root;
  const parts = absolute.slice(parsed.root.length).split(path.sep).filter(Boolean);
  for (let i = 0; i < parts.length; i += 1) {
    current = path.join(current, parts[i]);
    const info = await lstat(current);
    if (info.isSymbolicLink()) throw new Error("Symbolic links are not allowed.");
    if (i < parts.length - 1 && !info.isDirectory()) throw new Error("A parent is not a directory.");
  }
}

async function safeAsset(name) {
  const filename = path.join(assets, name);
  await rejectSymlinks(filename);
  const handle = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    if (!(await handle.stat()).isFile()) throw new Error("Maintenance asset must be a regular file.");
    const data = await handle.readFile();
    // Only the existing decorative SVGs are copied. Reject executable or
    // network-linked content if one of those source assets is later changed.
    const svg = data.toString("utf8");
    if (!/^\s*<svg\b/i.test(svg) || /<(?:script|style|foreignObject|iframe|animate|set)\b|@import|\bon\w+\s*=|\b(?:href|src)\s*=|url\(\s*["']?(?!#)/i.test(svg)) {
      throw new Error("Maintenance SVG must contain only local, passive graphics.");
    }
    return data;
  } finally { await handle.close(); }
}

const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="robots" content="noindex,nofollow">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'none'; connect-src 'none'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'">
  <meta name="referrer" content="no-referrer">
  <title>暂停服务 · Project Copilot</title>
  <link rel="icon" href="./assets/mark.svg" type="image/svg+xml">
  <link rel="stylesheet" href="./assets/maintenance.css">
</head>
<body>
  <header><img src="./assets/mark.svg" width="40" height="40" alt=""><div><strong>Project Copilot</strong><span>项目 · 知识 · 协作</span></div></header>
  <main>
    <p class="eyebrow">服务状态 · 暂停</p>
    <h1>暂歇片刻，<br>静候重逢。</h1>
    <p class="description">Project Copilot 暂停服务。<br>当前页面不提供登录、问答或工单操作，请稍后再来。</p>
    <div class="status"><span aria-hidden="true"></span>服务暂时暂停</div>
  </main>
  <footer>Project Copilot · 智能协作空间</footer>
</body>
</html>
`;

const css = `@charset "UTF-8";
:root{font-family:Inter,"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:#263e32;background:#f5f4ed;font-synthesis:none}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;flex-direction:column;background:linear-gradient(110deg,rgba(249,248,242,.35),rgba(249,248,242,.9)),url("./ink-mountains.svg") right bottom/960px auto no-repeat}
body:before{content:"";position:fixed;left:0;bottom:0;width:min(20vw,190px);height:65vh;background:url("./bamboo.svg") left bottom/contain no-repeat;opacity:.45;pointer-events:none;z-index:-1}
header{display:flex;align-items:center;gap:12px;margin:34px 7vw}header strong{font-family:Georgia,"Noto Serif SC",serif;font-size:23px;line-height:1.3}header span{display:block;font-size:12px;letter-spacing:.16em;color:#6c7869;margin-top:3px}
main{margin:auto;width:min(760px,84vw);padding:48px 0 90px}.eyebrow{color:#62755e;font-size:13px;letter-spacing:.18em;margin-bottom:26px}h1{font-family:Georgia,"Noto Serif SC","Songti SC",serif;font-size:clamp(38px,6vw,66px);font-weight:600;line-height:1.5;letter-spacing:.08em;margin:0 0 25px}.description{color:#637266;font-size:16px;line-height:2}.status{display:inline-flex;align-items:center;gap:9px;padding:9px 16px;border:1px solid #d7ddcf;border-radius:22px;font-size:13px;color:#576d50;margin-top:22px;background:#fafaf4}.status span{height:7px;width:7px;border-radius:50%;background:#839576}footer{font-size:12px;color:#7b8779;letter-spacing:.08em;margin:0 7vw 26px}
@media(max-width:600px){header{margin-top:25px}main{padding:35px 0 65px}.description{font-size:14px}body{background-size:auto,700px auto}}
`;

async function build() {
  // In-repository output is restricted to a private, ignored preparation area;
  // source trees and the production dist can never become deployment packages.
  if (output === root || (output.startsWith(root + path.sep) && !output.startsWith(permittedLocal))) {
    throw new Error("Output must be outside the repository or in data/maintenance-builds/NEW_DIRECTORY.");
  }
  await rejectSymlinks(path.dirname(output));
  try { await lstat(output); throw new Error("Output already exists; no files were overwritten."); }
  catch (error) { if (error.code !== "ENOENT") throw error; }

  const csp = "default-src 'none'; script-src 'none'; connect-src 'none'; style-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";
  const config = { headers: [{ source: "/*", headers: [
    { key: "Content-Security-Policy", value: csp },
    { key: "Cache-Control", value: "no-store" },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "no-referrer" },
    { key: "X-Frame-Options", value: "DENY" },
  ] }] };
  const files = new Map([
    ["public/index.html", Buffer.from(html)],
    ["public/assets/maintenance.css", Buffer.from(css)],
    ["public/edgeone.json", Buffer.from(JSON.stringify(config, null, 2) + "\n")],
  ]);
  for (const name of ["mark.svg", "bamboo.svg", "ink-mountains.svg"]) files.set(`public/assets/${name}`, await safeAsset(name));
  const manifest = {
    kind: "static_maintenance_v1",
    verification: "LOCAL_ARTIFACT_ONLY",
    static_directory: "public",
    cloud_function_sources: "absent",
    remote_route_removal: "NEEDS_MANUAL_CHECK",
    files: Object.fromEntries([...files].map(([name, bytes]) => [name, createHash("sha256").update(bytes).digest("hex")])),
  };

  // No removal, no recursive copying, no environment or private config reads.
  // A collision (including a concurrent output creation) fails atomically.
  await mkdir(output, { mode: 0o700 });
  await mkdir(path.join(output, "public"));
  await mkdir(path.join(output, "public", "assets"));
  for (const [name, bytes] of files) await writeFile(path.join(output, name), bytes, { flag: "wx" });
  await writeFile(path.join(output, "artifact-manifest.json"), JSON.stringify(manifest, null, 2) + "\n", { flag: "wx" });
  process.stdout.write(JSON.stringify({ status: "ok", kind: manifest.kind, output, files: files.size + 1, remote_route_removal: manifest.remote_route_removal }) + "\n");
}

try { await build(); }
catch (error) {
  process.stderr.write(`Maintenance build failed: ${error.message}\n`);
  process.exitCode = 1;
}
