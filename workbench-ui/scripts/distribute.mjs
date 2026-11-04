#!/usr/bin/env node
/**
 * 把 dist 的两份产物分发到两个宿主：
 *   dist/plugin/      → ~/.hermes/plugins/workbench/dashboard/  （manifest.entry / .css）
 *   dist/standalone/  → workbench-app/web/                      （独立版静态目录）
 *
 * 宿主目录不在 git 里（插件目录尤其），覆盖前先备份。
 */
import { cpSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, "..");
const repoRoot = resolve(uiRoot, "..");

const PLUGIN_DIR =
  process.env.WB_PLUGIN_DIR || join(homedir(), ".hermes", "plugins", "workbench", "dashboard");
const WEB_DIR = process.env.WB_WEB_DIR || join(repoRoot, "workbench-app", "web");

const pluginDist = join(uiRoot, "dist", "plugin");
const standaloneDist = join(uiRoot, "dist", "standalone");

function must(dir, what) {
  if (!existsSync(dir)) {
    console.error(`✗ 找不到 ${what}：${dir}（先跑 npm run build:plugin / build:standalone）`);
    process.exit(1);
  }
}

function backup(file) {
  if (!existsSync(file)) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const dest = `${file}.bak-${stamp}`;
  cpSync(file, dest);
  return dest;
}

must(pluginDist, "插件产物");
must(standaloneDist, "独立版产物");

// ---- plugin host -------------------------------------------------------
mkdirSync(PLUGIN_DIR, { recursive: true });
for (const f of ["index.js", "style.css"]) {
  const src = join(pluginDist, f);
  if (!existsSync(src)) continue;
  const dest = join(PLUGIN_DIR, f);
  const bak = backup(dest);
  cpSync(src, dest);
  console.log(`✓ plugin  ${f}${bak ? `（备份 ${bak.split("/").pop()}）` : ""}`);
}

// ---- standalone host ---------------------------------------------------
mkdirSync(WEB_DIR, { recursive: true });
for (const f of ["index.js", "style.css"]) {
  const src = join(standaloneDist, f);
  if (!existsSync(src)) continue;
  const dest = join(WEB_DIR, f);
  const bak = backup(dest);
  cpSync(src, dest);
  console.log(`✓ web     ${f}${bak ? `（备份 ${bak.split("/").pop()}）` : ""}`);
}

// index.html 从模板生成（lib 模式不会自己产出 html）
const tpl = join(uiRoot, "standalone.html");
if (existsSync(tpl)) {
  const html = readFileSync(tpl, "utf8");
  const dest = join(WEB_DIR, "index.html");
  const bak = backup(dest);
  writeFileSync(dest, html);
  console.log(`✓ web     index.html${bak ? `（备份 ${bak.split("/").pop()}）` : ""}`);
}

console.log("\n分发完成：");
console.log(`  插件 → ${PLUGIN_DIR}`);
console.log(`  独立 → ${WEB_DIR}`);