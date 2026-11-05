---
title: '1.3 待办三区与拖拽派发'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: '74736dd40e82ca63c992518e32ff899d3c023e8d'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 待办页还没有三区：仓库卡（`triage` + `bucket`）无法按今日/长期分开看，也无法把卡拖进流水线触发 1.2 的派发闸门；新建卡没有入口，验收为空时没有可视提示，契约也没法在 UI 里补全。

**Approach:** 新增 `TodoPage`：流水线（302px）/ 今日 / 长期三列；仓库卡只显示标题 + 验收一行，无状态装饰；拖进流水线调用 1.2 的 `dispatchTask`，验收为空则抖动 + 红字提示且不发请求；底部一句话建卡默认落今日。后端补 `POST /api/workbench/tasks` 与 `PATCH /api/workbench/metadata/{task_id}`，把 `bucket` 与 `contract` 写进 `workbench.db`；验收在卡上就地编辑。

## Boundaries & Constraints

**决策（2026-11-05，人已确认）：** 验收在卡上就地编辑；流水线只放 ready/running/review（队列/运行/待收分段）；待办第一且默认；今日/长期固定 `created_at` 倒序，不做栏内拖动/持久化。

**Always:**
- 三列顺序固定 流水线→今日→长期，`grid-template-columns: 302px 1fr 1fr`。
- 仓库卡（今日/长期）= `triage` + `bucket`；只显示标题 + 验收一行；无状态点/进度/assignee/边框高亮，hover 只换背景。
- 验收为空：警示色；拖入流水线 → 抖动 300ms + 红字「先补一句怎样算做完」，零请求。
- 派发只走 `dispatchTask`（1.2），不直接 PATCH ready；409 → 「已转为 todo，等 parent 完成」。
- 新建卡默认 `bucket=today`，落 `triage`，不自动派发；空标题不建卡。
- `bucket` 只用 `today|long`，不占用 `todo`。
- 明暗主题走 `base.css` token，不硬编码色值。

**Never:**
- 不做长期→今日自动倒流、1.4 回执 UI、全屏验收、栏内拖动排序与排序持久化。
- 不改上游文件、不写共享 `kanban.db`、不引入前端依赖。
- 不动看板 8 列行为。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开待办 | 有 today/long 卡 | 三列顺序正确；今日/长期只含对应 bucket 的 triage 卡；计数=渲染数 | N/A |
| 拖有验收卡 | 拖到流水线任意纵向位置 | 整列高亮；松手 `dispatchTask` 200；卡离开仓库，toast「已派发」 | 失败 toast |
| 拖空验收卡 | 拖到流水线 | 回原位、抖动 300ms、红字提示、零请求 | 客户端拦 |
| 409 parent | 拖有验收卡 | toast「已转为 todo，等 parent 完成」；刷新后不在今日/长期 | 不重复入队 |
| 新建卡 | 底部输入 + Enter | 落今日、triage、验收空警示、不派发 | 空标题忽略 |
| 编辑验收 | 点验收行 → 输入 → Enter/blur | 写入 workbench.db；警示消失；可派发 | 保存失败 toast |
| 轮询 | 5s | 列表/计数刷新，不丢输入焦点 | N/A |

</frozen-after-approval>

## Code Map

- `workbench-ui/src/todo/TodoPage.tsx` — 新增：三列、卡片、拖拽、建卡、验收编辑、toast/抖动。
- `workbench-ui/src/styles/todo.css` — 新增：`302px 1fr 1fr`、凹槽底色、投放高亮、抖动动画。
- `workbench-ui/src/App.tsx` — 增加「待办」tab，待办第一且默认打开。
- `workbench-ui/src/api/workbench.ts` — 增加 `createTask(title, bucket)`、`updateTaskMetadata(id, patch)`；复用 `getTaskMetadata`/`mergeTaskMetadata`/`hasAcceptance(metadata)`/`dispatchTask`。
- `workbench-ui/src/board/Board.tsx` — 复用卡片/拖拽/toast 模式；不改看板行为。
- `workbench-ui/src/platform.ts` — `api`/`ApiError` 已支持 status+code。
- `workbench-app/server.py` — `MetadataStore.merge`（JSON1 按 key，保留 `contract_defaults`）、`WorkbenchService.create_task/update_metadata`、路由 `POST /api/workbench/tasks` 与 `PATCH /api/workbench/metadata/{task_id}`；建卡走上游 `POST /tasks` 且 `triage=true`（1.2 闸门放行）。
- `workbench-app/workbench.db` — `task_metadata` 写 `bucket`/`contract`；`contract_defaults` 不被覆盖。
- 上游 `POST /tasks` 返回 `{task:{id,…}}`；列表取 `GET /board`。
- `workbench-app/tests/test_dispatch.py`（或新 `test_todo.py`）— 建卡/改 metadata 测试；UI 用 headless 探针覆盖矩阵。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- 建卡与 metadata 写端点；校验 bucket/contract；merge 保留 contract_defaults -- 契约可写
- [x] `workbench-ui/src/api/workbench.ts` -- `createTask` / `updateTaskMetadata` + 类型 -- 前端可调用
- [x] `workbench-ui/src/todo/TodoPage.tsx` -- 三列、卡片、拖拽、建卡、验收编辑、409/空验收分支 -- 待办三区
- [x] `workbench-ui/src/styles/todo.css` -- 布局、凹槽、投放高亮、抖动 -- 视觉分区
- [x] `workbench-ui/src/App.tsx` -- 待办 tab 与默认页 -- 入口
- [x] `workbench-app/tests/` -- 建卡/改 metadata 后端测试 + headless UI 探针 -- 回归
- [x] `workbench-ui` -- `npm run build` 并分发 -- 双宿主生效
- [x] `workbench-app/server.py` -- 重启 + `/healthz` -- 不破坏现有

**Acceptance Criteria:**
- Given today/long 卡, when 打开待办, then 列顺序 流水线→今日→长期，今日/长期只含对应 bucket 的 triage 卡，计数=渲染数
- Given 仓库卡, when 静态观察, then 只有标题 + 验收一行，无状态点/进度/assignee/边框高亮，hover 只换背景
- Given 拖动今日的卡, when 指针进入流水线任意纵向位置, then 整列高亮；松手走 1.2 dispatch，成功后卡离开仓库
- Given 验收为空的卡, when 拖进流水线, then 回原位、抖动 300ms、红字提示、零请求
- Given 新建卡, when 输入标题回车, then 落今日、验收空警示、不自动派发
- Given 补完验收, when 再拖入流水线, then dispatch 成功且警示消失
- Given 有未完成 parent, when 派发, then toast「已转为 todo，等 parent 完成」，卡不在今日/长期
- Given 改动完成, when 检查 workbench.db/kanban.db, then bucket/contract 在 workbench.db；共享 kanban.db 无 workbench 对象
- Given 服务重启, when `curl /healthz`, then `{"ok":true}`，看板 8 列正常

## Implementation Notes

- 后端写端点：`POST /api/workbench/tasks`（缺省 `bucket=today`，显式 null 视为非法；上游 `triage=true`；响应 `{ok,task,metadata}`）与 `PATCH /api/workbench/metadata/{task_id}`（只接受 `bucket`/`contract`，`contract` 内只接受 `input/accept/ready/budget`；`contract_defaults` 不可由客户端写）。建卡/写 metadata 的路由通过 `run_in_threadpool` 执行，避免 async 路由阻塞事件循环导致回环自调用超时。
- `MetadataStore.merge` 用 JSON1 `json_patch` 在 `BEGIN IMMEDIATE` 中按 key 合并，嵌套 `contract` 不整块覆盖，`contract_defaults` 保留；`accept` 落库前 trim，与派发闸门比较口径一致。
- 前端 `TodoPage`：三列固定顺序；今日/长期过滤 `triage` + `bucket` 并按 `created_at` 倒序；仓库卡仅标题 + 验收一行（无边框/状态装饰，hover 只换背景）；流水线整列接收拖放并按 ready/running/review 分段；空验收在客户端 `hasAcceptance` 拦截（抖动 320ms 清理、红字 2.6s、零 dispatch 请求）；验收就地编辑 Enter/blur 保存、Esc 取消；底部建卡默认 today 且空标题不发请求；5s 轮询只替换 board/metadata，不重置编辑输入。
- `todo.css` 只使用 `base.css` token；新增 `--wb-sunken`（`color-mix`，无支持时回退 `--wb-card`）表达流水线凹槽。`App.tsx` 待办第一且默认打开。
- 回归：`test_todo.py` 10 项 + `test_dispatch.py` 32 项全绿；`scripts/todo-probe.mjs`（`npm run probe:todo`，无新增依赖）33 项断言覆盖矩阵含三列/计数、静态卡片、空验收、验收编辑、派发、建卡、409、轮询焦点、明暗 token、窄视口、双 tab 与看板 8 列。`npm run build` 分发后宿主产物与 `dist` 一致；真实服务重启后 `/healthz={"ok":true}`、看板 8 列正常；`kanban.db` 无 `workbench%` 对象且 `tasks` 无 `metadata` 列，`workbench.db` 只含 `task_metadata`。

### 2026-11-05 定向加固

- **插件宿主**：`App.tsx` 用 `isPluginHost` 只显示看板并默认看板，附提示「待办请用 127.0.0.1:8787 独立版」；独立版保持待办第一/默认。两个面板常驻挂载，切 tab 只切 `display`，保留草稿/验收编辑状态。待办运行时只在独立版验证。
- **建卡**：`idempotency_key`（≤128）透传上游；建卡后 GET 校验 `status=="triage"`，否则 502 + 实际 `status`；上游 4xx → 400（含 `detail`/`upstream_status`），5xx/不可达 → 502；metadata 失败 → 503 + `task_id`，同 key 重试不重复建卡。UI 每个草稿尝试生成一个 UUID，失败复用、成功清空。
- **metadata 写**：`task_id` 先 strip，上游 GET 不存在 → 404，不再 upsert 孤儿行；dispatch（闸门读→PATCH→落点）与 metadata 写共用 per-task 锁；落点 ready 后重读验收兜底。校验上限：accept ≤2000、input ≤4000、deny_paths 1–100、请求体 ≤64KB；拒 `contract_defaults`/未知键/空 deny_paths。POST/PATCH 要求 `application/json`（415）且 `Origin` 与 Host 同源（403）；读取 body 捕获 `ClientDisconnect`。
- **UI**：composer 增加今日/长期切换（默认今日）；验收保存失败保留输入并重新聚焦；编辑/保存/派发中禁止拖拽，per-card 保存与派发 pending 去重；drop/dragend 清除整列高亮；首屏 loading 不提前显示空列；`TaskMetadata.bucket` 使用 `Bucket`；`fallbackCode` 按 dispatch/create/metadata 区分；`--wb-sunken` 改用 `--wb-page` token mix（无 `#000`）；仓库列 `minmax(220px,1fr)`，窄视口横向滚动而非挤没（加固覆盖字面 `1fr 1fr`）。
- **测试/探针**：`test_todo.py` 扩到 27 项（幂等、404/502/503、四类校验、Content-Type/Origin/64KB、dispatch 同源、锁等待）；探针扩到 46 项（真实 `draggable`、shake 完整窗口、dragend、外部轮询、created_at 排序、长期建卡、窄视口 ≥200px、tab 草稿保持）。新增 `npm run verify`（build + probe）。
- **文档**：ARCHITECTURE-SPINE 补 `POST /tasks`/建卡与写端点、锁与请求校验，并注明待办仅独立版；README 去掉过期 localStorage/主题开关描述；deferred-work 移除已完成的 1.3 写端点条目，新增插件宿主待办、无 bucket triage、budget 清空、共享 hook、1.4 回执。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1 | 插件宿主无 `/api/workbench/*`，默认待办页半残 | high | patch | 插件宿主仅挂 kanban router；改为宿主感知 tab（插件只留看板 + 提示） |
| BH2 | 建卡非原子、无幂等，重试会重复 | high | patch | 上游支持 `idempotency_key`；UI 每次提交复用一个 key |
| BH3 | 建卡把所有非 200 映射成 502 | medium | patch | 4xx→400、5xx/不可达→502，保留 detail |
| BH4 | metadata 可对不存在 id upsert | medium | patch | 先 GET 上游，404；测试改为 404 |
| BH5 | `contract.ready` 可写但无人读 | low | defer | P1 不使用；语义留给后续 |
| BH6 | 预算不可清空；空 deny_paths 可覆盖全局 | medium | patch | 拒绝空 deny_paths；预算清空记 deferred |
| BH7 | metadata 无长度/数量/请求体上限 | medium | patch | 加长度、deny_paths 数量、64KB body 限制 |
| BH8 | 写端点无 Origin/Content-Type 校验，可 CSRF | medium | patch | 要求 JSON + 同源 Origin/Host |
| BH9 | 长期列无法从 UI 建卡 | medium | patch | 底部加今日/长期切换，默认今日 |
| BH10 | 无 bucket 的 triage 卡在待办不可见 | medium | defer | 记入 deferred，后续加“未分类” |
| BH11 | 验收保存失败丢弃输入文本 | medium | patch | 失败保留编辑框与文本 |
| BH12 | 派发无 in-flight guard，可重复请求 | medium | patch | 每卡 pending 集合，重复 drop 忽略 |
| BH13 | 拖拽高亮可能残留 | low | patch | dragend/drop 清理，处理 null relatedTarget |
| BH14 | 首屏无 loading，空态与 404 不可分 | low | patch | 首次加载显示“加载中…” |
| BH15 | 窄视口 1fr 列塌陷 | medium | patch | `minmax(220px,1fr)` + 探针断言宽度/滚动 |
| BH16 | `--wb-sunken` 硬编码 #000 | low | patch | 改为 token/currentColor 混合 |
| BH17 | 探针弱：drag 直造事件、无条件断言 | medium | patch | 补真实 draggable、抖动窗口、轮询、顺序、主题 |
| BH18 | probe 未进门禁 | low | patch | 加 `npm run verify` 并写前置条件 |
| BH19 | test_todo 缺 502/503/畸形/并发 | medium | patch | 补测试 |
| BH20 | deferred-work 仍写 1.3 写端点待做 | low | patch | 更新条目并补 1.3 新 deferral |
| BH21 | 架构仍写只依赖三条 REST | low | patch | 加 `POST /tasks` 与写端点 |
| BH22 | README 仍说 localStorage 排序 | low | patch | 文案修正 |
| BH23 | 端点文档缺字段/trim/404/幂等 | low | patch | 补 README |
| BH24 | `bucket` 类型并成 string；400 兜底误映射 | low | patch | 用 `Bucket`；fallback 按调用区分 |
| BH25 | Board/Todo 各自实现 toast/轮询/拖拽 | low | defer | 抽共享 hook 记 deferred |
| BH26 | 生成物未进 diff | false | — | `diff -q` 已验四份一致；探针读构建产物 |
| EC1 | 同 BH2 | carried | carried | 同一主张 |
| EC2 | metadata task_id 未 strip | medium | patch | strip + 空值 400 |
| EC3 | ClientDisconnect 未捕获 | low | patch | 捕获并返回 None |
| EC4 | 派发中途清空验收可落 ready | medium | patch | 服务端 per-task 锁覆盖 gate→PATCH |
| EC5 | 验收保存中可拖拽派发 | medium | patch | 保存中也禁用拖拽 |
| EC6 | 切 tab 丢失草稿/编辑 | medium | patch | TodoPage 保持挂载、CSS 隐藏 |
| EC7 | 同 BH15 | carried | carried | 同上 |
| EC8 | 探针 CDP 崩溃会挂死 | low | patch | socket close 拒绝 pending + 超时 |
| EC9 | 同 BH15/BH17 | carried | carried | 同上 |
| VG1 | 轮询刷新未被测试 | medium | patch | 探针外部建卡后断言 6s 内出现 |
| VG2 | created_at 倒序未被断言 | medium | patch | 探针读 created_at 比对 DOM 顺序 |
| VG3 | 502/503 写端点未被测试 | medium | patch | 补 broken DB/closed upstream 测试 |
| VG4 | 同 BH2 | carried | carried | 同上 |

**分组与路由：** 无 bad_spec/intent_gap；全部为定向 patch 或 defer。插件宿主仅保留看板并提示待办用 8788/8787 独立版；长期建卡、锁、幂等、Origin/长度校验、探针与文档同步在本轮补丁内完成。defer：未分类卡、contract.ready、预算清空、共享 hook、插件宿主运行时验收。

## Design Notes

- 卡片只承载标题 + 验收；`contract.input`（材料/来源）暂不在 1.3 UI 暴露，留给后续详情抽屉。
- `contract.ready` 不参与闸门；1.3 只写 `contract.accept`（以及建卡时的空 `input`/`ready`）。
- 流水线只放 ready/running/review，分段为队列/运行/待收；todo/blocked/scheduled 留在看板。
- 今日/长期列表固定 `created_at` 倒序，不做栏内拖动；验收在卡上就地编辑。
- 后台建卡先 `POST /tasks`（triage=true）再写 metadata；两步非原子，失败时返回错误并提示重试；孤儿空 metadata 无副作用。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck && npm run build`
- `python3 workbench-app/tests/test_dispatch.py`（或 `test_todo.py`）-- 全绿
- 隔离实例 `WORKBENCH_PORT=8788` + 临时 `HERMES_KANBAN_HOME`/`WORKBENCH_DB`；建卡、改验收、派发
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`
- headless 探针：三列顺序/计数、空验收抖动零请求、有验收派发、新建卡落今日、验收编辑

**Manual checks:**
- 8787 待办/看板两 tab；明暗主题；窄视口