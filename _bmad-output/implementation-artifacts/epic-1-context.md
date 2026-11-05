# Epic 1 Context: 派发骨架（P1）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让「想法 → 仓库 → 拖进流水线 → 排队等 worker」这条最小链路在真实数据上跑通，同时把看板 tab 改为自有前端渲染、与 Hermes 原生 8 列逐项对齐（不嵌入原生 dashboard），移除自建 5 列这套第二状态口径。P1 完成后，工作台第一次能真派活，且不再维护与上游不一致的看板语义。

## Stories

- Story 1.0: 前端构建脚手架（React + TS + Vite，双产物）
- Story 1.1: 看板 tab 对齐 Hermes 原生 8 列
- Story 1.2: 契约字段与派发闸门
- Story 1.3: 待办三列与拖拽派发
- Story 1.4: 派发回执与排队位次

## Requirements & Constraints

- 待办页固定三列：流水线（302px）/ 今日 / 长期；拖进流水线即派发，整列任意纵向位置都是投放区。
- 仓库卡只有标题 + 验收一行：无状态点、进度、assignee、边框高亮。今日/长期用 `metadata.bucket = today|long` 区分，不占用 `todo`；仓库卡统一 `triage`。
- 验收为空的卡不得派发，前后端都拦；闸门覆盖 dispatch、直接 `PATCH status=ready`、无 `triage` 建卡、`bulk status=ready` 和 `reclaim`，返回 400 且不转发上游。
- 派发瞬间把预算/禁区生效值快照进 `metadata.contract_defaults`（`max_minutes` / `max_cost` / `deny_paths`）；来源为 `workbench-app/defaults.json` 全局默认 + 卡上 `contract.budget` 覆盖；P1 不接 routing 层，前端不写死数值。
- 派发回执在 drop 时以 pending「正在派发…」出现，响应后显示排队位次与预计开始时间；已 running 重派（position 0）显示「已在运行」；刷新后按 `ready` 列顺序重算；重复派发幂等、不覆盖已有快照；有未完成 parent 时返回 409，卡落 `todo`，UI 提示「已转为 todo」。
- 看板 8 列的名称/顺序/status 与原生逐项对齐，列定义全库只允许一处常量；卡片只显示 id、title、assignee、priority、block_kind、latest_summary、created_at/completed_at 中存在的字段，不自造状态徽章。
- NFR：上游 `~/.hermes/hermes-agent/` 零改动；不直接写共享 `kanban.db`，一律经 REST；首屏 ≤ 1s、5s 轮询、不引入 WebSocket；服务重启后 `/healthz` 仍 200 且不打断在跑任务；源码只改 `workbench-ui/` 与 `workbench-app/`，两宿主目录的 `index.js` / `style.css` 是构建产物。

## Technical Decisions

- 前端唯一源码在 `workbench-ui/src/`（React 18 + TS + Vite，产物为 IIFE）：plugin build 把 react/react-dom 设为 external，React 从 `window.__HERMES_PLUGIN_SDK__` 取；standalone build 正常打包 React。`npm run build` 一次分发到插件 dashboard 与 `workbench-app/web/`；宿主差异只允许出现在 `main.tsx`，数据层统一为 `api = SDK.fetchJSON | plainFetch`；旧 `sync-ui.sh` / `web/app.js` 流程作废。
- 后端是 FastAPI 薄壳（127.0.0.1:8787），挂载上游 kanban router；只依赖 `GET /board`、`GET /tasks/{id}`、`PATCH /tasks/{id}`、`POST /tasks` 公开 REST，不 import `kanban_db`、不改 `plugin_api.py`；自定义端点只加在 `/api/workbench/*`。
- 派发端点 `POST /api/workbench/dispatch/{task_id}`，body 为空（契约在卡上）：读卡 → 校验 `contract.accept` 非空 → 合并预算并写 `contract_defaults` 快照（带内部 `write_token`）→ `PATCH status=ready` → 再 GET 读取真实落点。只有落点为 `ready` 才按 `/board` 的 ready 列顺序算位次，`eta = now + minutes_per_task × 位次`（默认 7 分钟/张，来自 `defaults.json`）；成功返回 `{ok, position, eta, status}`。已 running 返回 `position: 0`（前端回执显示「已在运行」，不显示位次）；done/archived 返回 409 不复活；落点非 ready 返回 409 并补偿删除本次快照；上游不可用返回 502。
- 契约 metadata（`bucket` / `contract` / `contract_defaults`）存工作台自有 `workbench-app/workbench.db` 的 `task_metadata`（JSON1 按 key 更新），读取走 `GET /api/workbench/metadata?ids=`；`write_token` 不对外返回。P1 只使用 `contract.input/accept/budget`，不实现 provenance / `contract.ready`；1.3 增加 `POST /api/workbench/tasks`（`triage=true` + `idempotency_key`）与 `PATCH /api/workbench/metadata/{id}`（仅 bucket/contract），与 dispatch 共用 per-task 锁。
- 状态硬规则：`running` 不可手动 PATCH；blocked 恢复走 `ready`（unblock 路径）；PATCH 中 `None` = 不修改；有未完成 parent 的 `→ready` 可能返回 409。上游 `recompute_ready` 与原生 9119 dashboard 不经过本服务闸门，属已知限制，不额外加 watcher。
- 插件宿主没有 `/api/workbench/*`，只渲染看板 tab；待办三列与派发回执仅在独立版 8787 提供。

## UX & Interaction Patterns

- 三列 `grid-template-columns: 302px 1fr 1fr`；流水线面板底色比仓库深一档（凹槽 vs 平面），视觉分区即语义分区。
- 拖拽中整列高亮；被拒时卡片抖动 300ms + 红字提示「先补一句怎样算做完」。
- 回执固定落在顶部专用回执条（不是 toast）：drop 时「正在派发…」，响应后「已入队 · 第 N 位 · 预计 HH:MM」；已 running 重派显示「已在运行」（不显示位次）；只保留最近一次派发尝试，6 秒后自动消失、可手动关闭；不做进度条动画。
- 底部保留一句话建卡输入框，默认落 `bucket=today`；新卡验收为空以警示色标注，且不自动派发；hover 只换背景色；明暗双主题全走 `theme.css` token，不硬编码色值；看板 8 列横排 + 横向滚动。

## Cross-Story Dependencies

- 1.0 是使能项：1.1–1.4 的 UI 都改同一份 `workbench-ui/` 源码并经构建分发。
- 1.1 先于 1.3：旧 5 列渲染必须先删除，否则与待办三列两套并存。
- 1.2 先于 1.3：闸门决定拖拽的拒绝分支；1.3 调用 1.2 的 dispatch 端点。
- 1.4 依赖 1.2 返回的 `position` / `eta`；刷新后以 `/board` 为准，不依赖本地缓存。
