---
title: '3.3 定时唤醒'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
baseline_commit: 'da3840762626954eebd4bf382bf19fff663f93b7'
review_loop_iteration: 1
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 仓库卡只能靠人记得拖回流水线；挂起的卡没有“到点自己回队”的机制，长期/今日列表会积压。

**Approach:** 仓库卡在 workbench metadata 写顶层 `wake_at`（epoch seconds）；后台 `WakeScheduler` 扫描到期卡：有验收且无未完成 parent 时走现有 dispatch 进 `ready`，有 parent 进 `todo`；缺验收留在 `scheduled` 并写 `wake_error`。卡片/详情显示唤醒时间，可设置/清除/提前唤醒；Hermes 原生无 `wake_at` 的 `scheduled` 卡完全不动。

## Boundaries & Constraints

**Always:** 新仓库卡默认停 `scheduled`；`wake_at` 只写 workbench metadata；只有带 `wake_at` 的卡进入扫描；成功进入 `ready` 或 `todo` 后清空 `wake_at`/`wake_error`/`wake_next_try_at`；缺验收保留 `wake_at`、写 `wake_error` 并退避，不自动派发；手动提前唤醒与后台共用同一 `wake_task` 逻辑；重复扫描幂等。

**Never:** 不使用 Hermes 原生 `not_before`/改上游调度；不碰没有 `wake_at` 的原生 `scheduled` 卡；缺验收仍留在仓库；不新建表/不改 Hermes 内核；不把 `wake_error`/`wake_next_try_at` 开放给客户端写。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 到期可派发 | `wake_at<=now`、有验收、无 parent | 走 `_dispatch_locked` 进 `ready`，写 `contract_defaults`，清空 wake 字段 | 失败保留 `wake_at` + `wake_next_try_at` |
| 到期有 parent | `wake_at<=now`、有未完成 parent | PATCH `todo`，清空 wake 字段 | 失败保留并退避 |
| 到期缺验收 | `wake_at<=now`、验收为空 | 留在 `scheduled`，`wake_error=missing_acceptance`，`wake_next_try_at=now+300`，保留 `wake_at` | 不自动派发；补验收后自动重试或手动提前 |
| 手动提前 | 仓库卡 + 提前唤醒 | 与到期同一路径立即执行 | 缺验收 400 `missing_acceptance`；已离开仓库 409 `not_in_store` |
| 原生 scheduled | 无 workbench `wake_at` | 扫描不读取/不修改 | N/A |
| 非法 wake_at | 非 int / bool / 负数 | metadata PATCH 400 `invalid_metadata` | 不写入 |
| 上游失败 | dispatch/PATCH 502 | 保留 `wake_at`，`wake_next_try_at=now+60`，`wake_error=upstream_unavailable` | 下轮重试 |

</frozen-after-approval>

## Code Map

- `workbench-app/server.py` -- `METADATA_PATCH_KEYS` 加 `wake_at` 校验；`MetadataStore.due_wake_tasks(now)` 用 JSON1 找到期行；`WorkbenchService.wake_task()`（`_task_lock` 内复用 `_dispatch_locked`，有 parent 则 `_patch_status("todo")`）；`WakeScheduler`（照 `ReviewSweeper`，env `WORKBENCH_WAKE_SWEEP_SECONDS`，默认 30s，0 关闭）；`POST /api/workbench/wake/{task_id}`。
- `workbench-app/workbench.db` -- 复用 `task_metadata`：`wake_at`、`wake_error`、`wake_next_try_at`；无 schema 迁移。
- `workbench-ui/src/api/workbench.ts` -- `TaskMetadata`/`TaskMetadataPatch` 加 wake 字段；`wakeTask(taskId)`；错误码沿用 `missing_acceptance`/`not_in_store`/`upstream_unavailable`。
- `workbench-ui/src/todo/TodoPage.tsx` -- 仓库卡有 `wake_at` 时显示「将于 HH:MM 唤醒」chip；`TaskDetail` 传 `onWakeChange`/`onWakeNow`。
- `workbench-ui/src/todo/TaskDetail.tsx` + `styles/detail.css` -- 仓库卡「定时唤醒」区：`datetime-local`、5 分钟后、设置/清除、提前唤醒、`wake_error` 警示；无 wake 时保持原仓库卡两行结构。
- `workbench-app/tests/test_wake.py`（新建）-- 到期 ready/parent todo/缺验收/原生 scheduled/手动/非法值/调度器；`test_todo.py` 补 metadata 校验。
- `workbench-ui/scripts/todo-probe.mjs` -- 断言 wake chip、详情设置/清除/提前唤醒与错误态。
- `workbench-app/README.md` + `ARCHITECTURE-SPINE.md` -- 记录 wake_at/端点/env；Hermes 原生 scheduled 不受影响。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- metadata `wake_at` 校验 + `due_wake_tasks` -- 数据层
- [x] `workbench-app/server.py` -- `wake_task`/`WakeScheduler`/`POST /api/workbench/wake/{id}` -- 调度
- [x] `workbench-ui/src/api/workbench.ts` -- wake 类型/`wakeTask` -- 前端契约
- [x] `workbench-ui/src/todo/TodoPage.tsx` -- 卡片 wake chip + 详情回调 -- 入口
- [x] `workbench-ui/src/todo/TaskDetail.tsx` + `styles/detail.css` -- 唤醒控件/错误态 -- UI
- [x] `workbench-app/tests/test_wake.py` + `test_todo.py` -- 调度/端点/校验 -- 回归
- [x] `workbench-ui/scripts/todo-probe.mjs` -- chip/设置/提前唤醒/错误 -- 探针
- [x] `workbench-app/README.md` + `ARCHITECTURE-SPINE.md` -- 文档 -- 文档
- [x] `workbench-ui` -- `npm run build` 分发 -- 双宿主

**Acceptance Criteria:**
- Given 到期卡有验收且无 parent，when 唤醒，then 进 `ready`、写快照、清空 wake 字段。
- Given 到期卡有未完成 parent，when 唤醒，then 进 `todo`、清空 wake 字段，不直接 ready。
- Given 到期卡缺验收，when 唤醒，then 留 `scheduled`、`wake_error=missing_acceptance`、退避重试、不派发；补验收后可自动或手动唤醒。
- Given 仓库卡，when 设置/清除/提前唤醒，then UI 显示/移除时间并执行对应动作。
- Given Hermes 原生 `scheduled` 卡无 `wake_at`，when 扫描，then 状态不变。
- Given 非法 `wake_at`，when PATCH metadata，then 400；上游失败时保留 wake_at 并退避。
- Given 改动完成，when 跑 test_wake + test_todo + probe:todo + build，then 全绿，`/healthz` ok。

## Implementation Notes

- 2026-11-06：`test_wake` 12、`test_todo` 40、`test_dispatch` 38、`test_verify` 66、`test_notifications` 39、`test_session` 27；四套探针 193/45/29/26；typecheck/build、`/healthz` 全绿。
- 2026-11-06 review patch：R1–R17、R21 已修；`wake_task` 加 `require_due` 复检、detail 用 dispatch 预算、清字段失败仍 200、非 200 错误透传；测试补到 `test_wake` 25、其余 40/38/66/39/27；四套探针 196/45/29/26；typecheck/build、`/healthz` 全绿。
- `wake_task` 读取完整 detail 的 `links`/`link_tasks` 做 parent 判断；后台 `WakeScheduler` 默认 30s，测试用 0/0.2s；无 `wake_at` 的原生 `scheduled` 卡不扫描。
- 缺验收保留 `wake_at` 并 5 分钟退避；上游 502 保留 `wake_at` 并 60s 退避；手动提前唤醒不受退避限制。

## Spec Change Log

- 2026-11-06 review R16：澄清 Always——新仓库卡默认停 `scheduled`；有未完成 parent 时转 `todo` 并清空 wake 字段，原文“成功离开仓库后清空”与 parent→todo 路径矛盾。

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| R1 Edge1/VG-other | due scan 返回 id 后，用户清除/推迟 wake_at，scheduler 仍会唤醒 | high | patch | `wake_due` 取 id 后 `wake_task` 不复检 due；需 `require_due` |
| R2 Edge2 | 任一行 metadata JSON 损坏会让 `json_type` 抛错，整轮 sweep 失败 | medium | patch | `due_wake_tasks` 无 `json_valid` 守卫 |
| R3 Edge4/BH | 负/异常的 wake_at 被反复选中，可能每 30s 空转或误唤醒 | low | patch | query 未限 `wake_at >= 0`；`_has_wake_at` 只影响清理 |
| R4 BH | `wake_task` 用 `GATE_UPSTREAM_TIMEOUT` 0.75s 读完整 detail，过紧 | medium | patch | 最重的 upstream 读；应使用 dispatch/review 预算 |
| R5 BH | `_wake_to_todo` 信任 PATCH 200，不复读真实落点 | medium | patch | 与 `_dispatch_locked` 的“200 不等于落地”原则不一致 |
| R6 BH | 卡已进 ready/todo 后 `_clear_wake_fields` 失败却返回 503 | medium | patch | 主操作成功；应 200 + 日志 |
| R7 BH | 非 200 dispatch 结果统一映射 502/60s 退避，吞掉 invalid_defaults/metadata_unavailable/409 | medium | patch | `wake_task` else 分支 |
| R8 BH | 补验收后不清理 wake_error，详情仍显示缺验收 | medium | patch | `update_metadata`/`update_task` 只在 wake_at PATCH 时清 |
| R9 BH/Edge3 | `wakeTask` 无 timeout/abort，挂起时所有唤醒控件永久禁用 | medium | patch | 对照 `dispatchTask` 的 5s race |
| R10 BH | “5 分钟后”被分钟截断成 4–5 分钟；probe 下界会偶发失败 | medium | patch | `wakeAtValue` 丢秒；断言 `wakeSetFrom+250` |
| R11 BH | `wake_at` 无上限，epoch ms 也能写入，卡永远不会到期 | medium | patch | 加 2100 上限/now+10y，补测试 |
| R12 BH | 测试 harness 未默认关闭 30s WakeScheduler，污染既有套件 | medium | patch | `WorkbenchServer.start` 只关 notify |
| R13 VG1-5/BH | 缺 dispatch 清理/409 保留、未来 wake_at、scheduler 异常隔离、env 0/非法、backoff 到期重试、无 wake_at/todo 手动唤醒、503-after-success 等测试 | medium | patch | `test_wake.py` 未覆盖这些路径 |
| R14 BH | 仓库卡只有 chip，无 wake_error/next_try；chip 无跨日日期；wakeNow 丢 position/eta | medium | patch | `TodoPage.tsx`/`TaskDetail.tsx` |
| R15 BH/Edge5 | `clock(0)` 返回 “—”；wakeActionError 未映射 not_found/metadata_unavailable | low | patch | `TaskDetail.tsx` |
| R16 BH | README 仍写 metadata 只收 bucket/contract；workbench.ts JSDoc、ARCHITECTURE inventory/cost note 过期；spec Always 与 parent→todo 矛盾；尾换行 | medium | patch | 文档/规格对账 |
| R17 BH | 非法 `WORKBENCH_WAKE_SWEEP_SECONDS` 静默关闭调度，无启动日志 | low | patch | `_env_wake_sweep_seconds` |
| R18 BH | 无 `POST /wake/sweep`、成功唤醒无日志 | low | defer | 可选运维入口/可观测性 |
| R19 Edge6 | legacy triage/todo 卡缺验收时 wake 只返回 400，不停车，仍可能被上游提升 | low | defer | 新仓库卡走 scheduled；遗留卡后续迁移 |
| R20 BH/VG | 3.2/3.3 状态、构建产物、diff 路径/尾换行 | — | false | sprint 在 step-05 同步；构建产物由 probe 覆盖；diff 是审查工件 |
| R21 VG4 | probe 的 wake_error 断言被本地 action error 满足，未验证 metadata 分支 | medium | patch | 关闭/重开 drawer 后再断言 metadata 文案 |
| R22 BH | malformed wake_at 被静默跳过，无日志/清理 | low | defer | 外部 DB 污染；可后续加日志 |

**分组与路由：** G1 scheduler 正确性（R1/R2/R3/R4/R5/R6/R7/R12/R13）；G2 验收错误清理与 UI（R8/R9/R14/R15/R21）；G3 边界/文档（R10/R11/R16/R17）；defer：R18/R19/R22；false：R20。无 intent_gap / bad_spec；其余 patch。

## Design Notes

- `wake_task` 必须用 `_dispatch_locked`（已持 `_task_lock`），不能调 `dispatch()` 造成同锁重入。
- `wake_next_try_at` 只给调度器退避用；手动提前唤醒不受它限制。
- 缺验收保留 `wake_at`，用 `wake_next_try_at` 做 5 分钟退避，避免每 30s 重复报错；补验收后自动重试或手动提前。
- `wake_at` 顶层而非 `contract` 内，便于 JSON1 扫描；客户端只能写 `wake_at`，error/next 由服务端写。

## Verification

**Commands:**
- `python3 workbench-app/tests/test_wake.py` -- 全绿
- `python3 workbench-app/tests/test_todo.py` -- 全绿
- `cd workbench-ui && npm run typecheck && npm run build` -- 类型 + 双产物
- `cd workbench-ui && npm run probe:todo` -- chip/设置/提前唤醒通过
- `systemctl --user restart jarvis-workbench`；`curl /healthz` -- `{"ok":true}`
