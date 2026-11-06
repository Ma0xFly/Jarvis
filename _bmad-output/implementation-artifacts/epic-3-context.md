# Epic 3 Context: 变强（P3）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让工作台从“能派活、能收活”继续变强：验收页能显示可反驳的 AI 自述，完成卡能沉淀出可复用件，挂起的卡能到点自己回队，并把执行交给可插拔的 CLI 执行器。四项能力共享验收页、`server.py`、worker review 契约与 metadata，因此作为一个 Epic 交付；完成后系统开始积累可验证的能力，而不是只完成单张卡。

## Stories

- Story 3.1: 验收页自述三行
- Story 3.2: 固化登记与验收门禁
- Story 3.3: 定时唤醒
- Story 3.4: 执行器契约与 claude_code 适配器
- Story 3.5: 其余适配器与路由切换

## Requirements & Constraints

- 验收页在样张之后显示 AI 自述「交付 / 依据 / 没底」；「没底」缺失或为空要有可见警示，旧卡缺失时降级展示，不影响 A/R/S。
- 验收 accept 前检查最新 run 的固化物：至少一条合法 `{type, path, status}`；缺失返回 400 `missing_solidified`，reject/skip 不受影响；48h 自动收继续作为队列保底，不要求固化。
- 固化内容仍写 Hermes profile `skills/` / `routing.json`；工作台不新建记忆库。手动「标记已验证」写 workbench.db 的 `solidified_verified` overlay，不 PATCH 上游 run metadata。
- 仓库卡可设 `wake_at`（workbench metadata，epoch seconds）；到点有验收且无未完成 parent 走 dispatch 进 `ready`，有 parent 进 `todo`；缺验收留在仓库并给可见错误；原生 `scheduled` 卡没有 `wake_at` 时不受影响。
- 新增 `bin/executor-run`（stdin 任务书 → stdout 末行 JSON + 五个退出码 0/1/2/3/4）与 `executors.json`；适配器只做参数/输出翻译，不读 `kanban.db`、不写业务逻辑、不记录密钥。
- 执行器回写 `executor_session_id`；`routing.json` 的 `executor` 决定执行器；`hermes` 标记为 `mode: in-process`，CLI 调用返回 4。
- 上游零改动；新端点只加在 `/api/workbench/*`；不新建数据库表；`solidified` / `self_report` / `executor_session_id` 落 `task_runs.metadata`（最新 run），不是 `tasks.metadata`。
- 现有 `server.py` / `TodoPage` / `VerifyPage` / `TaskDetail` / `workbench.ts` 的行为和探针选择器要兼容；新增逻辑必须可被后端测试与 headless probe 覆盖。

## Technical Decisions

- `kanban_request_review` 的 `metadata` 由 Hermes 写入 `task_runs.metadata`，工作台读最新 run；`self_report` 与 `solidified` 都按此路径。
- `solidified` 的 verified 是工作台判断，存 `workbench.db task_metadata.solidified_verified`（path + verified_at），与上游 pending 合并展示；不新增记忆系统。
- `wake_at` 是 workbench 自有 metadata，与 Hermes 原生 `scheduled`/`not_before` 解耦；仓库卡继续停 `scheduled`。
- 执行器契约是 CLI，不是 Hermes 插件；`bin/` 与 `executors.json` 在 `~/Projects/Jarvis`，适配器 ~60 行。
- 验收页现有 `VerifyPage` 是全屏 overlay；新增自述/固化/唤醒 UI 不改变 A/R/S 键位与队列语义。
- 预算/验收/派发闸门沿用现有 workbench 方案；不改 `plugin_api.py`、不写 `kanban.db`。

## UX & Interaction Patterns

- 验收页顺序固定「样张 → 自述三行」；「没底」单独强调；缺失态可见但不阻塞判断。
- 固化块显示类型/路径/状态；缺失时给出阻断原因；pending 可手动标记 verified。
- 仓库卡显示「将于 HH:MM 唤醒」与「提前唤醒」；缺验收时显示唤醒失败原因。
- 会话抽屉显示 `executor_session_id`，P3 提供「续跑」入口的显示/复制，实际续跑接执行器契约。

## Cross-Story Dependencies

- 3.1 与 3.2 都改 `VerifyPage` / `workbench.ts`，可独立完成但共享组件；3.2 不依赖 3.1 的 UI。
- 3.3 用现有 `TodoPage` / `TaskDetail` / metadata 写端点；不依赖 3.1/3.2。
- 3.5 依赖 3.4 的 CLI/registry/adapter 契约；3.1–3.3 不依赖执行器。
- worker skill 契约（自述、solidified、executor_session_id）是 3.1/3.2/3.5 的跨系统依赖，故事内以 fixture/文档验证。