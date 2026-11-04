---
status: draft
stepsCompleted: [1, 2, 3]
inputDocuments:
  - _bmad-output/specs/spec-jarvis-workbench/SPEC.md
  - _bmad-output/specs/spec-jarvis-workbench/ARCHITECTURE-SPINE.md
  - _bmad-output/specs/spec-jarvis-workbench/PLAN.md
scope: P1（界面骨架）—— 后续批次 P2–P4 见文末预告
---

# Jarvis 人机工作台 - Epic Breakdown

## Overview

把 `SPEC.md` 的 13 项能力拆成可独立派发的 story。本文件当前只覆盖 **P1（界面骨架，约 2.5 天）**：让工作台**能真派活**，并把它与 Hermes 原生看板的关系摆正。P2–P4 的 story 在对应批次开工前追加。

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

### NonFunctional Requirements

| ID | 需求 |
|---|---|
| NFR-1 | 不改 `~/.hermes/hermes-agent/` 下任何文件（上游零改动） |
| NFR-2 | 不直接写 `kanban.db`，全部经 REST |
| NFR-3 | 首屏 ≤ 1s，轮询 5s（不引入 WebSocket） |
| NFR-4 | 改动可从 git 以外的方式回滚：只改 `workbench-app/` 与插件 `dashboard/` 两份 |
| NFR-5 | 服务重启后 `/healthz` 仍 200，不破坏正在跑的任务 |

### Additional Requirements

- 前端源在 `~/.hermes/plugins/workbench/dashboard/`，改完必须 `./sync-ui.sh`；直接改 `workbench-app/web/app.js` 会在下次同步被覆盖。
- 自定义后端端点只允许加在 `workbench-app/server.py` 的 `/api/workbench/*`。

### UX Design Requirements

- 三列宽度：流水线固定 302px，今日/长期等分剩余。
- 流水线面板底色比仓库深一档（凹槽 vs 平面），视觉分区即语义分区。
- 拖拽中整列高亮；被拒时卡片抖动 300ms + 红字提示。
- 明暗双主题全部走 `theme.css` 的 token，不硬编码色值。

### FR Coverage Map

| Story | 覆盖 |
|---|---|
| 1.1 看板回归原生 | FR-7 |
| 1.2 契约字段与派发闸门 | FR-2(部分) FR-4 FR-5 FR-8 FR-9 |
| 1.3 待办三区与拖拽 | FR-1 FR-2 FR-3 FR-8 |
| 1.4 派发回执 | FR-6 |

## Epic List

| Epic | 标题 | 价值 |
|---|---|---|
| 1 | 派发骨架（P1） | 工作台第一次能真派活，且不再维护一套自建看板 |

## Epic 1: 派发骨架（P1）

让「想法 → 仓库 → 拖进流水线 → 排队等 worker」这条最小链路在真实数据上跑通，同时把看板 tab 交还给 Hermes 原生实现。

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
- `~/.hermes/plugins/workbench/dashboard/index.js`（抽出 `COLUMNS` 常量、按它渲染、删掉 5 列旧映射）
- `~/.hermes/plugins/workbench/dashboard/style.css`（8 列横排 + 横向滚动）
- 同步：`workbench-app/sync-ui.sh`

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
**Then** 只显示标题、assignee、priority、block_kind、latest_summary 中存在的字段，无自造的状态徽章

**不做**：不嵌入 iframe、不打开 dashboard 页面、不改后端、不动待办三区。

---

### Story 1.2: 契约字段与派发闸门

As a 工作台用户，
I want 没有写验收的卡无法被派发，且派发时自动记录这次适用的预算与禁区，
So that 每张进流水线的卡都有人说得清"怎样算对"，事后也能追溯它按什么规则跑的。

**改动文件**：
- `workbench-app/server.py`（新增 `POST /api/workbench/dispatch/{task_id}`）
- `~/.hermes/plugins/workbench/dashboard/index.js`（拖拽前校验 + 409 提示）
- `workbench-app/web/boot.js` 或 `sdk-shim.js`（若需按运行形态切换端点）

**接口约定**：

```
POST /api/workbench/dispatch/{task_id}
  body: {}                       # 契约已在卡上，不在 body 里传
  200 → {"ok": true, "position": 2, "eta": "14:19", "status": "ready"}
  400 → {"ok": false, "error": "missing_acceptance"}      # contract.accept 为空
  409 → {"ok": false, "error": "blocked_by_parent", "status": "todo"}
```

服务端逻辑：读卡 → 校验 `metadata.contract.accept` 非空 → 写 `metadata.contract_defaults = {max_minutes, max_cost, deny_paths}`（三层默认合并结果）→ `PATCH status=ready` → 返回队列位次（按 `/board` 中 `ready` 列顺序）与预计开始时间。

**Acceptance Criteria:**

**Given** 一张 `metadata.contract.accept` 为空的卡
**When** 拖进流水线 / 直接 POST dispatch
**Then** 返回 400 `missing_acceptance`，卡仍在 `triage`，UI 抖动并提示"先补一句怎样算做完"

**Given** 一张验收非空的卡
**When** 派发成功
**Then** 卡状态变为 `ready`，且 `GET /api/plugins/kanban/tasks/{id}` 的 `metadata.contract_defaults` 含 `max_minutes`/`max_cost`/`deny_paths` 三个键

**Given** 一张有未完成 parent 的卡
**When** 派发
**Then** 接口返回 409，UI 提示"已转为 todo，等 parent 完成"，且不重复入队

**Given** 同一张卡
**When** 连续两次调用 dispatch
**Then** 第二次为幂等（返回 200 且状态仍 `ready`，位次不变，不产生第二条队列记录）

**不做**：不在前端写死预算数值（必须先读 `routing.json`，缺失时用全局默认文件 `workbench-app/defaults.json`）；不改 kanban 状态机。

---

### Story 1.3: 待办三区与拖拽派发

As a 工作台用户，
I want 待办页分成「流水线 / 今日 / 长期」三列，并从仓库把卡拖进流水线，
So that 派活这件事有明确的承诺动作，而不是在一张长清单里点来点去。

**改动文件**：
- `~/.hermes/plugins/workbench/dashboard/index.js`（布局重排、三区渲染、拖拽）
- `~/.hermes/plugins/workbench/dashboard/style.css`（三列网格、凹槽底色、拖拽高亮）

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

**改动文件**：`dashboard/index.js`（toast / 行内状态）、`style.css`。

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

## 后续 Epic 预告（暂不拆 story）

| Epic | 批次 | 内容 |
|---|---|---|
| 2 | P2 | 全屏验收模式（CAP-3）、P0 通知（CAP-4）、会话追溯（CAP-12） |
| 3 | P3 | 自述三行（CAP-6）、完成即固化（CAP-7）、定时唤醒（CAP-8） |
| 4 | P4 | 事件候选区（CAP-9）、四条配方（CAP-10）、熔断与成本（CAP-11） |
| 5 | P5 | 运行画布（CAP-13）、验收槽与抽样 |

拆下一批之前，先回填本文件 `stepsCompleted` 与对应 Epic。