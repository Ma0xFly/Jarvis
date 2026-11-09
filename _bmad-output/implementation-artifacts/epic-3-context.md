# Epic 3 Context: 变强（P3）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让验收与执行两条链各向前一步：验收页能读到可反驳的 AI 自述（交付 / 依据 / 没底），完成卡在收卡前必须至少登记一件可复用件，挂起的仓库卡能在指定时间自动回队，执行动作则收敛到统一的可插拔执行器契约。四项能力共享 `server.py`、验收页与 worker 的 review 契约，因此作为一个 Epic 交付；完成后系统开始积累可验证的能力，而不是只完成单张卡。

## Stories

- Story 3.1: 验收页自述三行
- Story 3.2: 固化登记与验收门禁
- Story 3.3: 定时唤醒
- Story 3.4: 执行器契约与 claude_code 适配器
- Story 3.5: 其余适配器与路由切换

## Requirements & Constraints

- 验收页读取最新 run 的 `metadata.self_report = {delivered, evidence, uncertain}`，排在样张与验收标准之后；`uncertain` 为空/缺失、结构畸形或旧卡无该字段时降级显示可见警示，不返回 500，也不阻断 A/R/S。
- accept 前校验最新 run 的 `metadata.solidified` 至少一条合法记录 `{type: routing|skill_patch|script, path, status: pending|verified}`；缺失、为空或全部非法返回 400 `missing_solidified`，卡留在 review；reject/skip 不受影响，48h 自动收仍是队列保底且不要求固化。
- 固化只做登记与审计：内容仍落 Hermes profile 的 `skills/` 或 `routing.json`；手动「标记已验证」写工作台库的 `solidified_verified` overlay（path + verified_at），不 PATCH 上游 run metadata，`verified` 不改变 accept 门禁，不新建记忆库。
- 仓库卡可设 `wake_at`（工作台 metadata，epoch 秒）；到期且有验收、无未完成 parent 时走现有 dispatch 进入 `ready`，有 parent 进 `todo`；缺验收留在 `scheduled` 并记录可见的 `wake_error`，退避后重试；手动提前唤醒不受退避限制；重复扫描不二次派发。
- 唤醒只处理带 `wake_at` 的卡，Hermes 原生 `scheduled` / `not_before` 卡不读不改；不做周期任务与时区选择（本地时间显示，存 epoch）。
- 统一执行器入口 `bin/executor-run` + `executors.json`：任务书只从 stdin 读（含输入/验收/禁区），stdout 最后一行是 JSON，至少含 `ok`、`executor`、`session_id`、`summary`、`cost`、`log_path`、`exit_code`；无 session id 时为 `null`，不伪造。
- 调用方只依赖五个退出码：0 成功、2 需要人、3 超时/超预算中断、4 执行器不可用（含缺二进制）、1 其它错误。
- 适配器只做参数与输出翻译：不 import Hermes 内部模块、不读写 `kanban.db`、不记录密钥、不放业务逻辑；P3 完整实现 `hermes`（进程内模式）与 `claude_code`，`dsh` / `codex` 注册并以 fake binary smoke 为必选回归，真实账号 smoke 可选，`--stream` 留作后续。
- `routing.json` 规则可指定 `executor`，事件与建卡方不能自行覆盖；无该字段保持现有默认；执行器不经过 magpie，费用由适配器自行上报；不做自动 fallback 链。
- 上游零改动；不直接写 `kanban.db`、不新建表/迁移；新端点只加在 `/api/workbench/*`；源码只改 `workbench-ui/` 与 `workbench-app/`，执行器新文件落在项目根的 `bin/`、`executors.json`、`routing.json`。
- `self_report` / `solidified` / `executor_session_id` 一律容忍缺失与畸形；服务重启后 `/healthz` 仍 200，不打断在跑任务。

## Technical Decisions

- `self_report`、`solidified`、`executor_session_id` 都落在**最新 run 的 `task_runs.metadata`**，由 worker 用 `kanban_request_review` 写入（不是 `tasks.metadata`）；工作台始终读最新 run，缺失返回 `null`。
- `solidified_verified` 存 `workbench.db` 的 `task_metadata`（`{path, verified_at}[]`），与上游 pending 记录合并展示；工作台不编辑 profile 文件。
- `wake_at` 与 Hermes 原生 `scheduled` / `not_before` 解耦；后台 `WakeScheduler` 默认每 30s 扫描（`WORKBENCH_WAKE_SWEEP_SECONDS`，0 关闭），只读带 `wake_at` 的 workbench 行；缺验收退避 300s、上游失败退避 60s，写 `wake_error` / `wake_next_try_at`（客户端不可写）。
- 端点：`POST /api/workbench/verify/{task_id}` 的 accept 加 `solidified` 门禁；新增 `POST /api/workbench/solidified/{task_id}/verify` 写 overlay；新增 `POST /api/workbench/wake/{task_id}`，卡不在仓库（triage/todo/scheduled）返回 409 `not_in_store`，无验收返回 400 `missing_acceptance`，有 parent 落 `todo`，否则走 dispatch 进 `ready`，成功后清空 `wake_at` / `wake_error` / `wake_next_try_at`。
- 执行器是 CLI 契约而非 Hermes 插件：`claude_code` 调 `claude -p --output-format json` 并透传 `--resume`；`dsh` 把 `--resume` 映射为 `--session-id`，从 NDJSON 首行解析 `session-<uuid>`；`hermes` 在 `executors.json` 标 `mode: in-process`，CLI 调用拒绝并返回 4。新增执行器只需适配器 + 注册 + routing 规则。
- 前端唯一源码仍是 `workbench-ui/src/`，构建分发到插件与独立版；新增 UI 复用现有 `VerifyPage` / `TodoPage` / `TaskDetail` / `workbench.ts`，不改变 A/R/S 键位与验收队列语义。

## UX & Interaction Patterns

- 验收页顺序固定「样张 → 验收标准 → 自述三行」；「交付 / 依据 / 没底」中「没底」单独强调；缺失态可见但不阻塞判断。
- 固化块逐条显示类型、路径、状态；缺失或全部非法时给出 accept 阻断原因；pending 可一键「标记已验证」。
- 仓库卡显示「将于 HH:MM 唤醒」与「提前唤醒」；唤醒失败（如缺验收）显示可见原因，清空唤醒时间后提示消失。
- 会话抽屉展示 `executor_session_id`；P3 先提供「续跑」入口的显示/复制，实际续跑通过执行器契约接通。
- 明暗双主题走 `theme.css` token，不硬编码色值。

## Cross-Story Dependencies

- 3.1 与 3.2 都改验收页与 `workbench.ts`，但 3.2 的 accept 门禁不依赖 3.1 的展示，可分别完成。
- 3.3 复用 Epic 1 的 dispatch 与 metadata 写端点，只改待办页/详情；不依赖 3.1/3.2。
- 3.4 是 3.5 的前置；3.5 复用 CLI、`executors.json` 注册表与适配器接口，并在 Epic 2 的会话抽屉上接续跑入口；3.1–3.3 不依赖执行器。
- 三个 profile 的 worker skill 必须在 `kanban_request_review` 中写 `self_report`、`solidified`、`executor_session_id`，并使用该收尾方式而非 `kanban_complete`；这是 3.1/3.2/3.5 的跨系统前置，用 fixture/文档验证。
- 3.5 切换 coder 到 `claude_code` 时需记录回滚 dsh 的步骤；执行器不可用不得伪装成成功。