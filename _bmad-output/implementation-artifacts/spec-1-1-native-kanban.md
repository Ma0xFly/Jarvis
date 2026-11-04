---
title: '1.1 看板 tab 回归 Hermes 原生'
type: 'refactor'
created: '2026-11-04'
status: 'draft'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 工作台的「看板」tab 现在是一套自建 8 列渲染（`renderFlow` + `STATUS_ORDER` + 自绘卡），与 Hermes 原生看板并存。两套口径迟早对不上，改状态也要维护两份实现；架构基线 §7 明确要求「看板不重画」。

**Approach:** 删掉自建列渲染，「看板」tab 改为嵌入 Hermes 原生看板页（`http://127.0.0.1:9119/kanban`）的 iframe；只有宿主不宜嵌套或 iframe 加载不出来时，才退回兜底卡片——一句说明 + 「打开原生看板」主按钮 + 8 列只读计数（纯数字，不渲染卡片）。

## Boundaries & Constraints

**Always:**
- 只改 `~/.hermes/plugins/workbench/dashboard/index.js` 与 `style.css`，改完必须跑 `./sync-ui.sh` 生成 `workbench-app/web/app.js`
- 「看板」tab 内不得出现自绘的列或卡；只读计数允许保留
- 明暗双主题一律走既有 token，不硬编码色值
- 上游 `hermes-agent/` 零改动；不新增后端端点；不动其他 tab

**Never:**
- 不重画看板、不抽 `/board` 数据自行渲染列
- 不引入新依赖、不改 `plugin_api.py` 与 `server.py`
- 不改「待办」/「已完成」tab 的行为与 `metadata` 约定

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 独立版打开 | 8787，点「看板」 | iframe 内是原生 8 列看板 | N/A |
| iframe 被拦或 9119 未启动 | 加载失败/超时 | 切兜底卡片：说明 + 主按钮 + 8 列计数 | 不空白、无 console error |
| 被 9119 dashboard 内嵌 | 宿主为原生 dashboard | 按 Open Questions 结论执行 | N/A |
| 反复切 tab | 已进入过「看板」 | iframe 不重载，滚动位置保留 | N/A |

</frozen-after-approval>

## Open Questions

- 插件被 Hermes dashboard（9119）内嵌时，「看板」tab 怎么显示？ options: A) 照旧 iframe —— 最贴合架构基线字面，但会 dashboard 套 dashboard，iframe 内再点侧边栏「工作台」还会继续套娃 / B) 内嵌时直接渲染兜底卡片（外链按钮 + 8 列只读计数），只有独立版 8787 用 iframe —— 避免套娃、视觉更干净，推荐 / C) 内嵌时隐藏「看板」tab —— 暴露最小，但 tab 数量随宿主变化，容易困惑

## Code Map

- `~/.hermes/plugins/workbench/dashboard/index.js:23` — `STATUS_ORDER` 就是那套 8 列列定义数组，唯一使用点 `renderFlow:418`，随渲染函数一起删
- `.../index.js:38-51` — `ENTERABLE`(40) / `ENTER_HINT`(46) 只服务 `flowDrop` 的落列校验，一并删
- `.../index.js:121` — `dropCol` 只服务 `renderFlow` 的列高亮，删
- `.../index.js:234-246` — `flowDrop`（自建看板的「拖拽=改状态」）只被 `renderFlow` 调用，删
- `.../index.js:417-472` — `renderFlow` 自建渲染本体（`wb-stats`/`wb-board`/`wb-col`/`wb-card`/`wb-dropempty`），整体替换
- `.../index.js:575` — tab 分派三元表达式，`renderFlow` 那一支换成原生看板面板
- `.../index.js:137-148`、`159-166` — `load()` + 5s 轮询供 tab 徽标与兜底计数复用，保留；`board.columns[i].name` / `.tasks.length` 即计数来源
- `.../style.css:87-110` — `/* flow */` 段整段替换成原生看板面板样式；`.wb-root`(4-26) 是 `flex column / height:100%`，新面板给 `flex:1; min-height:0` 即可撑满
- `workbench-app/sync-ui.sh` — 单向 `cp` 到 `web/app.js`、`web/style.css`，不手改副本；`server.py:79-81` 的 `/healthz` 用作验证探针

## Tasks & Acceptance

**Execution:**
- [ ] `~/.hermes/plugins/workbench/dashboard/index.js` -- 删 `STATUS_ORDER`/`ENTERABLE`/`ENTER_HINT`/`dropCol`/`flowDrop`/`renderFlow`；加 `NATIVE_KANBAN_URL`、`openNative()`、`laneCounts(board)`、`renderNative()`（工具条 + iframe；失败切兜底卡片；首次进入后保持挂载） -- 看板回归原生且不留自绘列
- [ ] `~/.hermes/plugins/workbench/dashboard/style.css` -- `/* flow */` 段换成 `.wb-native`/`.wb-native-bar`/`.wb-native-frame`/`.wb-fallback`/`.wb-lanes` -- 撑满高度、双主题一致
- [ ] `workbench-app/sync-ui.sh` -- 跑同步，把改动带到 `web/app.js` 与 `web/style.css` -- 独立版才看得到

**Acceptance Criteria:**
- Given 服务已重启且 9119 在跑, when 打开 8787 点「看板」, then iframe 内是原生 8 列（Triage/Todo/Scheduled/Ready/In Progress/Blocked/Review/Done），没有自绘的列或卡
- Given 仓库代码, when 检索 `renderFlow` / `STATUS_ORDER` / `wb-col` / `wb-card` / `flowDrop`, then 无残留调用点（只读计数辅助函数除外）
- Given 改动完成, when 执行 `./sync-ui.sh && systemctl --user restart jarvis-workbench && curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz`, then 返回 `{"ok":true}`，控制台无报错
- Given 9119 不可达或 iframe 被拦, when 打开「看板」, then 出现兜底卡片（说明 + 主按钮 + 8 列计数），不是空白
- Given 已进入过「看板」, when 切到「待办」再切回, then iframe 不重载（无整页白闪）

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- 原生页无 `X-Frame-Options`、无 CSP `frame-ancestors`（2026-11-04 实测），可直接嵌入；但它自带完整 dashboard 侧边栏与看板工具条，比自建 8 列「重」——这是复用原生的代价。
- 兜底不用跨域探针：9119 不回 `access-control-allow-origin`，其 `/healthz` 其实是 SPA fallback（1385 字节 HTML）。只能靠 iframe `onLoad` + 超时判定。「是否被内嵌」用宿主注入的 `window.__HERMES_SESSION_TOKEN__` 判断（独立版 `index.html` 不注入）。

## Verification

**Commands:**
- `cd ~/Projects/Jarvis/workbench-app && ./sync-ui.sh` -- expected: 打印「已同步：app.js / style.css」
- `systemctl --user restart jarvis-workbench` -- expected: 退出码 0
- `curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz` -- expected: `{"ok":true}`
- `grep -n "renderFlow\|STATUS_ORDER\|flowDrop\|wb-col\|wb-card" ~/.hermes/plugins/workbench/dashboard/index.js workbench-app/web/app.js` -- expected: 无输出
- `diff -q workbench-app/web/app.js ~/.hermes/plugins/workbench/dashboard/index.js` -- expected: identical

**Manual checks:**
- 8787 →「看板」：iframe 内是原生 8 列，Console 无报错；「待办」↔「看板」来回切不出现整页重载
- 停掉 9119 再打开「看板」：出现兜底卡片，主按钮能开新窗口