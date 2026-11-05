# Epic 2 Context: 闭环（P2）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

补上人机闭环的后半段：跑完的活能在全屏验收里逐张判断，P0 卡会主动叫人，任何一张卡都能跳回执行会话。P1 已让「想法 → 仓库 → 拖进流水线 → 排队等 worker」跑通；P2 完成后，派出去、跑完、推给你、你验收这条链成立（收得掉、叫得应、追得回），不再停在 `review` 里靠翻日志处理。

## Stories

- Story 2.1: 全屏验收与验收动作
- Story 2.2: P0 通知与通知操作
- Story 2.3: 会话追溯入口

## Requirements & Constraints

- 验收是**全屏模式，不是 tab**：从右上角「待你处理 N」计数或流水线「待收」行进入；一张一屏，展示产出物样张（可翻页）、验收标准原文、本轮运行信息。
- 键位 `A` 收 / `R` 不收 / `S` 跳过 / `Esc` 退出。`A` → `done`；`R` 必须填写理由（前端与后端都拦），写理由 comment 后回 `triage`；`S` 保持 `review`。动作统一走 `POST /api/workbench/verify/{task_id}`。
- 队列清空自动退出并给出本批统计；`review` 超过 48h 未处理按既定规则静默通过并留记录，避免队列自堵。
- P0 仅限会堵下游的卡（有子卡或有人等）：10 秒内桌面系统通知 + 右上角常驻/置顶；非 P0 不打扰。同一事件 30 分钟内只升级一次；P1 攒批（3 条或 30 分钟）、P2 日报。
- 通知内可直接「收 / 不收 / 10 分钟后再提醒」；`review` 卡用「还有 42h 自动收」倒计时代替未读；验收槽固定容量 3，同一次 decompose 的兄弟卡成束。
- 验收 / 通知 / 会话均 5s 轮询，不引入 WebSocket；首屏 ≤1s。通知只走桌面系统通知，手机通道本期不做（接口预留）。
- 会话追溯：验收/已完成卡可「打开执行会话」，优先读 `task.metadata.worker_session_id`；执行器卡读 `executor_session_id`，展示该轮完整对话，并可用执行器续跑参数接上。
- 上游零改动；不直接写 `kanban.db`；`state.db` 只读反查；新端点只加在 `/api/workbench/*`。
- 本期验收只改上游状态；完成即固化（`metadata.solidified`）属后续 Epic，不在 2.1 范围。

## Technical Decisions

- 后端仍是 FastAPI 薄壳：`GET /api/workbench/inbox` 聚合 `review` 卡 + P0 `blocked` 卡；`POST /api/workbench/verify/{task_id}` 接受 `accept` / `reject` / `skip`（`reject` 带必填 `reason`）。只经公开 REST 改上游状态，不 import `kanban_db`、不改 `plugin_api.py`。
- 验收动作映射：`accept` = `PATCH status=done`；`reject` = 先写理由 comment 再 `PATCH status=triage`；`skip` = 不改状态。拒绝理由在 API 与 UI 两侧都必须非空。
- 会话追溯数据源：`GET /tasks/{id}` 已返回 `metadata.worker_session_id` 及 comments / events / runs；Hermes 自动写入 `worker_session_id`。完整对话从 `state.db` 只读反查（sessions / messages，含 FTS），`auto_prune: false`，保留期只对手动 prune 生效。`kanban/logs/<task>.log` 失败时可能只有 1 KB，不可作为记录档。
- 会话模型：一次 attempt 一条会话，`request_id = kanban:[board, task_id, run_id]`；dsh 是第二层嵌套（coder 会话 → dsh `session-<uuid>`）。续跑参数由执行器契约提供，本期不实现执行器适配器。
- `executor_session_id` 当前没有任何写入/读取路径，属 Epic 3 执行器契约（CAP-14）；2.3 必须容忍该字段缺失，先把 `worker_session_id` 路径做通。
- 通知走 `notify-send` / 桌面环境系统通知；同一事件 30 分钟内只升级一次。
- 插件宿主没有 `/api/workbench/*`，只能渲染看板 tab；依赖新端点的验收、通知、会话界面在独立版 8787 提供。
- 前端唯一源码在 `workbench-ui/src/`，改完 `npm run build` 分发；5s 轮询，不引入 WebSocket。

## UX & Interaction Patterns

- 全屏布局：顶栏「验收 1/3 + 进度 + 退出」；左侧产出物样张（可翻页），右侧验收标准 + AI 自述/运行信息，底部 `A/R/S`。
- 键盘优先（`A/R/S/Esc`）；拒绝理由是必填输入，不填不能提交。
- 通知内可直接操作；P0 常驻/置顶；`review` 卡显示「还有 42h 自动收」倒计时。
- 验收槽固定容量 3；同一次 decompose 的兄弟卡成束；清空后给本批统计。

## Cross-Story Dependencies

- 2.1 先于 2.2：先有验收模式，通知才有落点；通知内的收/不收/稍后提醒直接调用 2.1 的 verify 端点。
- 2.1 与 2.2 共享 inbox 聚合（review + P0 blocked）；2.3 的入口挂在验收/已完成卡上，需与 2.1 的界面一起落地。
- 2.3 的执行器续跑依赖 Epic 3 的 CAP-14（`executor_session_id` 回写与 dsh 续跑参数）；本期只能完整交付 Hermes `worker_session_id` 路径。
- Epic 1 提供 `review` 卡与 `metadata.contract.accept`；worker 必须以 `kanban_request_review` 收尾，`kanban_complete` 会绕过验收。验收通过只落 `done`，固化物登记属 Epic 3 的 CAP-7。