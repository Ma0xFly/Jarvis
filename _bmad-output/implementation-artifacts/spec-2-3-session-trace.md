---
title: '2.3 会话追溯入口'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: 'e7cfcff3b97c30d4feb1cbeba4fdf29d42e06488'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 验收/已完成卡看不到该轮执行会话；`worker_session_id` 没有入口，判断与排障只能翻 `kanban/logs` 或 `state.db`。

**Approach:** 新增 `GET /api/workbench/session/{task_id}`：从最新 run 的 `metadata.worker_session_id`（回退 `tasks.session_id`）解析会话，只读 `~/.hermes/state.db` 取 session + 最近 N 条 messages；前端 `SessionViewer` 抽屉从验收页与看板已完成卡打开，展示该轮对话；执行器续跑留 Epic 3。

## Boundaries & Constraints

**决策（2026-11-05，人已确认）：** 会话查看器是应用内只读抽屉，不跳 9119；只取最近 200 条消息，tool calls 折叠，超出提示 truncated。

**Always:**
- 只读 `~/.hermes/state.db`（`WORKBENCH_STATE_DB` 可覆盖），不写；上游只读 `GET /tasks/{id}`。
- 会话 id 解析顺序：最新 run `metadata.worker_session_id` → `tasks.session_id`；都无 → 404 `no_session`；state.db 无此 session → 404 `session_missing`。
- 消息按时间正序，最多 200 条 + `truncated`；tool_calls 折叠为名称/摘要；不返回 system prompt。
- 独立版 8787；插件宿主无会话入口。
- 入口：验收页 session slot + 看板已完成卡；Esc/关闭回原处，焦点管理。
- run metadata 里若有 `executor_session_id`，只展示并注明续跑属 Epic 3；不写会话、不启动。

**Never:**
- 不做执行器续跑/写 `executor_session_id`；不打开 9119 dashboard（Q1 决定 in-app）。
- 不 import `kanban_db`；不写 `kanban.db`/`state.db`；不引入依赖。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开会话 | 任务有 worker_session_id | 抽屉显示 session 元数据 + 最近 200 条消息 | N/A |
| 无会话 id | run/task 都无 | 404 `no_session` + 提示 | 不打开空查看器 |
| state.db 无此会话 | id 存在但 sessions 无行 | 404 `session_missing` | N/A |
| state.db 不可用/锁 | 文件缺失/只读打不开 | 503 `session_unavailable` | 不阻塞验收 |
| 任务不存在 | 非法 id | 404 | N/A |
| 超长会话 | >200 条 | 只返回最近 200 + `truncated`，UI 提示 | N/A |
| tool calls | 消息带 tool_calls | 折叠为名称/摘要，不渲染原始大 JSON | N/A |
| 执行器 id | run metadata 有 executor_session_id | 展示为次要 id + 「续跑属 Epic 3」 | N/A |
| 插件宿主 | 9119 | 无会话入口 | N/A |

</frozen-after-approval>

## Code Map

- `workbench-app/server.py` — `WorkbenchService.session_transcript(task_id)`：`GET /tasks/{id}` 取 runs，按最新倒序找 `metadata.worker_session_id`，回退 `task.session_id`；只读打开 state.db（URI `mode=ro`、busy timeout），读 `sessions` 行 + `messages` 最近 200 条；新增 `GET /api/workbench/session/{task_id}`。`WORKBENCH_STATE_DB`、`WORKBENCH_SESSION_MESSAGE_LIMIT` 覆盖。
- `~/.hermes/state.db` 实际路径（不是 `~/.hermes/hermes-agent/state.db`）；表：`sessions(id, source, title, model, started_at, ended_at, message_count, input_tokens, output_tokens, profile_name, …)`、`messages(session_id, role, content, tool_calls, tool_name, timestamp, …)`；按 `session_id` 查询并 LIMIT。
- 上游 `GET /tasks/{id}` 的 `runs[].metadata.worker_session_id` 与 `task.session_id`；`executor_session_id` 若存在只读展示。
- `workbench-ui/src/session/SessionViewer.tsx` — 新增：抽屉、会话元数据、消息列表、tool calls 折叠、truncated 提示、Esc/关闭、焦点。
- `workbench-ui/src/styles/session.css` — 抽屉与消息样式、明暗 token。
- `workbench-ui/src/App.tsx` — 会话抽屉状态与挂载；接收 VerifyPage/Board 的打开回调。
- `workbench-ui/src/verify/VerifyPage.tsx` — 2.1 预留的 session slot 变成「打开执行会话」。
- `workbench-ui/src/board/Board.tsx` — 已完成卡加「会话」入口（不改变拖拽）。
- `workbench-ui/src/api/workbench.ts` — `getSession(taskId)` + session 类型/错误码。
- `workbench-app/tests/test_session.py` — 新增：解析顺序、404/503、消息 limit、只读、executor 展示；`session-probe` 覆盖抽屉与两个入口。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- session 解析 + state.db 只读 + `GET /api/workbench/session/{task_id}` -- 后端
- [x] `workbench-ui/src/api/workbench.ts` -- `getSession` + 错误码 -- 前端调用
- [x] `workbench-ui/src/session/SessionViewer.tsx` -- 抽屉、消息、tool calls、truncated、焦点 -- 查看器
- [x] `workbench-ui/src/styles/session.css` -- 抽屉/消息/明暗 token -- 视觉
- [x] `workbench-ui/src/App.tsx` -- 抽屉状态与挂载 -- 入口
- [x] `workbench-ui/src/verify/VerifyPage.tsx` -- session slot 打开 -- 验收入口
- [x] `workbench-ui/src/board/Board.tsx` -- 已完成卡会话入口 -- 看板入口
- [x] `workbench-app/tests/test_session.py` + 探针 -- 回归
- [x] `workbench-ui` -- `npm run build` 分发 -- 双宿主
- [x] `workbench-app/server.py` -- 重启 + `/healthz` -- 不破坏现有

**Acceptance Criteria:**
- Given 任务有 worker_session_id, when 从验收页或已完成卡打开会话, then 显示 session 元数据 + 最近 200 条消息
- Given run/task 无会话 id, when 打开, then 404 `no_session` 提示，不显示空查看器
- Given state.db 无该 session, when 打开, then 404 `session_missing`
- Given 超长会话, when 打开, then 只返回最近 200 条并提示 truncated
- Given 消息带 tool_calls, when 展示, then 折叠为名称/摘要
- Given executor_session_id 存在, when 展示, then 出现次要 id + 「续跑属 Epic 3」
- Given 打开会话, when 检查, then 不写 kanban.db/state.db，上游状态不变
- Given 插件宿主, when 打开, then 无会话入口
- Given 改动完成, when build + restart, then 双宿主产物一致，`/healthz` → `{"ok":true}`

## Implementation Notes

Hardening pass (2026-11-05, coordinator):

- **Read semantics match Hermes's reader.** The message window is ordered by
  `messages.id` (never `timestamp`; clocks regress) and returned ascending by
  id. When the columns exist (defensive `PRAGMA table_info`): liveness is
  `(active = 1 OR compacted = 1)`, `display_kind = 'hidden'` rows are skipped,
  `display_identity` is deduped keeping the highest id, and `_compressed_summary`
  rows are projected like Hermes's string branch (merged carrier → prior tail;
  legacy marker → live ask after it; pure handoff → dropped). `role='system'`
  and user rows starting `[System:` / `[CONTEXT COMPACTION — REFERENCE ONLY]`
  are skipped; `sessions.system_prompt` is never selected.
- **Bounds.** Each message content is capped at ~20KB with a marker and
  `content_chars` / `content_truncated`; the response content array is capped at
  ~1MB by dropping oldest messages. The SQLite read carries a best-effort
  progress handler with a 4s deadline; the connection closes in `finally` on
  every path (including 404/503).
- **Counts.** `returned_count` / `total_count` come from the filtered display
  window (SQL deduped count cross-checked with the scan); `truncated` is
  `total_count > returned_count`, independent of a stale/inflated
  `sessions.message_count` (exposed unchanged as metadata). UI shows
  `显示 X / 共 Y 条消息` plus a truncated banner.
- **Resolution.** "最新一轮": runs newest-first by `started_at`/id; the first
  `metadata.worker_session_id` present in `sessions` wins, else the next older
  run, else `tasks.session_id` (labelled "发起会话"). `executor_session_id` is
  returned only from the run that supplied the resolved session.
- **Tool calls.** Only the tool name and the literal `参数已折叠` are returned;
  arguments are never parsed into the response, so secrets in the prefix or
  after 240 chars cannot leak.
- **Transport.** Transcript responses carry `Cache-Control: no-store`. README
  documents the endpoint, env vars, and the sensitivity / loopback-only note.

Deferred work (not in this story):

- Compression-lineage ancestor merging via `sessions.parent_session_id`.
- Hermes `display_kind` variants beyond `hidden` + `display_identity` dedupe.
- A run selector for browsing older attempts (resolution always starts at the
  newest resolved round).
- Shared CDP harness for the four UI probes (each still carries its own copy).

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1/VG | 按 `timestamp` 排序窗口；Hermes 按 `id`（时钟会回退） | high | patch | live DB 12/394 会话窗口不同、20 个顺序不同 |
| BH2/EC1/VG | 未过滤 `active/compacted`，撤销轮次会显示 | high | patch | live DB 6,464 条 `active=0` |
| BH3 | 只过滤 role='system'，真实 system 文本以 user 存储 | medium | patch | 1,347 条 `[System:`、110 条压缩参考 |
| BH4 | 未用 `display_kind/display_identity` 展示投影 | medium | patch | 597 条 hidden、4 组重复身份 |
| BH5 | 单条消息可达 350KB，无单条/总大小上限 | high | patch | live DB 5 条 >100KB |
| BH6 | tool-call “摘要”发送原始参数前缀（可能含命令/路径/密钥） | high | patch | 改 name + 「参数已折叠」 |
| BH7 | 抽屉一律标「执行会话」，task.session_id 是发起会话 | medium | patch | 区分来源标签 |
| BH8 | 未合并压缩 lineage（parent_session_id） | medium | defer | 记 deferred-work |
| BH9/EC2 | executor_session_id 可能配到另一轮 run | medium | patch | 只从解析出 session 的 run 取 |
| BH10/EC5 | 最新 session 不在 state.db 时不尝试更旧 run/task.session | medium | patch | 逐个候选回退 |
| BH11 | truncated 的计数口径与窗口不一致 | medium | patch | 活跃计数 + 显示 X/共 Y |
| BH12 | ended_at 取了不显示 | low | patch | 结束/耗时/运行中 |
| BH13 | 抽屉打开停在最旧消息 | medium | patch | 打开滚到最新 |
| BH14 | 抽屉非真模态（背景可滚动/可读） | medium | patch | body 锁 + inert/aria-hidden |
| BH15/VG3 | 抽屉打开时 A/R/S 未测，可能操作背景卡 | high | patch | 探针断言不前进 |
| BH16 | 探针 setReviewEventAge 未断言 rowcount | low | patch | 加断言 |
| BH17/VG1-2 | 后端/错误态测试缺口（空/锁库、missing、503 UI） | medium | patch | 补测试/探针 |
| BH18 | 测试 fixture 手抄 schema，缺 active/display 列 | medium | patch | 按真实列建 fixture |
| BH19/VG4 | README 未记 session 端点/env/test_session | medium | patch | 补文档与测试清单 |
| BH20 | 会话响应无 `Cache-Control: no-store` | high | patch | 加头 + README 警示 |
| BH21 | `_open_state_db` 异常路径连接泄漏 | medium | patch | finally close |
| BH22 | SQLite 读无执行预算（3s 只包上游） | low | patch | progress handler/总 deadline |
| BH23 | spec 任务未勾选、矩阵/Notes 缺边角 | medium | patch | 收尾补齐 |
| BH24 | verify-probe 注释仍写 2.3 slot | low | patch | 改注释 |
| BH25 | 四个探针各自复制 CDP harness | low | defer | 记 deferred-work |
| EC4 | 404 时 executor_session_id 不展示 | low | accept | worker session 缺失时没有可打开的会话；2.3 不展示 |

**结论：** 无 bad_spec/intent_gap；全部定向 patch（lineage/display 变体/run selector/探针 harness 记 defer/accept）。补丁后重跑 16+39+34+38+29 后端测试、115/31/22/18 探针与现场 session smoke。

## Design Notes

- 真实会话落点：`task_runs.metadata.worker_session_id`（worker 收尾由 `_stamp_worker_session_metadata` 写入）或 `tasks.session_id`；PLAN 中 `task.metadata.worker_session_id` 是概念路径，as-built 以 run metadata 为准。
- state.db 3GB；只读 URI + busy timeout + 按 session_id LIMIT 200；不碰 FTS/全表扫描。
- 执行器续跑（claude `--resume` / dsh `--session-id`）属 Epic 3；本故事只展示 id 与完整对话。
- 抽屉是应用内只读查看器；不跳 9119、不提供发消息/续跑，避免写入 state.db。
- 只取最近 200 条；tool calls 折叠；超出提示 truncated，不提供无限滚动。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck && npm run build`
- `python3 workbench-app/tests/test_session.py` -- 全绿
- 回归：`test_notifications.py` / `test_verify.py` / `test_dispatch.py` / `test_todo.py`
- 隔离实例：seed run metadata worker_session_id + 临时 state.db；断言 session/messages/limit/404/503
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`

**Manual checks:**
- 8787：验收页与已完成卡打开抽屉、长会话 truncated、tool calls 折叠、明暗主题；插件宿主无入口