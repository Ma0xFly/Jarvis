# Epic 2 Context: 闭环（P2）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

补上人机闭环的后半段：跑完的活能在全屏验收里逐张判断，会堵下游的卡主动叫人，任何一张卡都能跳回该轮执行会话。P1 已让「想法 → 仓库 → 拖进流水线 → 排队等 worker」跑通；P2 完成后，「派出去 → 跑完 → 推给你 → 你验收」这条链成立（收得掉、叫得应、追得回），不再停在 `review` 里靠翻日志处理。

## Stories

- Story 2.1: 全屏验收与验收动作
- Story 2.2: P0 通知与通知操作
- Story 2.3: 会话追溯入口

## Requirements & Constraints

- 验收是**全屏模式，不是 tab**：从右上角待处理计数或流水线「待收」行进入；一张一屏展示产出物（`result` / `latest_summary` + 可翻页附件）、验收标准原文与本轮运行信息。
- `A` 收 → `done`；`R` 不收（理由必填 1–2000 字，前后端都拦）→ 写 comment 后回 `triage`；`S` 跳过保留 `review`；`Esc` 退出。动作统一走 `POST /api/workbench/verify/{task_id}`。
- 队列清空自动退出并给出本批统计；`review` 超过 48h 按规则静默通过并留审计记录，避免队列自堵。
- P0 仅限会堵下游的卡：`blocked` 且有未完成子卡，或 `block_kind ∈ {needs_input, capability}`；`review` 且有未完成子卡也算。10 秒内桌面通知 + 右上角常驻/置顶，非 P0 不打扰；同一事件 30 分钟只升级一次；P1 攒 3 条或 30 分钟，P2 每日 08:00 一行。
- 通知内可直接处理：`review` 卡收/不收，P0/P1 可 10 分钟后再提醒；非 `review` 只给提醒与打开看板，P2 的 `review` 只给收/不收。`review` 用「还有 Xh Ym 自动收」倒计时替代未读；通知中心验收槽容量 3，同一次 decompose 的兄弟卡成束，槽满排队。
- 验收 / 通知 / 会话均 5s 轮询，不引入 WebSocket；首屏 ≤1s。桌面通知只走 `notify-send`（命令可注入），手机通道本期不做（接口预留）。
- 会话追溯：验收/已完成卡可「打开执行会话」，优先读 `task.metadata.worker_session_id`；执行器卡读 `executor_session_id`，展示该轮完整对话并可用续跑参数接上。
- 上游零改动；不直接写 `kanban.db`；`state.db` 只读反查；新端点只加在 `/api/workbench/*`；源码只改 `workbench-ui/` 与 `workbench-app/`；服务重启后 `/healthz` 仍 200，不打断在跑任务。
- 2.3 必须容忍 `executor_session_id` 缺失（属 Epic 3 CAP-14），P2 只完整交付 `worker_session_id` 路径；验收只改上游状态，不写 `metadata.solidified`（Epic 3 CAP-7）。

## Technical Decisions

- 后端是 FastAPI 薄壳，只经公开 REST 读写上游（`workbench.db` 只存工作台 metadata）；不 import `kanban_db`，不改 `plugin_api.py`；插件宿主没有 `/api/workbench/*`，验收/通知/会话界面只在独立版 8787 提供。
- `GET /api/workbench/inbox` 纯读、只含 `review` 卡，按最早 `review_requested` 排序；`POST /reviews/sweep` 与后台 5 分钟 sweeper 负责 48h 扫描。
- `POST /verify/{task_id}`：`accept` → `done`；`reject` → 先写 comment 再 `triage`（comment 失败不改状态）；`skip` 不改状态。PATCH 后重读：仍 `review` → 502，已离开 → 409；可带本轮 review 时间返回 409 `stale_review`。
- 48h 自动收按最近一次 `review_requested` 计时，无该事件的卡永不自动收；落 `done` 后写 `author=workbench` 审计 comment，幂等且不覆盖 worker summary。
- 通知引擎后台 5s 轮询 + guarded sweep，`GET /api/workbench/notifications` 纯读快照，`POST /notifications/{event_id}/action` 执行动作；分类只读 `/board` + `/tasks/{id}`，动作复用 verify，不新增状态写路径。桌面发送失败降级为 `desktop.last_error`。
- 会话数据源：`GET /tasks/{id}` 已返回 `metadata.worker_session_id`、comments/events/runs；完整对话从 `state.db` 只读反查（sessions / messages，含 FTS），`auto_prune: false`，保留期只对手动 prune 生效。`kanban/logs/<task>.log` 失败时可能只有 1 KB，不可作为记录档。
- 会话模型：一次 attempt 一条会话，`request_id = kanban:[board, task_id, run_id]`；dsh 是第二层嵌套（coder → dsh `session-<uuid>`）。续跑参数与适配器属 Epic 3（claude_code `--resume`、dsh `--session-id`）。

## UX & Interaction Patterns

- 验收布局：顶栏「验收 1/3 + 进度 + 退出（Esc）」；左侧产出物样张（可翻页），右侧验收标准 + 运行信息（profile/时长/outcome；AI 自述三行属 Epic 3），底部 `A/R/S`。
- 键盘优先；拒绝理由必填；忽略按键重复；焦点管理与清空后自动退出（`Esc` 可立即退出）。
- 通知中心：P0 置顶、P1 面板、P2 日报、`review` 倒计时、槽位与排队摘要；动作或采集失败要可见。
- 验收槽只约束通知中心的展示与推送，不限制 2.1 全屏验收队列的处理能力。
- 明暗双主题全走 `theme.css` token；插件宿主只渲染看板并提示使用独立版。

## Cross-Story Dependencies

- 2.1 先于 2.2：先有验收模式，通知才有落点；通知内的收/不收直接调用 2.1 的 verify。
- 2.2 未扩展 `/inbox` 聚合 blocked 卡，而是使用独立 notification 快照与端点；通知动作以 `expected_review_at` 防范陈旧通知处理到下一轮 review。
- 2.3 的入口挂在 2.1 的验收页与已完成卡上，需接入现有 `VerifyPage` / App 状态；会话内容只读，不改卡状态。
- 2.3 的执行器续跑依赖 Epic 3 的 CAP-14；本期只能完整交付 Hermes `worker_session_id` 路径。
- Epic 1 提供 `review` 卡与 `metadata.contract.accept`；worker 必须以 `kanban_request_review` 收尾（`kanban_complete` 会绕过验收）。验收通过只落 `done`，固化物登记属 Epic 3 CAP-7。