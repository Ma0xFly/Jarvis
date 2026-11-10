# Epic 4 Context: 触发层（P4）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让工作台从「人手建卡」走向「事件自己进活」：外部事件先落候选区并带原文锚点，人一键转卡；早报、GitHub、网站巡检、告警四条配方无人值守运行，有料产出、无料静默；子卡链超过 8 层被拒并留下原因；每张卡的花费可查、同类任务可按路由规则名比较。CAP-9/10/11 共享 `workbench.db`、worker 契约与任务详情/聚合视图，因此作为一个 Epic 交付。

## Stories

- Story 4.1: 事件入口与候选区
- Story 4.2: 四条配方与无人值守触发
- Story 4.3: 子卡熔断（8 层上限）
- Story 4.4: 成本记录与规则聚合

## Requirements & Constraints

- **事件入口**：统一接收 `source/type/object/summary/link/confidence/idempotency_key`；同 key 重复或并发投递只产生一条候选并返回 `created:false`；新事件 5 分钟内可见；校验失败 400、存储不可用 503，不 500；非 JSON/超大 body 返回 415/413。
- **候选不是卡**：只存工作台库，不进入待办三区、不写上游；活动列表只显示达阈值候选，低置信度仅进当天日报、不发通知；候选必须能点回/复制原始来源。
- **一键转卡**：复用工作台建卡路径，转出的卡验收为空、直接派发被契约闸门拒绝；重复转卡返回 409 且不建第二张卡。
- **四条配方**：morning-brief（08:00 写 `briefs/YYYY-MM-DD.md`）、github-issues（约 5 分钟轮询产候选）、website-watch（连续异常建卡、恢复给原卡加 comment 且不自动改状态）、ops-alerts（webhook 建卡，要重启则 blocked/`needs_input` 并留 `ask` 评论块）。四套各有配置、幂等与静默路径。
- **静默与失败**：无料/健康/无新事件时只输出 `[SILENT]`、退出 0，不建卡/候选/文件/通知；鉴权、网络或上游失败时退出码非 0、stderr 可见、不建重复卡。事件不能指定 `assignee`/`cwd`，profile 由配方配置决定，未知 profile 返回 400；secret 只读环境变量，缺失/错误返回 401，不入卡/skill/日志。
- **子卡熔断**：根卡 `hops=1`（缺失按 1），子卡为 parent+1；`hops > 8` 拒绝建卡并在父卡留 `hops_limit` comment；block/unblock/review/reject 均不重置，只有新链重新计数；详情可见层级；四类 worker 同一契约。
- **成本口径**：单卡 `cost={minutes,tokens,usd,by_rule,sources,degraded}`；分钟来自 `task_runs` 起止累加；token/费用来自 magpie `usage.jsonl`（仅覆盖 hermes）或适配器上报；`usd` 以 `credits` 为代理并标 `usd_source="magpie_credits"`；缺失为 `null`，显示「未上报」，不参与平均与总费用分母；按 `routing.json` 规则名聚合，缺规则名归 `unknown`。
- **降级与边界**：`cost`/`hops`/`route_rule`/`executor_cost`、`usage.jsonl` 缺失或畸形时如实降级，不 500；上游零改动、不直接写 `kanban.db`、不建上游表；新端点只加在 `/api/workbench/*`；5 秒轮询，不引入 WebSocket；重启后 `/healthz` 仍 200。

## Technical Decisions

- `workbench.db` 新增 `candidates` 表（`idempotency_key` 唯一、状态、`task_id`、时间戳），启动时建表、无历史迁移；转卡时上游幂等键映射为 `candidate:<id>`，重试不建第二张卡。
- 新端点：`POST /ingest`、`GET /candidates?status=pending`、`GET /candidates/daily?date=`、`POST /candidates/{id}/convert`；日报端点供 morning-brief 读取，不单独发通知。阈值 `candidate_min_confidence` 默认 0.6，放 `defaults.json`，可用环境变量覆盖。
- `create_task` 增加可选 `assignee`（只允许 `routing.json` 中出现的 profile）与 `body`（≤4000）；转卡把完整 summary 与 link 写入卡 body，标题按既有上限截断，原文锚点不丢。
- `recipes.json` 是唯一配方配置；配方代码在项目根 `recipes/<name>/`，逻辑不写进 `server.py`，以 symlink 装进 profile `skills/`（外部文件，先备份）；建卡统一走 `/api/workbench/tasks`（`triage=true` → `scheduled`），再写 `contract.input/accept`。幂等键：github `github:<repo>:<number>:<updated_at>`、website `website:<url>:<signal>`、ops `ops:<fingerprint>`；website 同一异常只保留一张打开卡。
- 熔断强制点在 worker skill 契约 + `bin/subtask-create`（工作台只提供测试夹具与层级展示）；建卡前 GET 父卡并合并写回缺失 `hops`，写子卡 metadata 先 GET 合并、失败重试一次；被拒时 stdout 末行 JSON 含 `hops_limit`，退出码 2；同一 parent+title+body 幂等。
- worker 在最新 run metadata（`kanban_request_review`）写 `route_rule`/`executor`/`executor_cost`；`executor_cost` 取自 `bin/executor-run` 末行 JSON 的 `{tokens,usd}`，hermes 由 magpie 覆盖；`bin/route` 输出保留 `rule`/`rule_index`。magpie usage 按 run 时间窗过滤，可精确匹配则精确归属，否则标近似。
- `cost`/`cost_updated_at` 是 `workbench.db` 的服务端只读键：`GET /metadata` 返回，客户端 PATCH 带 `cost` 返回 400 `invalid_metadata`；单卡成本按需计算并做 30 秒内存缓存；summary 按规则名聚合（≤365 天），返回 `runs/tasks/minutes/tokens/usd/avg_*`，`unknown` 单独一行。

## UX & Interaction Patterns

- 候选区独立于待办三区；候选卡显示来源/类型/摘要/置信度/原文锚点/转卡按钮，低置信度不出现在活动列表。
- 任务详情显示「子卡层级 x/8」；第 9 层拒绝原因在评论时间线可见。
- 成本块显示分钟/token/费用/规则名；`null` 显示「未上报」；聚合视图显示各规则 total/avg；4.4 只做一个聚合入口。
- 5 秒轮询；明暗主题走 `theme.css` token，不硬编码色值。

## Cross-Story Dependencies

- 4.2 依赖 4.1 的 `/ingest`、`candidates` 表与 `create_task.body`，须先完成 4.1。
- 4.3 与 4.4 都改 `workbench-conventions` skill 与 `test_worker_contract.py`，按 4.3 → 4.4 顺序实施，彼此不并行；两者不依赖 4.1/4.2。
- 4.4 复用 Epic 3 的 `bin/route` 与 `bin/executor-run`；dsh/codex 的 `usd=null` 必须显示「未上报」。
- 四类 worker profile 的 skill/SOUL 属外部文件：修改前备份，不改上游内核；契约测试对四个 profile 参数化，profile 文件缺失时 fail 而非 skip。