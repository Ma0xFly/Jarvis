# Epic 1 Context: 派发骨架（P1）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让「想法 → 仓库 → 拖进流水线 → 排队等 worker」这条最小链路在真实数据上跑通，同时把看板 tab 从自建 5 列改为自有前端渲染、与 Hermes 原生 8 列逐项对齐（不嵌入 dashboard）。这是 P1（界面骨架，约 2.5 天）：完成后工作台第一次能真派活，且不再维护第二套状态口径。

## Stories

- Story 1.0: 前端构建脚手架（React + TS + Vite，双产物）
- Story 1.1: 看板 tab 对齐 Hermes 原生 8 列
- Story 1.2: 契约字段与派发闸门
- Story 1.3: 待办三区与拖拽派发
- Story 1.4: 派发回执与排队位次

## Requirements & Constraints

- 待办页三区固定顺序：流水线（302px）/ 今日 / 长期；拖进流水线即派发，整列任意纵向位置都是投放区。
- 仓库卡只呈现标题 + 验收一行：无状态点、无进度、无 assignee、无边框高亮。
- 验收为空的卡不得派发，前后端都拦；被拒时卡留在原位、不发起网络请求。1.2 的闸门覆盖 POST dispatch、直接 `PATCH status=ready`、无 `triage` 的建卡、`bulk status=ready` 与 `reclaim`（中间件拦，不转发；PATCH 不存在的 id 先 GET，404 透传）。
- 派发瞬间把预算/禁区的生效值快照写入 `metadata.contract_defaults`（`max_minutes` / `max_cost` / `deny_paths`）。1.2 只有“全局默认 → 卡上 `contract.budget`”两层；routing 层 deferred。前端不写死数值，全局默认读 `workbench-app/defaults.json`，耗时估算基准（7 分钟/张）也放同一文件。
- workbench 契约 metadata（`bucket` / `contract` / `contract_defaults`）存 `workbench-app/workbench.db` 的 `task_metadata`，不写共享 `kanban.db`；读取走 `GET /api/workbench/metadata?ids=`。
- 派发后 3 秒内显示排队位次与预计开始时间；刷新后按 `ready` 列顺序重算，不依赖本地缓存；重复派发幂等（200、不覆盖快照、位次重算）。
- 今日/长期用 `metadata.bucket = today|long` 区分，不占用 `todo` 状态；仓库卡统一 `triage`。
- 有未完成 parent 时派发返回 409 `blocked_by_parent`，卡落 `todo`，UI 提示「已转 todo」，且不重复入队。
- 看板必须与原生 8 列逐项对齐（名称 / 顺序 / status），列定义全库只允许一处常量：triage 待整理 · todo 待执行 · scheduled 已排期 · ready 就绪 · running 运行中 · blocked 待拍板 · review 待验收 · done 已完成。
- 看板卡片只显示 id、title、assignee、priority、block_kind、latest_summary、created_at/completed_at 中存在的字段，不自造状态徽章。
- 上游 `~/.hermes/hermes-agent/` 零改动；不直接写 `kanban.db`，一律走 REST；自定义后端端点只加在 `workbench-app/server.py` 的 `/api/workbench/*`。`server.py` 不 import `kanban_db` / `kanban_db_connect`，不依赖私有函数。
- 首屏 ≤ 1s，5s 轮询，不引入 WebSocket；服务重启后 `/healthz` 仍 200，不打断正在跑的任务。

## Technical Decisions

- 前端唯一源码是 `workbench-ui/src/`（React 18 + TS + Vite lib 模式，IIFE）。`npm run build` 产出两份并分发到插件 dashboard 与 `workbench-app/web/`；两个宿主目录里的 `index.js` / `style.css` 是产物，禁止手改。旧 `sync-ui.sh` 作废。
- plugin build 把 react / react-dom 设为 external，banner 从 `window.__HERMES_PLUGIN_SDK__` 取 React；standalone build 正常打包 React。宿主差异只允许出现在 `main.tsx`；数据层为 `api = SDK.fetchJSON | plainFetch`。
- 后端保持 FastAPI 薄壳（127.0.0.1:8787），复用挂载的 Hermes kanban REST；不得改 `plugin_api.py`。
- 派发端点 `POST /api/workbench/dispatch/{task_id}`，body 为空（契约在卡上）：读卡（先判 404；running → 200 `position:0`；done/archived → 409，均不要求验收）→ 校验 `contract.accept` 非空 → 合并 `defaults.json` + 卡上 `contract.budget`（带上限/类型校验）→ 先写 `contract_defaults` 快照（带 `write_token`）→ `PATCH status=ready` → **再 GET `/tasks/{id}` 读真实落点**；只有 ready 才读 `/board` 算位次（未命中重读一次，仍不命中 502），`eta = now + minutes_per_task × 位次`。成功返回 `{ok, position, eta, status}`。错误码：400 `missing_acceptance`、409 `blocked_by_parent` / `not_ready` / `not_dispatchable`、404 `not_found`、500 `invalid_defaults`、502 `upstream_unavailable`、503 `metadata_unavailable`。落点不是 `ready` 时 409 并补偿删除本次快照。
- ready 中间件闸门：覆盖 `PATCH status=ready`、无 `triage` 的 `POST /tasks`、`POST /tasks/bulk` + `status=ready`、`POST /tasks/{id}/reclaim`；与 dispatch 共用验收检查。空验收 400、metadata 库不可用 503，均不转发上游。dispatch 自身的 PATCH 因卡已有验收文本而放行。上游 `recompute_ready` 与原生 9119 dashboard 不经过本服务，属已知限制。
- REST 状态硬规则：`running` 不可手动 PATCH；blocked 恢复走 unblock（可能落 `ready` / `review` / `todo`）；PATCH 中 `None` = 不修改，清空用显式 `clear_*` 字段；有未完成 parent 的 `→ready` 可能返回 409。
- metadata 约定：`bucket`、`contract {input, accept, ready, budget?}`、`contract_defaults` 存 `workbench.db` 的 `task_metadata`（含内部 `write_token` 归属列，不对外返回）。1.2 只使用 `input/accept/budget`，不实现 provenance / `contract.ready`；`solidified` / `cost` / `hops` / `executor` / `executor_session_id` / `classification` 均未落地（deferred，不属于上游现状）。1.2 只提供读端点，写端点属 1.3。
- 验证：`cd workbench-ui && npm run typecheck`；`python3 workbench-app/tests/test_dispatch.py -v`（临时库）；重启 `systemctl --user restart jarvis-workbench` 后 `curl --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz`；实验可用 `WORKBENCH_PORT=8788` 另起一份，不打断在用服务。

## UX & Interaction Patterns

- 三列 `grid-template-columns: 302px 1fr 1fr`；流水线面板底色比仓库深一档（凹槽 vs 平面），视觉分区即语义分区。
- 拖拽时整列高亮；被拒时卡片抖动 300ms + 红字提示「先补一句怎样算做完」。
- 回执文案「已入队 · 第 N 位 · 预计 HH:MM」，落在该卡位置或顶部 toast；不做进度条动画。
- 底部保留一句话建卡输入框（默认 `bucket=today`）；新卡验收为空时用警示色标注，且不自动派发。
- hover 只换背景色，不加边框；明暗双主题全走 `theme.css` 的 token，不硬编码色值。
- 看板 8 列横排 + 横向滚动。

## Cross-Story Dependencies

- 1.0 是使能项：1.1–1.4 的 UI 都改同一份 `workbench-ui/` 源码并经构建分发。
- 1.1 先于 1.3：旧的 5 列渲染必须先删除，否则与待办三区两套并存。
- 1.2 先于 1.3：闸门决定拖拽的拒绝分支；1.3 调用 1.2 的 dispatch 端点。
- 1.4 依赖 1.2 返回的 `position` / `eta`，刷新后以 `/board` 为准。