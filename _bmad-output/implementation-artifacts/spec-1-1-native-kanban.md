---
title: '1.1 看板 tab 对齐 Hermes 原生 8 列'
type: 'refactor'
created: '2026-11-04'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
context: []
baseline_commit: '5b05aa11ecfff3e4d8c3a9d8f1b2e4fefe060154'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 工作台看板要与 Hermes 原生 8 列逐项对齐，但列定义必须单点、卡片只显示有值字段，避免两套列名和自造装饰。**现状**：`workbench-ui/src/board/columns.ts` 已是唯一列定义，`Board.tsx` 已按它渲染 8 列；剩余差异只在卡片空字段占位（`assignee` 兜底「未指派」、空时间 span）和运行中绿点。

**Approach:** 保持 `COLUMNS` 单点；`renderCard` 改为字段有值才渲染（assignee / `block_kind` / `latest_summary` / 相对时间），删除 `wb-live` 绿点；重建 `workbench-ui` 双产物并分发到插件宿主与独立版。**不嵌入 iframe，不打开 dashboard 页面**——看板仍由工作台自有前端渲染。

## Boundaries & Constraints

**决策（2026-11-04，人已确认）：** 以 `workbench-ui` 源码为唯一源；空 `assignee` 隐藏不占位；删除运行中绿点。

**Always:**
- 前端唯一源码在 `workbench-ui/src/`；产物由 `npm run build`（typecheck + plugin + standalone + distribute）生成，宿主目录副本绝不手改
- 8 列 key/label 固定为：`triage` 待整理 / `todo` 待执行 / `scheduled` 已排期 / `ready` 就绪 / `running` 运行中 / `blocked` 待拍板 / `review` 待验收 / `done` 已完成，顺序不变
- 列名与 8 列数组全库只出现在 `COLUMNS` 一处；由它 `map`/`reduce` 出来的查找表不算第二份定义
- 拖拽仍走原生状态机（`PATCH {status}`）：`running` 不可手动设，被拒要有中文原因
- 明暗双主题走既有 token，不硬编码色值

**Never:**
- 不嵌入 iframe、不打开 `9119` dashboard 页面
- 不改后端（`server.py` / `plugin_api.py`）、不新增端点、不引入依赖
- 不动待办三区与验收模式（属 Story 1.3 / M2）
- 不改 `workbench-app/sync-ui.sh`（旧架构脚本，已不在构建链路）

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开看板 | 任意数据 | 8 列横排，列名/顺序与原生一致，无第 9 列、无「队列/待收」旧列 | N/A |
| 就绪 → 运行中 | 拖拽 | 拒收，卡片回原位并提示 `running` 不能手动设 | 客户端拦，不发请求 |
| 待验收 → 已完成 | 拖拽 | `PATCH status=done` 成功，刷新后仍在「已完成」 | 409 时按原逻辑 toast 原因 |
| 卡片缺字段 | `assignee` / `block_kind` / `latest_summary` / 时间为空 | 对应片段完全不渲染，不占位、不显示 `null`；不出现运行中绿点 | N/A |
| 列多到放不下 | 窄视口 | 看板横向滚动，列不被压到不可读 | N/A |

</frozen-after-approval>

## Code Map

- `workbench-ui/src/board/columns.ts` — `COLUMNS`（唯一有序列定义）+ 派生 `COL_BY_KEY`/`COL_KEYS`；`colLabel`/`ENTERABLE`/`enterHint` 全部从中派生。本故事只核验不改。
- `workbench-ui/src/board/Board.tsx` — `renderCard`：本故事改动点（assignee/时间有值才渲染、删绿点）；`flowDrop`：`running` 拦截 + `ENTERABLE` 校验 + `PATCH {status}`；`COLUMNS.map`：列渲染。时间用 `completed_at || created_at`（API 无 `updated_at`），`timeLabel` 对 falsy 返回空串，条件渲染依赖该契约。
- `workbench-ui/src/styles/board.css` — `.wb-board` 横向滚动、`.wb-col` 固定 168px；`.wb-live` 规则已无使用者，本故事删除。
- `workbench-ui/package.json` — `build` = typecheck + plugin + standalone + distribute。
- `workbench-ui/scripts/distribute.mjs` — 分发两份产物，覆盖前备份 `.bak-<时间戳>`。
- `~/.hermes/plugins/workbench/dashboard/` 与 `workbench-app/web/` — 构建产物宿主，只由构建写入。
- `workbench-app/web/app.js`、`web/boot.js`、`web/sdk-shim.js`、`workbench-app/sync-ui.sh` — 旧架构遗留，未被 `index.html` 加载，本故事不动（见 Design Notes）。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-ui/src/board/Board.tsx` -- `renderCard` 只在字段有值时渲染 assignee / 时间，删除 `wb-live` 绿点 -- 卡片只呈现有值字段
- [x] `workbench-ui/src/styles/board.css` -- 删除已无使用者的 `.wb-live` 规则 -- 清掉死样式
- [x] `workbench-ui/src/board/columns.ts` -- 核验 `COLUMNS` 仍是唯一有序列定义，无改动（`COL_KEYS` 无消费者，另记 deferred-work）-- 列名单点
- [x] `workbench-ui/package.json` -- 执行 `npm run build`（typecheck + 双产物 + 分发）-- 插件与独立版同时更新
- [x] `jarvis-workbench.service` -- 重启服务并用 `/healthz` 验证 -- 独立版生效

**Acceptance Criteria:**
- Given 构建完成, when 对比 `dist/plugin` 与 `~/.hermes/plugins/workbench/dashboard/`（插件宿主不打开，见 Never）, then 两份产物逐字节一致；打开 `127.0.0.1:8787` 呈现 8 列，列名与顺序与原生逐项一致，无第 9 列、无「队列/待收」旧列
- Given 一张卡在「就绪」, when 拖到「运行中」, then 客户端拦截、卡片回原位、提示「运行中由调度器接管，不能手动设置」，且不发请求
- Given 一张卡在「待验收」, when 拖到「已完成」, then `PATCH status=done` 成功，`/board` 返回该卡 `status: "done"`，刷新后仍在「已完成」列
- Given `workbench-ui/src`, when 检索「待整理」/「待执行」/「已排期」等列名, then 只命中 `columns.ts` 的 `COLUMNS` 定义与一处说明性注释，不存在第二份有序 8 列数组
- Given 任意一张卡, when 观察内容, then 只出现标题、id、`P{n}`、assignee、`block_kind`、`latest_summary`、相对时间中实际有值的字段，无运行中绿点等自造徽章
- Given 改动完成, when `npm run build` + 重启 + `curl healthz`, then 返回 `{"ok":true}`，构建与浏览器控制台均无报错

## Implementation Notes

- 2026-11-04：`Board.tsx` 的 `renderCard` 改为 `time` 只算一次、非空才渲染 `wb-time`；`assignee` 非空才渲染 `wb-tag`；删除 `wb-live` 绿点与不再使用的 `COL_BY_KEY` 引入。`columns.ts` 核验后未改。
- `npm run build` 通过：typecheck 干净，plugin/standalone 两产物生成，`distribute.mjs` 覆盖插件宿主与 `workbench-app/web/`（新备份 `.bak-2026-11-04T14-51-57-*`）；`diff -q` 四个产物与宿主完全一致。
- 验收探针（headless Chromium + CDP）：8 列名称/顺序正确；空字段卡不渲染 assignee/时间/block/summary；就绪→运行中零请求且 toast「运行中由调度器接管，不能手动设置」；待验收→已完成发出 `PATCH {status:done}`，刷新后仍在已完成；760px 视口 `.wb-board` scrollWidth 1407 > clientWidth 716，列宽 168px。端到端持久化用 `WORKBENCH_PORT=8788` + 临时 `HERMES_KANBAN_HOME` 验证，真实 `kanban.db` 未改动（测试任务在 8787 返回 404）。
- 未覆盖：9119 插件页面未实际打开（spec 禁止）；明暗主题未做视觉走查；`board.css:148` 的 `.wb-live` 规则成为死代码，本故事未动。
- 2026-11-04（回环 1）：按修订 spec 重派生——`board.css` 的 `.wb-live` 死规则已删除（取代上一条「未动」的记录）；`Board.tsx` 改动与首轮相同。
- 回环 1 复验：typecheck 干净；`npm run build` 打印 5 行；4 个产物与宿主 `diff -q` 全部一致；重启后 `/healthz` ok；隔离 8788 端到端（8 列、空字段不渲染、就绪→运行中零请求、待验收→已完成 PATCH + `/board` done、刷新仍在、760px 滚动、Console 无错）；真实 `kanban.db` 的 SHA-256 与 mtime 未变。
- 回环 1 的最终产物来自 `.bak-2026-11-04T15-08-57-*` 那次构建；上述 `diff -q`、`/healthz` 与 8788 端到端均对这份最终构建执行。14-51-57 的备份是回环前首轮产物。

## Spec Change Log

### 2026-11-04 — 审查回环 1（bad_spec）

- **触发：** 三路审查发现非冻结段与实现/约束不一致：AC 要求打开 9119 插件看板，与 Never「不打开 9119」冲突；Code Map 行号停留在改动前；Verification 未覆盖 `style.css`、Console、绿点与 5 行分发日志；AC 弱于 epics 的 `/board` 断言；「全库唯一」的检索范围与旧 `app.js` 不符（BH-7）；`server.py` 任务与「不改后端」冲突；`## Spec Change Log` 标题被误删。
- **修订：** 按人确认的 Q1 决议（以 `workbench-ui` 为唯一源）重写冻结段 Problem/Approach/Boundaries；Code Map 改用函数级锚点；AC 改为「产物字节一致 + 8787 实测」，补 `/board` 断言、字段白名单与源码范围；Verification 补齐 4 个 diff、Console/绿点检查与 5 行日志；任务改用服务单元；恢复本日志标题；新增删除 `.wb-live` 死规则的任务。
- **避免的坏状态：** 验收项无法执行或无法复核；插件宿主验证与 Never 自相矛盾；审查者按旧行号核对错误位置。
- **KEEP：** 空 assignee 隐藏、空时间不渲染、无绿点；`COLUMNS` 单点；双宿主产物字节一致；拖拽回归证据（零请求 / 正确 PATCH / 刷新后仍在已完成）。

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH-1 | diff 未纳入 `workbench-app/web/index.js` | false | — | 该文件是构建产物；diff 头部已注明排除；`diff -q` 证明与 `dist/standalone/index.js` 字节一致；纳入 288KB 压缩代码无审查信号 |
| BH-2 | `.bak-*` 备份与构建产物策略缺失 | defer | defer | `distribute.mjs` 与已跟踪的 8 个 `.bak` 均早于本故事；本故事只多生成一组备份 |
| BH-3 | `## Spec Change Log` 标题被删 | low | bad_spec | 修改前 spec 只剩 `## Review Triage Log`，append-only 台账缺失；已在本轮恢复 |
| BH-4 | AC 要求打开插件看板，Never 禁止打开 9119 | medium | bad_spec | AC 与 Never 不可同时满足；插件宿主只能靠产物一致性验证 |
| BH-5 | I/O 矩阵无可重复测试 | defer | defer | `workbench-ui` 无 test 脚本/配置；CDP 探针一次性；Never 禁止新增依赖（与 VG-1 同源） |
| BH-6 | Code Map 行号停留在改动前 | low | bad_spec | 实测 `renderCard` 144-164、`COLUMNS.map` 195、build 脚本 11、`COL_KEYS` 50；已改为函数级锚点 |
| BH-7 | 「代码库」检索 AC 与命令范围不符；旧 `app.js` 仍有列名 | low | bad_spec | `workbench-app/web/app.js` 6 处命中且未被 `index.html` 加载；命令只查 `workbench-ui/src`；AC 已限定源码并注明旧产物 |
| BH-8 | 死规则 `.wb-live` | low | patch | `board.css` 已无 JSX 使用者；删除为单行改动（与 EC-1/VG-2 同源） |
| BH-9 | Verification 未支撑 style.css/Console/绿点/日志行数等声明 | low | bad_spec | 原命令只 diff 两个 `index.js`；`distribute` 实际打印 5 行；已在 Verification 补齐 |
| BH-10 | AC 弱于 epics：缺 `/board` status=done 断言 | low | bad_spec | epics 1.1 要求 `/board` 返回 done；原 AC 只写「刷新后仍在」；已补 |
| BH-11 | frozen Approach 夸大 delta | false | — | Problem 段已点名实际剩余差异；Approach 描述目标终态，不误导改动范围 |
| BH-12 | `server.py` 任务与「不改后端」冲突 | low | bad_spec | 原任务点名 `server.py`，diff 中无该文件改动；已改为 `jarvis-workbench.service` |
| BH-13 | `COL_KEYS` 无消费者 | defer | defer | 该导出在 `10f2685` 已存在，非本故事引入；删除属独立清理 |
| EC-1 | `.wb-live` 死规则（与 BH-8 同） | low | patch | 同 BH-8 |
| VG-1 | `renderCard` 空字段/绿点行为无可重复测试 | defer | defer | 预验证缺口；无测试栈且冻结段禁止新增依赖；建议后续固化 CDP 探针 |
| VG-2 | `.wb-live` 死规则（与 BH-8 同） | low | patch | 同 BH-8 |

**分组与路由：**
- **bad_spec 组**（BH-3/4/6/7/9/10/12，最高 medium）→ 回环 step-02：改非冻结段并重派生。
- **patch 组**（BH-8/EC-1/VG-2）→ 随重派生修复（已写入 Tasks）。
- **defer 组**（BH-2/BH-5/VG-1/BH-13）→ `deferred-work.md`。
- **false**（BH-1/BH-11）→ 驳回，不入后续。

### 回环 1 复审（2026-11-04，三路）

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH2-1 | frozen 段被改但未见重新协商记录 | false | — | Q1 人答「按 workbench-ui 重写」已记为「决策」行；Problem/Approach/Boundaries 的重写即该决议 |
| BH2-2 | `deferred-work.md` 未跟踪、不在 diff | false | — | 文件已在指定路径落盘并被 Tasks/Design Notes 引用；`git diff` 按设计不含未跟踪文件，交付物在工作树中 |
| BH2-3 | deferred 条目无 finding 编号 | low | patch | 已在每条 evidence 前补 BH/VG 编号 |
| BH2-4 | Implementation Notes 引用了首轮构建 | low | patch | 已补最终构建 `.bak-…15-08-57-*` 与检查对象 |
| BH2-5 | deferred 备份数量写成 3 | low | patch | 实际 6 个未跟踪（两轮 × 3），已更正 |
| BH2-6 | 「全库唯一」与旧 `app.js` 不符 | carried | carried（BH-7） | 同一主张；AC/命令已限定 `workbench-ui/src`，Design Notes 已说明旧产物 |
| BH2-7 | 插件宿主运行时未验证 | defer | defer | 字节一致只证明部署；9119 被 Never 禁止，运行时路径无法覆盖 |
| BH2-8 | 同一条 AC 混合产物对比与 8787 渲染 | false | — | 两者同属「双宿主对齐」验收，措辞不造成未覆盖行为 |
| BH2-9 | 三诊日志列语义混淆 | false | — | 表格为阅读辅助，裁决/路由两列并列不改变分流结果 |
| BH2-10 | Change Log 未覆盖冻结段修订 | low | patch | 已补 Q1 决议与 BH-7 |
| BH2-11 | Verification 缺 `未指派` 检查 | low | patch | 已把 `未指派` 并入 grep 检查 |
| BH2-12 | 建议提交探针 | carried | carried（VG-1） | 同 BH-5/VG-1；已记 deferred |
| BH2-13 | 生成物无哈希 | false | — | `diff -q` 四对产物已逐字节校验；哈希不增加可复核性 |
| BH2-14 | `columns.ts` 硬编码 `#8b5cf6` | defer | defer | 该行自 `10f2685` 存在，非本故事引入；已记 deferred |
| BH2-15 | I/O 矩阵未定义「有值」；空 title/assignee | false | — | API 拒绝空 title（`title.strip()` 校验），assignee 经 `normalize_profile_name` 拒绝空串；不可达 |
| BH2-16 | Code Map 丢失时间戳说明 | low | patch | 已补 `completed_at || created_at` 与 `timeLabel` 空串契约 |
| BH2-17 | 主题无人眼验收 / 无 AC | false | — | 本故事只删元素不加色；已用 computed style 抽查两组 token |
| BH2-18 | 旧「未动」记录仍在前 | false | — | 后续条目已明确「取代上一条」 |
| BH2-19 | diff 头部把字节一致套到 `.bak` | false | — | 头部只对生成物声明；备份为覆盖前快照 |
| BH2-20 | 生成物清单漏 `index.html` | false | — | 已核对 `standalone.html` 与 `workbench-app/web/index.html` 一致 |
| BH2-21 | 「5 行」不精确 | false | — | 指 5 条 ✓ 文件行；汇总行不影响核对 |
| VG2-1 | 字段隐藏无可执行验证 | carried | carried（VG-1） | 同一缺口（无测试栈） |
| VG2-2 | 绿点移除只靠 grep | carried | carried（VG-1） | 同一缺口；运行时探针已跑但未入库 |
| VG2-3 | 构建新鲜度无门禁 | defer | defer | 无 CI/构建门禁；已记 deferred |
| VG2-O1 | README 仍是旧流程 | defer | defer | 旧文档早于技术栈切换；已记 deferred |

**复审结论：** 无新增 bad_spec/intent_gap；Edge Case 层零发现。patch 项均为文档校正，已直接修正；defer 项已补入 `deferred-work.md`。无需再次回环。

## Design Notes

- 本故事已基本落地：`columns.ts` 与 `Board.tsx` 在 commit `10f2685` 引入；剩余差异只有 `renderCard` 的空值处理与绿点。
- 原生卡片显示 `unassigned` 占位；经人确认选择隐藏空字段，不跟随原生占位。
- 插件宿主不打开 9119（Never），用 `dist/plugin` 与宿主产物逐字节一致代替视觉验收；8787 独立版做真实渲染与拖拽验证。
- 构建会覆盖两个宿主目录；`distribute.mjs` 自带 `.bak-<时间戳>` 备份，无需 git 即可回滚。
- 冻结段的「全库唯一」按前端源码理解：旧 `workbench-app/web/app.js` 仍有一份未加载的旧映射，清理已记入 `deferred-work.md`，不构成本故事的第二份有效定义。
- 明暗主题未做人眼走查；重派生代理已用 computed style 抽查两组 token（深色卡片 `#121a17`、浅色 `#ffffff`）均生效，本故事只删元素、不加色值。
- 遗留（见 `deferred-work.md`）：旧 `app.js`/`sync-ui.sh`、`COL_KEYS` 无消费者、`.bak-*` 与生成物策略、构建新鲜度门禁、`columns.ts` 的 `#8b5cf6` 硬编码色、插件宿主运行时验收、无可重复 UI 测试、README 旧流程。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck` -- expected: 无类型错误
- `cd workbench-ui && npm run build` -- expected: 两个 mode 构建成功；distribute 打印 5 行（插件 `index.js`/`style.css`、web `index.js`/`style.css`/`index.html`）
- `diff -q workbench-ui/dist/plugin/index.js ~/.hermes/plugins/workbench/dashboard/index.js && diff -q workbench-ui/dist/plugin/style.css ~/.hermes/plugins/workbench/dashboard/style.css` -- expected: 无输出（一致）
- `diff -q workbench-ui/dist/standalone/index.js workbench-app/web/index.js && diff -q workbench-ui/dist/standalone/style.css workbench-app/web/style.css` -- expected: 无输出（一致）
- `systemctl --user restart jarvis-workbench` -- expected: 退出码 0
- `curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz` -- expected: `{"ok":true}`
- `grep -rn "待整理\|待执行\|已排期" workbench-ui/src` -- expected: 只命中 `columns.ts` 的 `COLUMNS` 定义与一处说明性注释
- `grep -rn "wb-live\|未指派" workbench-ui/src` -- expected: 无输出

**Runtime probe（headless Chromium + CDP，一次性脚本，不入库）:**
- 8 列名称/顺序；空字段卡无 tag/time/block/summary；就绪→运行中零请求且提示正确；待验收→已完成发出 `PATCH status=done` 且刷新后仍在；760px 视口横向滚动；Console 无错误。
- 持久化与 `/board` 断言用 `WORKBENCH_PORT=8788` + 临时 `HERMES_KANBAN_HOME` 实例，不碰真实 `kanban.db`。

**Manual checks:**
- 8787 看一遍：8 列名称/顺序对原生，窄窗口横向可滚动，Console 无报错（插件宿主不打开，按 Never 用产物一致性代替）
- 拿一张卡「就绪 → 运行中」：被拒并提示；「待验收 → 已完成」：成功且刷新后仍在「已完成」
- 明暗主题各看一遍：列底、卡片、标签都跟随主题