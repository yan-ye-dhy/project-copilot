#!/usr/bin/env node
/** Build an isolated same-origin static frontend; never replace an existing output. */
import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = path.join(projectRoot, "src", "project_copilot", "web");
const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== "--output-dir" || !args[1])) {
  throw new Error("Usage: node scripts/build_edgeone.mjs [--output-dir NEW_DIRECTORY]");
}
const output = path.resolve(args[1] || path.join(projectRoot, "dist"));
// Only known frontend files may enter the public output. New assets must be
// deliberately added here rather than silently copying private files.
const requiredAssets = ["api.js", "app.js", "styles.css", "mark.svg", "ink-mountains.svg", "bamboo.svg"];
const localMarker = 'const API_PREFIX = ""; // EDGEONE_API_PREFIX';
const deployedMarker = 'const API_PREFIX = "/api"; // EDGEONE_API_PREFIX';

async function rejectSymlinkComponents(target, allowMissingLeaf = false) {
  const absolute = path.resolve(target);
  const parsed = path.parse(absolute);
  const parts = absolute.slice(parsed.root.length).split(path.sep).filter(Boolean);
  let current = parsed.root;
  for (let i = 0; i < parts.length; i += 1) {
    current = path.join(current, parts[i]);
    let info;
    try { info = await lstat(current); }
    catch (error) {
      if (error.code === "ENOENT" && allowMissingLeaf && i === parts.length - 1) return;
      throw error;
    }
    if (info.isSymbolicLink()) throw new Error(`Symbolic links are not allowed: ${current}`);
    if (i < parts.length - 1 && !info.isDirectory()) throw new Error(`Parent is not a directory: ${current}`);
  }
}

async function readRegularFile(filename) {
  await rejectSymlinkComponents(filename);
  // O_NOFOLLOW protects the final path component against a symlink substitution.
  const handle = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    if (!(await handle.stat()).isFile()) throw new Error(`Not a regular file: ${filename}`);
    return await handle.readFile();
  } finally { await handle.close(); }
}

async function build() {
  if (output === sourceRoot || output.startsWith(sourceRoot + path.sep)) {
    throw new Error("Output must be outside the frontend source directory.");
  }
  await rejectSymlinkComponents(sourceRoot);
  await rejectSymlinkComponents(path.dirname(output));
  // lstat catches existing files, directories and dangling links without following them.
  try { await lstat(output); throw new Error(`Output already exists: ${output}`); }
  catch (error) { if (error.code !== "ENOENT") throw error; }

  const sourceAssets = path.join(sourceRoot, "assets");
  await rejectSymlinkComponents(sourceAssets);
  const entries = await readdir(sourceAssets, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.isSymbolicLink()) throw new Error(`Symbolic links are not allowed: ${entry.name}`);
    if (!entry.isFile()) throw new Error(`Only regular flat assets are supported: ${entry.name}`);
  }
  const names = entries.map((entry) => entry.name);
  for (const name of requiredAssets) if (!names.includes(name)) throw new Error(`Missing asset: ${name}`);
  const files = new Map();
  files.set("index.html", await readRegularFile(path.join(sourceRoot, "index.html")));
  for (const name of requiredAssets) files.set(`ui-assets/${name}`, await readRegularFile(path.join(sourceAssets, name)));
  const apiSource = files.get("ui-assets/api.js").toString("utf8");
  if (apiSource.split(localMarker).length !== 2) throw new Error("Static API prefix marker must occur exactly once.");
  files.set("ui-assets/api.js", Buffer.from(apiSource.replace(localMarker, deployedMarker), "utf8"));

  // No recursive removal: mkdir fails atomically if a user output appears concurrently.
  await mkdir(output);
  await mkdir(path.join(output, "ui-assets"));
  for (const [relative, data] of files) {
    await writeFile(path.join(output, relative), data, { flag: "wx" });
  }
  process.stdout.write(`Built ${files.size} static files in ${output}\n`);
}

try { await build(); }
catch (error) {
  process.stderr.write(`Static build failed: ${error.message}\n`);
  process.exitCode = 1;
}
