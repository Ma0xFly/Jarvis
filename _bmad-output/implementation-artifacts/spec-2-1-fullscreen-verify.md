---
title: '2.1 全屏验收与验收动作'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: 'c649c554c52d504405069ae15c21ed2e25a77493'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** worker 跑完后卡停在 `review`，用户只能翻看板或日志逐张判断；没有全屏验收界面，拒绝不强制写理由，`review` 超时也没有自动收规则，队列会自堵。

**Approach:** 新增 `GET /api/workbench/inbox` 与 `POST /api/workbench/verify/{task_id}`，以及全屏 `VerifyPage`：一张一屏展示验收标准、产出物（`result`/`latest_summary` + 附件）与本轮运行信息；`A` 收 → `done`，`R` 不收（理由必填）→ comment + `triage`，`S` 跳过，`Esc` 退出；队列清空给本批统计；`review` 超 48h 自动收并留记录。右上角 review 计数与流水线「待收」行是入口。

## Boundaries & Constraints

**决策（2026-11-05，人已确认）：** 产出物用文本（result/latest_summary）+ 附件（链接/图片内联），不引入 PDF/iframe 预览；48h 自动收 = 后台 5 分钟扫描 + inbox 读取兜底；验收队列按最早进入 review 排序（先清积压）。

**Always:**
- 验收是全屏模式，不是 tab；只在独立版 8787 提供（插件宿主只渲染看板）。
- `A` → `PATCH status=done`（summary「验收通过」）；`R` 理由前后端都必填，先写 comment 再 `PATCH status=triage`；`S` 不改状态、前进到下一张；`Esc` 退出。
- 验收页数据只读：workbench metadata 的 `contract.accept/input` + `GET /tasks/{id}` 的 `result`/`latest_summary`/`attachments`/`events`/`runs`；不自造产出物。
- 48h 自动收：review 时长取最近一次 `review_requested` 事件；超时 → comment「48h 自动收」+ `PATCH status=done`；幂等，不重复收。
- 右上角 review 计数与队列来自 inbox；5s 轮询，不引入 WebSocket。
- 上游零改动、不直接写 `kanban.db`；新端点只加在 `workbench-app/server.py` 的 `/api/workbench/*`。

**Never:**
- 不做 P0 通知（2.2）、不做会话查看器（2.3，仅预留位置）、不做完成即固化闸门（P3）。
- 不做产出物编辑器/嵌入 iframe；不新增前端依赖。
- 验收页不直接 PATCH 上游，必须走 verify 端点。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开验收 | 有 review 卡 | 一张一屏：验收标准、result/summary、附件、运行信息；计数 1/N | N/A |
| A 收 | review 卡 | `PATCH done` + summary；卡片离开队列 | 409/502 保留在队列并提示 |
| R 不收（无理由） | review 卡 | 400 `reason_required`，状态不变 | 前端不提交 |
| R 不收（有理由） | review 卡 | comment 写入 + `PATCH triage`；卡片离开队列 | comment 失败则不改状态 |
| S 跳过 | review 卡 | 状态不变，前进到下一张；计入 skipped | N/A |
| 48h 超时 | review 事件早于 48h | 自动 done + comment；不出现在 inbox | 幂等 |
| 队列清空 | 最后一张处理完 | 显示本批 accepted/rejected/skipped 统计，退出 | N/A |
| 非 review 卡 | 已被处理/状态变化 | 409 `not_in_review` | 刷新队列 |
| 上游不可用 | PATCH/comment 失败 | 502/503；卡片留在队列 | 不谎报 |
| 插件宿主 | 9119 | 不渲染验收 UI，只显示看板 | N/A |

</frozen-after-approval>

## Code Map

- `workbench-app/server.py` — `WorkbenchService.inbox()/verify()`；`GET /api/workbench/inbox`（review 卡 + metadata + review_age_seconds，按最早 review 排序）；`POST /api/workbench/verify/{task_id}`（accept/reject/skip）；48h `ReviewSweeper`（后台线程，5 分钟，daemon；inbox 读取兜底）。复用现有 `_request`/`_get_task`/`_board`/`MetadataStore`。
- 上游 REST：`GET /board`、`GET /tasks/{id}`（events/attachments/runs）、`PATCH /tasks/{id}`（done/triage）、`POST /tasks/{id}/comments`；`review_requested` 事件给 review 时长。
- `workbench-ui/src/verify/VerifyPage.tsx` — 新增：全屏验收、A/R/S/Esc、理由输入、队列前进、统计、倒计时。
- `workbench-ui/src/styles/verify.css` — 顶栏 1/N、左产出物/右验收标准、底部键位、明暗 token。
- `workbench-ui/src/App.tsx` — 右上角 review 计数 + 全屏打开状态（独立版）。
- `workbench-ui/src/todo/TodoPage.tsx` — 流水线「待收」计数/行可点击进入验收（通过回调）。
- `workbench-ui/src/api/workbench.ts` — `getInbox()`、`verifyTask(id, action, reason?)` + 错误码。
- `workbench-app/tests/test_verify.py` — 新增后端测试；`workbench-ui/scripts/` 探针扩展验收流程。
- 会话入口：2.3 在验收页预留位置，本故事不放死按钮。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- inbox/verify 端点、review_age、48h sweeper（后台 + 读取兜底）、理由校验 -- 验收后端
- [x] `workbench-ui/src/api/workbench.ts` -- `getInbox` / `verifyTask` + 错误码 -- 前端调用
- [x] `workbench-ui/src/verify/VerifyPage.tsx` -- 全屏验收、A/R/S/Esc、理由必填、统计、倒计时 -- 验收 UI
- [x] `workbench-ui/src/styles/verify.css` -- 布局与明暗 token -- 视觉
- [x] `workbench-ui/src/App.tsx` -- review 计数与全屏入口 -- 入口
- [x] `workbench-ui/src/todo/TodoPage.tsx` -- 待收行/计数进入验收 -- 入口
- [x] `workbench-app/tests/test_verify.py` -- accept/reject/skip/48h/理由/409/502 -- 回归
- [x] `workbench-ui/scripts/` 探针 -- 验收流程与键盘 -- UI 回归
- [x] `workbench-ui` -- `npm run build` 并分发 -- 双宿主
- [x] `workbench-app/server.py` -- 重启 + `/healthz` -- 不破坏现有

**Acceptance Criteria:**
- Given 有 review 卡, when 打开验收, then 一张一屏显示验收标准/产出物/运行信息，计数 1/N
- Given review 卡, when 按 A, then 卡进 done，队列前进；刷新后不再出现
- Given review 卡, when 按 R 不填理由, then 不提交；填理由后写 comment 并回 triage
- Given review 卡, when 按 S, then 状态不变并前进；本批统计含 skipped
- Given review 超 48h, when sweeper/inbox 运行, then 自动 done + comment「48h 自动收」，只收一次
- Given 队列清空, when 最后一张处理完, then 显示 accepted/rejected/skipped 统计并退出
- Given 非 review 卡, when verify, then 409 `not_in_review`
- Given 上游/元数据失败, when verify, then 502/503，卡留在队列
- Given 插件宿主, when 打开, then 只有看板，无验收入口
- Given 改动完成, when build + restart, then 双宿主产物一致，`/healthz` → `{"ok":true}`

## Implementation Notes

- 2026-11-05 hardening: `GET /api/workbench/inbox` is a pure read; the 48h fallback sweep is `POST /api/workbench/reviews/sweep` (same-origin/JSON guarded), with the 5-min daemon sweeper kept. `GET /api/workbench/inbox/count` serves the header without per-card detail reads.
- Inbox detail reads run in parallel under one overall deadline; a failed detail is skipped and reported in `failed_ids` instead of blanking the queue. `/board` review ids use a 1s cache + in-flight lock (`WORKBENCH_INBOX_CACHE_SECONDS`).
- verify: reject reason capped at 2000 chars (`reason_too_long`); same-round duplicate workbench comments are deduped; every PATCH outcome re-reads the card and returns 502 when it is still `review`, with `comment_written` on every path.
- 48h policy: newest `review_requested` resets the clock; TTL/sweep/backoff are env-overridable (invalid values fall back; `WORKBENCH_REVIEW_SWEEP_SECONDS<=0` disables). Auto-collect PATCHes `done` while preserving the worker summary (audit-summary fallback only when upstream refuses a summary-less completion), writes the round-scoped「48h 自动收（review:<id>）」record only after the landing, and backs off on failure (`auto_collect_due` / `auto_collect_error` on the item).
- Verify UI: 3s auto-exit after batch stats, 5s queue sync (merge/refresh/re-sort, skipped cards never re-added), Esc exits even from the reason panel, key repeat ignored, focus trap/restore, generic TTL countdown from each item's received time, `result`/image attachments/run error+summary shown, and a notice when the requested `initialTaskId` is no longer in the queue.

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1/EC9 | 队列清空未自动退出，与 spec 矛盾 | medium | patch | 统计页只有「返回待办」；改为展示后自动退出，Esc 仍可用 |
| BH2 | inbox 两个 5s 轮询 + O(N) 详情扇出 | high | patch | App 只取 count；详情并行 + 总 deadline + 缓存/防重入 |
| BH3 | 单卡详情失败拖垮整个 inbox | medium | patch | 跳过失败卡 + `failed_ids`，不再整队列 502 |
| BH4/EC4 | `comment_written` 未回传，重试重复写理由 | medium | patch | 全路径回传；同理由在最新 review 后已存在则跳过 |
| BH5/EC6 | 48h 自动收失败无终态/退避 | medium | patch | PATCH 落 done 后才记录；失败标记 + 退避，inbox 可见 |
| BH6 | 自动收覆盖 worker summary | medium | patch | 省略 summary 或使用独立审计文案，保留 worker 自述 |
| BH7 | 自动收幂等按卡不按 review 轮 | medium | patch | 记录带最新 `review_requested` 时间/id |
| BH8 | 倒计时硬编码 48h | low | patch | 用后端 `review_ttl_seconds` |
| BH9/VG8/EC3 | 轮询新卡年龄锚定页面加载 | medium | patch | 每卡 receivedAt + 每次 inbox 刷新年龄 |
| BH10 | syncQueue 破坏最早优先、字段陈旧 | medium | patch | 按 id 合并、刷新字段、按 review 时间重排 |
| BH11 | fallbackCode 409 全局映射 not_in_review | low | patch | 按调用上下文区分 |
| BH12 | 键重复会连续验收 | medium | patch | 忽略 `event.repeat`，动作 pending 锁 |
| BH13 | 无焦点管理/焦点环 | medium | patch | 初始焦点、Tab 陷阱、归还焦点、focus-visible |
| BH14 | run.error 取了不显示 | low | patch | 展示 run.error/summary |
| BH15/VG1-7 | 测试缺口（两次 review、扫描失败、去重、200 未落、409 UI、skip 轮询、无事件排序） | medium | patch | 补后端与探针用例 |
| BH16 | 探针只覆盖 happy path | medium | patch | 补图片附件/result/空验收/外部处理/键盘/自动退出等 |
| BH17 | README 未记录 inbox/verify/count/sweep | low | patch | 补端点、错误码、TTL、env |
| BH18 | sprint-status/任务清单未反映 2.1 | medium | patch | 收尾时更新 sprint-status 与任务勾选 |
| BH19 | GET inbox 触发写操作、无 guard | high | patch | inbox 纯读；扫描改 guarded POST `/reviews/sweep` |
| BH20 | onRetry 死代码、initialTask 缺失无提示 | low | patch | 接线或删除；缺失卡给提示 |
| BH21/EC8 | 理由无长度上限；TTL 不可覆盖；sweep 间隔未 clamp | medium | patch | 理由 2000；`WORKBENCH_REVIEW_TTL_SECONDS`；间隔 finite/正数 |
| BH22/EC5 | finish 后停止轮询，统计不完整 | medium | patch | finish 期间继续轮询/新卡提示 |
| EC1 | PATCH 409 仍 review 被映射 not_in_review | medium | patch | 非 200 后重读状态；仍 review → 502 |
| EC2 | not_in_review 加入 handledRef 后不再回队 | medium | patch | 不加入；仍 review 的卡下次轮询可回 |
| EC7 | 上一张的 actionError 带到下一张 | low | patch | 切卡时清空 |
| BH23 | 两个探针重复 CDP harness | low | defer | 抽共享 `scripts/lib/` 记 deferred |

**结论：** 无 bad_spec/intent_gap；全部定向 patch（唯一 defer 是探针 harness 抽取）。补丁后重跑 18+38+29 后端测试、20+115 探针、构建与现场检查。

## Design Notes

- reject 落 `triage` 依据上游 `_drag_to`：`triage` 分支跳过 review 的 reopen，直接写状态；理由 comment 先写，失败则不改状态。
- 48h 时长取 `review_requested` 事件的 `created_at`；没有该事件的 review 卡不自动收（不猜）；后台 5 分钟扫 + inbox 读取兜底，保证没人开也能收。
- 产出物用 `result`/`latest_summary` 文本 + 附件（链接/图片内联），不引入 PDF/iframe 预览；成本（`metadata.cost`）与自述三行属后续，P2 只显示运行 profile/时长/outcome。
- 验收队列只含 review，按最早 `review_requested` 排序；P0 blocked 聚合在 2.2 扩展 inbox。
- 验收页不进入插件宿主；独立版路由/状态在 App 层，不新增浏览器路由依赖。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck && npm run build`
- `python3 workbench-app/tests/test_verify.py` -- 全绿
- `python3 workbench-app/tests/test_dispatch.py` / `test_todo.py` -- 回归
- 隔离实例：seed review 卡（含旧 review 事件）、inbox、A/R/S、48h 自动收
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`

**Manual checks:**
- 8787：右上角计数进入全屏，A/R/S/Esc、理由必填、清空统计；明暗主题；插件宿主只有看板