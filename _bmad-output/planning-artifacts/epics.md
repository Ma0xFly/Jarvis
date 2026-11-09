---
status: final
stepsCompleted: [1, 2, 3]
p2StepsCompleted: [1, 2, 3]
p3StepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _bmad-output/specs/spec-jarvis-workbench/SPEC.md
  - _bmad-output/specs/spec-jarvis-workbench/ARCHITECTURE-SPINE.md
  - _bmad-output/specs/spec-jarvis-workbench/PLAN.md
  - sketches/006-final/index.html
  - sketches/007-dispatch-pipeline/index.html
scope: P1/P2 已完成；P3 stories 已拆完（3.1–3.5），待 sprint planning
---

# Jarvis 人机工作台 - Epic Breakdown

## Overview

把 `SPEC.md` 的 13 项能力拆成可独立派发的 story。**P1（界面骨架）与 P2（闭环）已完成**（Epic 1 的 1.0–1.4、Epic 2 的 2.1–2.3 全部 done）；本文件正在追加 **P3（变强）**：自述三行（CAP-6）、完成即固化（CAP-7）、定时唤醒（CAP-8）、执行器契约（CAP-14）。P4–P5 的 story 在对应批次开工前追加。

## Requirements Inventory

### Functional Requirements

| ID | 需求 | 来源 |
|---|---|---|
| FR-1 | 待办页呈现三区：流水线（最左）/ 今日待办 / 长期待办 | CAP-1 |
| FR-2 | 仓库里的卡只有标题 + 验收一行，无状态点、无进度、无 assignee | CAP-1 / CAP-2 |
| FR-3 | 拖进流水线 = 派发；整列为投放区 | CAP-1 |
| FR-4 | 验收为空的卡不允许派发，前后端都拦 | CAP-2 |
| FR-5 | 派发瞬间把预算/禁区生效值快照进 `metadata.contract_defaults` | CAP-2 |
| FR-6 | 派发后 3 秒内显示排队位次与预计开始时间 | CAP-1 |
| FR-7 | 看板 tab 以自有前端渲染，但 8 列名/序/status 与原生逐项对齐，列定义单点 | CAP-5 |
| FR-8 | 今日/长期用 `metadata.bucket` 区分，不占用 `todo` 状态 | CAP-2 |
| FR-9 | 派发遇到未完成 parent 返回 409，前端提示"已转 todo" | CAP-2 |
| FR-10 | 全屏验收：从右上角计数或待收行进全屏，逐张展示产出物/验收标准/本轮运行信息；A 收 / R 不收 / S 跳过 / Esc 退出；队列清空自动退出并给本批统计；review 超 48h 按规则静默通过并记录 | CAP-3 |
| FR-11 | 验收动作：accept → done；reject 写理由 comment 并回 triage；skip 保留；走 `POST /api/workbench/verify/{task_id}` | CAP-3 |
| FR-12 | P0 通知：会堵下游的卡 10 秒内系统通知 + 右上角常驻/置顶；非 P0 不打扰；同一事件 30 分钟只升级一次；P1 攒批、P2 日报；通知内可收/不收/10 分钟后再提醒；倒计时代替未读；验收槽容量 3 | CAP-4 |
| FR-13 | 会话追溯：验收/已完成卡可「打开执行会话」，读 `worker_session_id` / `executor_session_id`，看到该轮完整对话并可用执行器续跑参数接上 | CAP-12 |
| FR-14 | 验收页在样张之后显示 `run.metadata.self_report` 的交付/依据/没底三行；「没底」为空或缺失时显示明确警示 | CAP-6 |
| FR-15 | 验收通过前校验最新 run `metadata.solidified` 至少一条 `{type,path,status:pending}`；固化内容仍落 Hermes profile `skills/` / `routing.json`，工作台只做登记展示与 `pending→verified` 审计（verified 写 workbench.db overlay） | CAP-7 |
| FR-16 | 仓库卡可设 `metadata.wake_at`；到点由工作台后台把卡移入 `ready`（有验收且无未完成 parent）或 `todo`（有 parent）；缺验收则留在仓库并提示 | CAP-8 |
| FR-17 | 新增 `bin/executor-run` + `executors.json`：stdin 任务书、stdout 末行 JSON、五个退出码；适配器只翻译；回写 `metadata.executor_session_id`；`routing.json` 可指定 executor | CAP-14 |

### NonFunctional Requirements

| ID | 需求 |
|---|---|
| NFR-1 | 不改 `~/.hermes/hermes-agent/` 下任何文件（上游零改动） |
| NFR-2 | 不直接写 `kanban.db`，全部经 REST |
| NFR-3 | 首屏 ≤ 1s，轮询 5s（不引入 WebSocket） |
| NFR-4 | 改动可从 git 以外的方式回滚：源码只改 `workbench-ui/` 与 `workbench-app/`，插件 `dashboard/` 与 `web/` 是构建产物 |
| NFR-5 | 服务重启后 `/healthz` 仍 200，不破坏正在跑的任务 |
| NFR-6 | P0 只走桌面系统通知，手机通道不做（接口预留） |
| NFR-7 | 验收/通知/会话均 5s 轮询，不引入 WebSocket；首屏 ≤ 1s |
| NFR-8 | 上游零改动；`state.db` 只读反查，不直接写 `kanban.db`；新端点只加在 `/api/workbench/*` |
| NFR-9 | 执行器 CLI/适配器不改上游、不读 `kanban.db`、不写业务逻辑；只做参数/输出翻译 |
| NFR-10 | `self_report` / `solidified` / `executor_session_id` 缺失或畸形时降级展示，不 500 |
| NFR-11 | 唤醒只处理带 `wake_at` 的卡；仓库卡继续 `scheduled`，无 `not_before` 的原生卡不受影响；唤醒幂等 |
| NFR-12 | 密钥不进任务书/skill；适配器不记录密钥 |

### Additional Requirements

- 前端唯一源码在 `workbench-ui/src/`；改完 `npm run build` 分发到插件 dashboard 与 `workbench-app/web/`。旧 `sync-ui.sh` 与 `web/app.js` 流程已作废。
- 自定义后端端点只允许加在 `workbench-app/server.py` 的 `/api/workbench/*`。
- P2 端点：`GET /api/workbench/inbox`（review + P0 blocked 聚合）、`POST /api/workbench/verify/{task_id}`（accept/reject/skip）、会话追溯只读 `state.db` 与任务 metadata。
- P2 规则：review 超 48h 自动收并记录；验收槽容量 3，兄弟卡成束；桌面通知走系统通知（notify-send/桌面环境），手机通道不做。
- 会话追溯优先读 `task.metadata.worker_session_id`；执行器卡读 `executor_session_id`，dsh 续跑参数由执行器契约提供。
- P3 决策（2026-11-06）：CAP-6 读最新 run `metadata.self_report`；CAP-7 读最新 run `metadata.solidified`（`kanban_request_review` 写入 `task_runs.metadata`，不是 `tasks.metadata`），固化内容复用 Hermes profile `skills/` 与 `routing.json`，手动 verified 写 workbench.db 的 `solidified_verified` overlay，**不新建记忆库**；CAP-8 用 workbench `wake_at`，不采用 Hermes 原生 `not_before`，避免与仓库卡占用的 `scheduled` 冲突。
- `bin/executor-run`、`executors.json`、`routing.json` 放在 `~/Projects/Jarvis`；不改 `~/.hermes/hermes-agent/`。
- 三个 profile 的 skill 补：自述三行、`solidified` 登记、`executor_session_id` 回写、`kanban_request_review` 而非 `kanban_complete`。
- P3 适配器范围：`hermes` + `claude_code` 完整实现，`dsh` + `codex` 注册并 smoke；`--stream` 作为后续增强。

### UX Design Requirements

- UX-DR1：三列宽度：流水线固定 302px，今日/长期等分剩余。
- UX-DR2：流水线面板底色比仓库深一档（凹槽 vs 平面），视觉分区即语义分区。
- UX-DR3：拖拽中整列高亮；被拒时卡片抖动 300ms + 红字提示。
- UX-DR4：明暗双主题全部走 `theme.css` 的 token，不硬编码色值。
- UX-DR5：验收全屏布局——顶栏「验收 1/3 + 进度 + 退出」、左侧产出物样张（可翻页）、右侧验收标准 + 自述/运行信息、底部 `A/R/S`。
- UX-DR6：键盘优先（A/R/S/Esc）；拒绝理由为必填输入，不填不能提交。
- UX-DR7：通知内可直接操作（收/不收/10 分钟后再提醒）；右上角 P0 常驻/置顶；review 卡显示「还有 42h 自动收」倒计时。
- UX-DR8：验收槽固定容量 3；同一次 decompose 的兄弟卡成束。
- UX-DR9：验收页顺序固定「样张 → 自述三行」；「没底」单独强调；缺失态可见。
- UX-DR10：验收页显示固化记录（类型/路径/状态）；缺失时给出阻断原因。
- UX-DR11：仓库卡显示「将于 HH:MM 唤醒」与「提前唤醒」。
- UX-DR12：会话抽屉展示 `executor_session_id` 与「续跑」入口（P3 先显示/复制，实际续跑接执行器契约）。

### FR Coverage Map

| Story | 覆盖 |
|---|---|
| 1.0 构建脚手架 | 使能项（无 FR） |
| 1.1 看板对齐原生 | FR-7 |
| 1.2 契约字段与派发闸门 | FR-2(部分) FR-4 FR-5 FR-8 FR-9 |
| 1.3 待办三区与拖拽 | FR-1 FR-2 FR-3 FR-8 |
| 1.4 派发回执 | FR-6 |
| 2.1 全屏验收与验收动作 | FR-10 FR-11 |
| 2.2 P0 通知与通知操作 | FR-12 |
| 2.3 会话追溯入口 | FR-13 |
| 3.1 验收页自述三行 | FR-14 |
| 3.2 固化登记与验收门禁 | FR-15 |
| 3.3 定时唤醒 | FR-16 |
| 3.4 执行器契约与 claude_code 适配器 | FR-17 |
| 3.5 其余适配器与路由切换 | FR-17 |

### UX-DR Coverage Map

| UX-DR | Story |
|---|---|
| UX-DR1–4（三列/凹槽/拖拽反馈/主题 token） | 1.1、1.3 |
| UX-DR5–6（验收全屏布局/键盘） | 2.1 |
| UX-DR7–8（通知操作/验收槽） | 2.2 |
| UX-DR9（自述三行） | 3.1 |
| UX-DR10（固化展示与阻断） | 3.2 |
| UX-DR11（唤醒时间/提前唤醒） | 3.3 |
| UX-DR12（executor session/续跑入口） | 3.5 |

NFR coverage：NFR-9/NFR-12 → 3.4/3.5；NFR-10 → 3.1/3.2；NFR-11 → 3.3；其余 NFR 沿用 Epic 1/2 的约束。

## Epic List

| Epic | 标题 | 价值 |
|---|---|---|
| 1 | 派发骨架（P1） | 工作台第一次能真派活，且不再维护一套自建看板 |
| 2 | 闭环（P2） | 收得掉、叫得应、追得回 |
| 3 | 变强（P3） | 验收更可判断、完成有沉淀、挂起能自回队、执行器可替换 |

## Epic 1: 派发骨架（P1）

让「想法 → 仓库 → 拖进流水线 → 排队等 worker」这条最小链路在真实数据上跑通，同时把看板 tab 交还给 Hermes 原生实现。

### Story 1.0: 前端构建脚手架（React + TS + Vite，双产物）

As a 开发者，
I want 一条 `npm run build` 就能产出两份可用的 IIFE 包，
So that 后续所有 UI story 都改同一份源码，且两端行为一致。

**背景**：插件宿主用经典 `<script>` 加载（无 `type="module"`），且已把 React 注入 `window.__HERMES_PLUGIN_SDK__`。
所以产物必须是 IIFE，且插件包**不能**再打包 React。

**目录**：

```
workbench-ui/
├── package.json          # react/react-dom/typescript/vite/@vitejs/plugin-react
├── vite.config.ts        # 两个 mode：plugin（external react）/ standalone（打包 react）
├── tsconfig.json
├── index.html            # 仅 standalone 用，含 <div id="workbench-root">
├── scripts/distribute.mjs# 把产物复制到两个宿主目录
└── src/
    ├── main.tsx          # 唯一入口：检测宿主
    ├── platform.ts       # api = SDK.fetchJSON | plainFetch
    └── App.tsx           # 占位组件（一个标题即可）
```

**改动文件**：`workbench-ui/**`（新建）、`.gitignore`（加 `workbench-ui/node_modules/`、`workbench-ui/dist/`）。

**Acceptance Criteria:**

**Given** 干净环境
**When** `cd workbench-ui && npm install && npm run build:plugin`
**Then** 产出 `dist/plugin/index.js` 与 `dist/plugin/style.css`，且 `head -c 300 dist/plugin/index.js` 含 `window.__HERMES_PLUGIN_SDK__.React`

**Given** 同一份源码
**When** `npm run build:standalone`
**Then** 产出 `dist/standalone/index.js`、`style.css` 与 `index.html`，且该 js 内**不含** `__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED`（说明 React 已打进 standalone）

**Given** plugin 产物
**When** `grep -c "__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED" dist/plugin/index.js`
**Then** 结果为 `0`（React 没被打进插件包）

**Given** 构建完成
**When** `npm run build`（含 distribute）
**Then** 两个宿主目录的 `index.js` / `style.css` 与 dist 一致，`curl -s 127.0.0.1:8787/healthz` 仍 `{"ok":true}`

**不做**：不实现任何业务 UI；不改 `plugin_api.py`；不动 `manifest.json` 的 entry/css 路径（仍为 `index.js` / `style.css`）。

---

### Story 1.1: 看板 tab 对齐 Hermes 原生 8 列

As a 工作台用户，
I want 工作台自己的看板用 Hermes 那 8 列的名称和顺序，
So that 我不用记两套列名，也不会在拖拽时把状态搞错。

**背景**：现有 `app.js` 自建了 5 列（队列 / 运行中 / 待收 / 今日仓库 / 已完成），与原生 8 列对不上。
目标**不是嵌入 dashboard**，而是用自己的前端渲染原生那 8 列。

**列定义（唯一常量，照抄）**：

| key | label |
|---|---|
| `triage` | 待整理 |
| `todo` | 待执行 |
| `scheduled` | 已排期 |
| `ready` | 就绪 |
| `running` | 运行中 |
| `blocked` | 待拍板 |
| `review` | 待验收 |
| `done` | 已完成 |

**改动文件**：
- `workbench-ui/src/board/columns.ts`（新增 `COLUMNS` 常量）
- `workbench-ui/src/board/Board.tsx`（按常量渲染，删掉旧 5 列映射）
- `workbench-ui/src/styles/board.css`（8 列横排 + 横向滚动）
- 构建分发：`npm run build`

**Acceptance Criteria:**

**Given** 服务已重启
**When** 打开「看板」tab
**Then** 呈现 8 列，列名与顺序与上表完全一致，无第 9 列、无"队列/待收"这类旧列

**Given** 一张卡在「就绪」列
**When** 拖到「运行中」
**Then** 请求被拒（`running` 不可手动设），卡片回原位并提示原因

**Given** 一张卡在「待验收」列
**When** 拖到「已完成」
**Then** PATCH `status=done` 成功，刷新后仍在「已完成」列，`/board` 返回的 status 为 `done`

**Given** 代码库
**When** 检索 `待整理` / `待执行` / `已排期` 等列名
**Then** 命中只出现在 `COLUMNS` 定义处（一处），不存在第二份 8 列数组

**Given** 任意一张卡
**When** 观察卡片内容
**Then** 只显示 id、标题、assignee、priority、block_kind、latest_summary、created_at/completed_at 中存在的字段，无自造的状态徽章

**不做**：不嵌入 iframe、不打开 dashboard 页面、不改后端、不动待办三区。

---

### Story 1.2: 契约字段与派发闸门

As a 工作台用户，
I want 没有写验收的卡无法被派发，且派发时自动记录这次适用的预算与禁区，
So that 每张进流水线的卡都有人说得清"怎样算对"，事后也能追溯它按什么规则跑的。

**改动文件**：
- `workbench-app/server.py`（`POST /api/workbench/dispatch/{task_id}`、`GET /api/workbench/metadata`、ready 中间件闸门：PATCH ready / 非 triage 建卡 / bulk ready / reclaim）
- `workbench-app/defaults.json`（全局预算 + 7 分钟/张，唯一默认源）
- `workbench-app/tests/test_dispatch.py`（可重复集成测试，临时 `HERMES_KANBAN_HOME`/`WORKBENCH_DB`）
- `workbench-ui/src/api/workbench.ts`（调用 + 错误码 + `hasAcceptance` / `getTaskMetadata` / `mergeTaskMetadata`）
- `workbench-ui/src/platform.ts`（错误对象保留 HTTP 状态与后端 `error` 码）

**存储**：工作台 metadata 存 `workbench-app/workbench.db` 的 `task_metadata`
（JSON1 按 key 更新，`write_token` 记录快照归属且不对外返回），不读写共享
`kanban.db`；任务状态与队列位次仍只经 `/api/plugins/kanban/*` 公开 REST。
预算只做“全局默认 + 卡上 `contract.budget` 覆盖”；routing 层在 1.2 不实现。
数值上限：`max_minutes ≤ 10080`、`max_cost ≤ 1000`、`minutes_per_task ≤ 1440`。

**接口约定**：

```
POST /api/workbench/dispatch/{task_id}
  body: {}                       # 契约已在卡上，不在 body 里传
  200 → {"ok": true, "position": 2, "eta": "14:19", "status": "ready"}
  200 → {"ok": true, "position": 0, "eta": null, "status": "running"}   # 已 running，不 PATCH
  400 → {"ok": false, "error": "missing_acceptance"}                    # contract.accept 为空
  409 → {"ok": false, "error": "blocked_by_parent", "status": "todo"}   # 有未完成 parent
  409 → {"ok": false, "error": "not_ready", "status": "review"}         # 上游实际落点不是 ready
  409 → {"ok": false, "error": "not_dispatchable", "status": "done"}
  404 / 500 / 502 / 503 → not_found / invalid_defaults / upstream_unavailable / metadata_unavailable

GET /api/workbench/metadata?ids=a,b
  200 → {"a": {"contract": {...}, "contract_defaults": {...}, "bucket": "today"}}
        # 无记录的 id 省略；workbench.db 不可用时 503 metadata_unavailable
```

服务端逻辑：读卡（先判 404；running → 200 `position:0`；done/archived → 409，
均不要求验收）→ 校验 `metadata.contract.accept` 非空 → 合并 `defaults.json` 与卡上
`contract.budget` 得到 `{max_minutes, max_cost, deny_paths}` → 先写
`contract_defaults` 快照（不覆盖已有快照，带 `write_token`）→ `PATCH status=ready`
→ **再 GET `/tasks/{id}` 读取真实落点**；只有 ready 才按 `/board` 的 `ready` 列顺序
算位次与 `eta = now + minutes_per_task × 位次`（board 未命中重读一次，仍不命中 502）。
上游实际落点不是 `ready` 时返回 409 并补偿删除本次快照。ready 中间件同时拦住
`PATCH status=ready`、无 `triage` 的建卡、`bulk status=ready` 与 `reclaim`；PATCH
不存在的 id 先 GET，404 透传而不是误报 400。空验收一律不转发。

**Acceptance Criteria:**

**Given** 一张 `metadata.contract.accept` 为空的卡
**When** 直接 `PATCH status=ready`、无 `triage` 建卡、`bulk status=ready`、`reclaim` 或 POST dispatch
**Then** 返回 400 `missing_acceptance`，卡状态不变，不发起上游状态变更、不入队

**Given** 一个不存在的 id
**When** 直接 `PATCH status=ready`
**Then** 先 GET 卡，404 原样透传，不误报 `missing_acceptance`

**Given** 一张验收非空的卡
**When** 派发成功
**Then** 卡状态变为 `ready`，且 `GET /api/workbench/metadata?ids={id}` 的 `contract_defaults` 含 `max_minutes`/`max_cost`/`deny_paths` 三个键

**Given** 一张有未完成 parent 的卡
**When** 派发
**Then** 接口返回 409 `blocked_by_parent`，卡落 `todo`，ready 队列不新增，UI 提示"已转为 todo，等 parent 完成"

**Given** 上游把卡落到 `review` / `todo`（如 unblock 恢复 review）
**When** 派发
**Then** 返回 409 `not_ready` / `blocked_by_parent`，`status` 为实际落点，快照补偿删除

**Given** PATCH 已成功
**When** 用 GET `/tasks/{id}` 读到落点不是 `ready`，或 `/board` 连续两次没有该卡
**Then** 前者返回 409 并补偿快照；后者返回 502 `upstream_unavailable`，不返回 position 0

**Given** 同一张已 ready 的卡
**When** 再次调用 dispatch
**Then** 返回 200，按当前 ready 队列重算位次，不重复入队、不覆盖已有快照

**Given** 一张 done / archived 的卡
**When** 派发
**Then** 返回 409 `not_dispatchable`，不复活

**Given** `defaults.json` 类型/范围非法
**When** 派发
**Then** 返回 500 `invalid_defaults`，不写快照

**不做**：不实现待办三区/拖拽 UI（Story 1.3 调本故事的 dispatch）；不在前端写死预算数值；不接 routing 层；不改 kanban 状态机；不写共享 `kanban.db`；不实现 provenance / `contract.ready`；不给上游 `recompute_ready` 或原生 9119 dashboard 加 watcher（明确为范围外已知限制）；不改自调用线程池设计；不删除已跟踪的 `.bak-*` / 旧 UI 文件（清理 deferred）。

---

### Story 1.3: 待办三区与拖拽派发

As a 工作台用户，
I want 待办页分成「流水线 / 今日 / 长期」三列，并从仓库把卡拖进流水线，
So that 派活这件事有明确的承诺动作，而不是在一张长清单里点来点去。

**改动文件**：
- `workbench-ui/src/todo/TodoPage.tsx`（三列网格、三区渲染、拖拽）
- `workbench-ui/src/styles/todo.css`（凹槽底色、投放高亮）

**布局约定**：`grid-template-columns: 302px 1fr 1fr`，顺序固定为 流水线 / 今日 / 长期；底部保留一句话建卡输入框（默认落 `bucket=today`）。

**Acceptance Criteria:**

**Given** 已存在若干 `bucket=today` 与 `bucket=long` 的卡
**When** 打开待办 tab
**Then** 列顺序为 流水线→今日→长期；今日只出现 today 卡，长期只出现 long 卡，计数与渲染数量一致

**Given** 仓库中的卡
**When** 静态观察
**Then** 无状态点、无进度条、无 assignee 徽标、无边框高亮（hover 只换背景色）

**Given** 拖动今日的一张卡
**When** 指针进入流水线列的任意纵向位置
**Then** 整列出现投放高亮；松手后走 Story 1.2 的 dispatch 流程

**Given** 拖动一张验收为空的卡进流水线
**When** 松手
**Then** 卡回到原位，抖动 300ms，出现红字提示，不发起网络请求

**Given** 新建一张卡
**When** 在底部输入框回车
**Then** 卡落今日列，验收栏为空并以警示色标注，且不自动派发

**不做**：不实现长期→今日的自动倒流；不实现排序持久化（沿用 `localStorage`）。

---

### Story 1.4: 派发回执与排队位次

As a 工作台用户，
I want 派发后立刻看到"第几位、什么时候开始"，
So that 我知道这次点击生效了，不会因为没反馈而连点三次。

**改动文件**：`workbench-ui/src/todo/Receipt.tsx`（toast / 行内状态）、`workbench-ui/src/styles/todo.css`。

**Acceptance Criteria:**

**Given** 派发成功返回 `{position, eta}`
**When** UI 收到响应
**Then** 3 秒内在该卡位置或顶部 toast 显示「已入队 · 第 N 位 · 预计 HH:MM」

**Given** 页面刷新
**When** 重新加载 `/board`
**Then** 队列位次按 `ready` 列顺序重算，显示的位次与新状态一致（不依赖本地缓存）

**Given** 队列已有 2 张卡
**When** 再派发 1 张
**Then** 显示的预计时间按 `now + 每张平均耗时 × 位次` 计算，且与 `/api/workbench/dispatch` 返回值一致

**不做**：不做进度条动画；不做预估模型训练（先用固定 7 分钟/张的占位估算，写在 `defaults.json`）。

---

## Epic 2: 闭环（P2）

让跑完的活能在全屏验收里逐张判断，P0 卡主动叫人，任何一张卡都能跳回执行会话。

### Story 2.1: 全屏验收与验收动作

As a 工作台用户，
I want 在全屏里逐张收掉 `review` 卡、拒绝时留下理由，
So that 跑完的活有明确结论，队列不会自堵。

**改动文件**：`workbench-app/server.py`、`workbench-ui/src/verify/`、`workbench-ui/src/api/workbench.ts`、`workbench-ui/src/App.tsx`、`workbench-ui/src/todo/TodoPage.tsx`、`workbench-ui/src/styles/verify.css`。

**Acceptance Criteria：** 见 `_bmad-output/implementation-artifacts/spec-2-1-fullscreen-verify.md`（已实现；34 项后端测试 + 31 项验收探针）。

**不做**：P0 通知（2.2）、会话查看器（2.3）、完成即固化闸门（P3）。

### Story 2.2: P0 通知与通知操作

As a 工作台用户，
I want 只有会堵下游的卡主动打扰我，且通知里就能处理，
So that 不被无关提醒淹没，也不漏掉真正卡住的事。

**Acceptance Criteria：** 见 `_bmad-output/implementation-artifacts/spec-2-2-p0-notifications.md`（已实现；P0 判定、系统通知、30 分钟只升级一次、通知内收/不收/稍后）。

### Story 2.3: 会话追溯入口

As a 工作台用户，
I want 从验收/已完成卡直接打开该轮执行会话，
So that 判断和排障不必去翻日志。

**Acceptance Criteria：** 见 `_bmad-output/implementation-artifacts/spec-2-3-session-trace.md`（已实现；`worker_session_id` 路径优先，`executor_session_id` 续跑属 Epic 3）。

## Epic 3: 变强（P3）

让验收页能读到可反驳的自述、完成卡能沉淀出可复用件、挂起的卡能到点自己回队，并把任务交给可插拔执行器；四项共享 `server.py`、验收页与 worker skill，因此作为一个 Epic 交付。

**FRs covered:** FR-14, FR-15, FR-16, FR-17

### Story 3.1: 验收页自述三行

As a 工作台用户，
I want 在验收页先看样张、再看 AI 自述的「交付 / 依据 / 没底」，
So that 我能用可反驳的自述判断产物，而不是被总结措辞锚定。

**改动文件**：`workbench-app/server.py`（inbox item 读取 latest run `metadata.self_report`）、`workbench-ui/src/api/workbench.ts`（类型）、`workbench-ui/src/verify/VerifyPage.tsx` + `styles/verify.css`（展示）、`workbench-app/tests/test_verify.py`、`workbench-ui/scripts/verify-probe.mjs`；worker skill 契约写入 `workbench-conventions` / 三个 profile 的 review 约定。

**Acceptance Criteria:**

**Given** 一张 review 卡的最新 run 带 `metadata.self_report = {delivered, evidence, uncertain}`
**When** 打开全屏验收
**Then** 样张仍在最前，右侧验收标准之后依次显示「交付 / 依据 / 没底」三行
**And** 「没底」视觉上单独强调

**Given** `uncertain` 为空、缺失或 `self_report` 结构畸形
**When** 打开该卡
**Then** 显示「自述缺失 / 没底为空」警示，不抛 500，也不影响 A/R/S 操作
**And** 旧卡没有 `self_report` 时同样降级显示

**Given** worker 按新契约调用 `kanban_request_review`
**When** 工作台读取 `GET /tasks/{id}`
**Then** 最新 run metadata 带有 `self_report`，且 fixture 测试覆盖三种字段
**And** 缺失时返回 `null` 而不是伪造内容

**不做**：不因自述缺失阻断收卡；不让模型生成自述；不新建表或改上游 schema。

### Story 3.2: 固化登记与验收门禁

As a 工作台用户，
I want 在验收时看到这张卡沉淀了什么，并在收卡前确认至少一条可复用件，
So that 完成卡不会只留下结果而没有下次可用的资产。

**改动文件**：`workbench-app/server.py`（读取/校验最新 run `metadata.solidified`、accept 门禁、手动 verified 写 workbench.db overlay）、`workbench-ui/src/api/workbench.ts`、`workbench-ui/src/verify/VerifyPage.tsx` + `styles/verify.css`、`workbench-app/tests/test_verify.py`、`workbench-ui/scripts/verify-probe.mjs`；三个 profile 的 review skill 契约。

**Acceptance Criteria:**

**Given** review 卡的最新 run `metadata.solidified` 至少有一条合法记录
**When** 打开验收页
**Then** 显示每条固化的 `type`（`routing` / `skill_patch` / `script`）、`path`、`status`（`pending` / `verified`）
**And** 合法记录存在时允许 accept

**Given** 最新 run `metadata.solidified` 缺失、为空或没有合法记录
**When** 用户按 A / 调用 `POST /api/workbench/verify/{id}` 的 accept
**Then** 返回 400 `missing_solidified`，卡仍留在 review
**And** reject / skip 不受影响；48h 自动收仍按现有规则执行（作为队列保底，不要求固化）

**Given** 一条 `status=pending` 的固化记录
**When** 用户点「标记已验证」
**Then** 工作台在 `workbench.db` 的 `task_metadata.solidified_verified` 记录 `{path, verified_at}`，不 PATCH 上游 run metadata
**And** UI 把该条显示为 verified；`verified` 不改变 accept 门禁；上游 run 记录保持 pending 作为原始登记

**Given** `solidified` 是单个对象、数组或含畸形字段
**When** 读取该卡
**Then** 归一化为列表并忽略非法条目；全部非法时按缺失处理，不 500

**Given** worker 按新契约在 `kanban_request_review` 写入 `solidified`
**When** 工作台读取详情
**Then** 能从最新 run `metadata.solidified` 读到该记录；fixture 覆盖 pending / verified / 缺失
**And** 固化内容仍写 profile `skills/` / `routing.json`，工作台不新建记忆库

**不做**：不做自动引用扫描（P3 只做手动 verified）；不由工作台编辑 profile skill 文件；不阻断 reject/skip；不改上游内核。

### Story 3.3: 定时唤醒

As a 工作台用户，
I want 给挂起的卡设一个唤醒时间，到点自动回队，也能手动提前，
So that 不用靠记忆把卡捞回来。

**改动文件**：`workbench-app/server.py`（`wake_at` 校验、due 扫描、dispatch/todo 落地）、`workbench-app/workbench.db` 的 `task_metadata` JSON（无 schema 迁移）、`workbench-ui/src/todo/TodoPage.tsx` + `todo/TaskDetail.tsx`（时间显示/设置/提前唤醒）、`workbench-ui/src/api/workbench.ts`、`workbench-app/tests/test_todo.py`、`workbench-ui/scripts/todo-probe.mjs`。

**Acceptance Criteria:**

**Given** 一张仓库卡
**When** 设置 `wake_at = now + 5m`
**Then** 卡片显示「将于 HH:MM 唤醒」，metadata 持久化；清除后显示消失

**Given** `wake_at` 到期，卡有验收且无未完成 parent
**When** 后台唤醒扫描运行
**Then** 走现有 dispatch 流程进入 `ready`（写 `contract_defaults`、位次按现队列计算），清空 `wake_at`
**And** 重复扫描不会二次派发

**Given** `wake_at` 到期且卡有未完成 parent
**When** 扫描运行
**Then** 卡进入 `todo`（显式唤醒后的等待态），清空 `wake_at`；不直接进 `ready`

**Given** `wake_at` 到期但验收为空
**When** 扫描运行
**Then** 卡留在 `scheduled`，写入可见的 `wake_error=missing_acceptance`，不自动派发
**And** 用户补验收后可手动唤醒

**Given** 用户点「提前唤醒」
**When** 验收存在
**Then** 立即走 dispatch/提前唤醒路径；验收缺失则提示并保留卡
**And** `wake_at` 只在该卡真正离开仓库后清空

**Given** Hermes 原生 `scheduled` 卡没有 `wake_at`
**When** 唤醒扫描运行
**Then** 扫描不读取、不修改该卡
**And** 仓库卡继续停 `scheduled`，不采用 Hermes `not_before`

**不做**：不做周期任务/重复唤醒；不做时区选择（本地时间显示，存 epoch）；不改上游调度器。

### Story 3.4: 执行器契约与 claude_code 适配器

As a 工作台/worker，
I want 用一个统一 CLI 把任务交给 claude_code，并拿到统一 JSON 与退出码，
So that 执行器可以替换而不改业务流程。

**改动文件**：`bin/executor-run`（新建）、`bin/executors/claude_code.py`（新建）、`executors.json`（新建）、`tests/test_executor_run.py`（新建）、`workbench-app/README.md`；`routing.json` 的 `executor` 字段留到 3.5。

**Acceptance Criteria:**

**Given** `executors.json` 注册了 `claude_code`
**When** `executor-run --executor claude_code --cwd <dir> --mode write --budget-minutes 30 --max-usd 2` 从 stdin 读取任务书
**Then** 调用 `claude -p --output-format json`（或等价非交互命令），stdout 最后一行是统一 JSON
**And** JSON 含 `ok`、`executor`、`session_id`、`summary`、`cost`、`log_path`、`exit_code`

**Given** 执行器成功、需要人、超预算、不可用、其它错误
**When** CLI 退出
**Then** 分别返回 0 / 2 / 3 / 4 / 1；调用方只依赖这五个码
**And** 缺二进制映射为 4，超时/预算中断映射为 3

**Given** `--resume <session-id>`
**When** 适配器执行
**Then** claude_code 收到 `--resume`，session id 透传
**And** 无 session id 时 JSON 字段为 `null`，不伪造

**Given** 适配器执行
**When** 检查依赖
**Then** 不 import Hermes 内部模块、不读写 `kanban.db`、不把密钥写入日志
**And** 任务书只从 stdin 读取，参数只做翻译

**不做**：不做 streaming（后续增强）；不接 dsh/codex（3.5）；不做插件；不改 Hermes 内核。

### Story 3.5: 其余适配器与路由切换

As a 工作台用户，
I want 能用 dsh/codex 等执行器，并让 routing 按任务类型选执行器，
So that 换执行器不改业务流程，coder 可以切到 claude_code。

**改动文件**：`bin/executors/{dsh,codex,hermes}.py`、`executors.json`、`routing.json`、`tests/test_executor_run.py`、`workbench-app/README.md`、`workbench-conventions`/coder profile 配置；`workbench-ui/src/session/SessionViewer.tsx`（展示 `executor_session_id` / 续跑入口）。

**Acceptance Criteria:**

**Given** `dsh` 适配器
**When** 以 `--resume <session-id>` 执行
**Then** 映射为 `dsh --session-id`，从 NDJSON 首行解析 `session-<uuid>`
**And** `codex` 适配器注册并可 smoke；不可用返回 4

**Given** `hermes` 执行器
**When** 通过 CLI 调用
**Then** `executors.json` 标记为 `mode: in-process`；`executor-run --executor hermes` 拒绝并返回 4
**And** 错误信息说明 hermes 在 worker 进程内运行，不伪装成已执行的外部 CLI

**Given** `routing.json` 的规则带 `executor`
**When** worker 处理匹配任务
**Then** 使用该执行器调用 `executor-run`；事件/建卡方不能自行覆盖 executor
**And** 无 `executor` 字段时保持现有默认

**Given** coder 切到 `claude_code`
**When** 跑一张 fixture/真实任务
**Then** `kanban_request_review` 的 metadata 带 `metadata.executor_session_id`
**And** 会话抽屉显示该 id 与续跑入口（P3 先显示/复制，实际续跑接执行器契约）

**Given** claude_code 不可用
**When** 切换验证
**Then** 记录回滚到 dsh 的步骤，不把环境问题伪装成成功

**不做**：不做自动 fallback 链；不做流式模式；不做执行器管理页；不新建执行器插件。

## 后续 Epic 预告（暂不拆 story）

| Epic | 批次 | 内容 |
|---|---|---|
| 4 | P4 | 事件候选区（CAP-9）、四条配方（CAP-10）、熔断与成本（CAP-11） |
| 5 | P5 | 运行画布（CAP-13）、验收槽与抽样 |

拆下一批之前，先回填本文件 `stepsCompleted` 与对应 Epic。