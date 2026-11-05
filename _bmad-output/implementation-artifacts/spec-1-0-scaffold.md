---
title: '1.0 前端构建脚手架（React + TS + Vite，双产物）'
type: 'enablement'
created: '2026-11-05'
status: 'done'
route: 'record'
story: '1.0'
baseline_commit: '859562a9ebac58ba666a44ed791aa4e3d8aa1f34'
implementation_commit: '10f2685d2fc49f1221f0ebec8ee5b11bdd6f0337'
---

# Story 1.0 · 前端构建脚手架（补记）

> **这是一份事后补记的交付记录，不是开发前冻结的 spec。** Epic 1 复盘 action item 18
> 要求「给 Story 1.0 补 spec 或正式记录『使能项无 spec』的决定」；本文件选择后者，
> 并记录 as-built 证据与验收复核。

## 决定：使能项，不追溯伪造 spec

Story 1.0 是**使能项（enabler）**，不直接实现任何 FR（`epics.md` 的 FR Coverage Map
标注为「使能项（无 FR）」）。它在 P1 开工前作为一次性基础设施落地，因此没有按功能
故事走「spec → 冻结 → 三路审查」流程；`epics.md` 中 Story 1.0 的目录、构建命令与四条
AC 实际承担了 spec 的角色。

Epic 1 复盘确认了两个证据缺口：

1. 实现提交 `10f2685` 的主题是「docs: 新增学习手册…」，未点名 1.0，git 归因不清；
2. `implementation-artifacts/` 下没有 1.0 的 spec 文件。

本文件不追溯性发明当时的冻结文本，只记录**实际建成了什么、按什么标准验收、后来如何
演化**。后续纯使能项若仍不单独写 spec，应在 `epics.md` 中保留可验收的 AC，并在实现
提交主题里点名 story。

## 目标（来自 epics.md）

一条 `npm run build` 产出两份可用的 IIFE 包，后续所有 UI story 改同一份源码：

- 插件宿主用经典 `<script>` 加载（无 `type="module"`），且已把 React 注入
  `window.__HERMES_PLUGIN_SDK__`；插件包**不能**再打包 React。
- 独立版自己打包 React 并挂到 `#workbench-root`。
- 产物分发到 `~/.hermes/plugins/workbench/dashboard/` 与 `workbench-app/web/`，
  宿主目录只放产物。

## 实际建成（commit 10f2685，2026-11-04）

| 文件 | 作用 |
|---|---|
| `workbench-ui/package.json` | react/react-dom、typescript、vite；`typecheck` / `build:plugin` / `build:standalone` / `build` |
| `workbench-ui/vite.config.ts` | 两个 mode：plugin（React external + 宿主全局 banner）/ standalone（打包 React）；IIFE；CSS 固定输出 `style.css` |
| `workbench-ui/tsconfig.json` | TypeScript 配置 |
| `workbench-ui/index.html` | `vite dev` 入口（`#workbench-root`） |
| `workbench-ui/standalone.html` | 独立版页面模板（`/static/index.js`、`/static/style.css`、`#workbench-root`） |
| `workbench-ui/src/main.tsx` | 宿主检测：有 SDK → `PLUGINS.register`；否则 `createRoot(#workbench-root)` |
| `workbench-ui/src/platform.ts` | 数据层：`SDK.fetchJSON` 或原生 `fetch`；错误形状统一 |
| `workbench-ui/src/App.tsx` / `src/board/*` | 首个渲染目标（看板首版；1.1 在此对齐 8 列） |
| `workbench-ui/src/styles/base.css` / `board.css` | 基础 token 与看板样式 |
| `workbench-ui/scripts/distribute.mjs` | 两份产物复制到插件 dashboard 与 `workbench-app/web/`，覆盖前备份；从 `standalone.html` 生成 `workbench-app/web/index.html` |
| `.gitignore` | 忽略 `workbench-ui/node_modules/` 与 `workbench-ui/dist/`（`859562a` 随技术栈定稿加入） |

注：`10f2685` 同时带入了 1.1 的看板首版（`Board.tsx` / `columns.ts`），所以它不是纯
1.0 提交；1.0 的脚手架文件是该提交的前半部分。后续 1.1–1.4 与 2.1 都只改
`workbench-ui/src/` 并通过同一构建分发，未再新建构建链。

## 验收复核（按 epics.md 的 1.0 AC）

| AC | 结果 | 证据 |
|---|---|---|
| `npm install && npm run build:plugin` 产出 `dist/plugin/index.js` + `style.css`，banner 含 `window.__HERMES_PLUGIN_SDK__.React` | 通过 | `vite.config.ts` 的 plugin banner；插件包 external React（`__SECRET_INTERNALS...` 计数为 0） |
| 同一份源码 `npm run build:standalone` 产出 `dist/standalone/index.js` + `style.css` | 通过 | standalone 打包 React；实际产物为 js + css 两个文件，`index.html` 由 distribute 从 `standalone.html` 模板生成到 `workbench-app/web/` |
| `npm run build` 含 typecheck + 两次 build + distribute，两个宿主产物与 dist 一致 | 通过 | `package.json` `build` 脚本；`distribute.mjs` 逐文件复制并备份 |
| `/healthz` 仍 `{"ok":true}`，不破坏既有服务 | 通过 | Epic 1 现场检查；后续 1.1–2.1 继续复用同一构建链 |

宿主差异只出现在 `src/main.tsx`：有 SDK 注册 tab；无 SDK 整页渲染。1.0 之后唯一影响
脚手架行为的修正是 Epic 1 复盘 #7（`bf47b03`）：`main.tsx` 与 `platform.ts` 统一宿主
谓词为 `sdk && sdk.fetchJSON`；有 SDK 但没有 `fetchJSON` 时抛出明确错误，不再误判为
插件宿主。

## 后续演化

- 1.1 起 `Board.tsx` 改为 8 列常量渲染；1.3 增加 `TodoPage.tsx`；2.1 增加
  `VerifyPage.tsx`。构建拓扑（唯一源 → 两份 IIFE 产物）未变。
- `npm run verify` 后来扩展为 `build + probe:todo + probe:verify`（1.1/2.1 的
  headless 探针）；仍不新增前端测试框架（见 `deferred-work.md`）。
- 插件宿主只渲染看板 tab；待办三区与全屏验收只在独立版 8787 提供。宿主目录产物
  仍禁止手改，旧 `sync-ui.sh` 已作废。

## 证据

- `_bmad-output/planning-artifacts/epics.md` Story 1.0（目录、命令、AC）
- `git show --stat 10f2685`；`git show 10f2685:workbench-ui/...`
- `_bmad-output/implementation-artifacts/epic-1-retro-2026-11-05.md`（1.0 无 spec、
  提交主题未点名；action item 18）
- `_bmad-output/specs/spec-jarvis-workbench/ARCHITECTURE-SPINE.md` §3（两套 build 差异）