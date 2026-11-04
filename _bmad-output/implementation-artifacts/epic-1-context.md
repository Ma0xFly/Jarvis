# Epic 1 Context: 派发骨架（P1）

<!-- Compiled from planning artifacts. Edit freely. Regenerate with compile-epic-context if planning docs change. -->

## Goal

让「想法 → 仓库 → 拖进流水线 → 排队等 worker」这条最小链路在真实数据上跑通，同时把看板 tab 交还给 Hermes 原生实现。这是 P1（界面骨架，约 2.5 天）：完成后工作台第一次能真派活，且不再维护一套自建看板——状态操作只有一种口径。

## Stories

- Story 1.1: 看板 tab 回归 Hermes 原生
- Story 1.2: 契约字段与派发闸门
- Story 1.3: 待办三区与拖拽派发
- Story 1.4: 派发回执与排队位次

## Requirements & Constraints

- 待办页三区固定顺序：流水线 / 今日 / 长期；拖进流水线即派发，整列任何纵向位置都是投放区。
- 仓库卡只呈现标题 + 验收一行：无状态点、无进度、无 assignee、无边框高亮。
- 验收为空的卡不得派发，前后端都要拦；拒绝时不产生任何状态变更。
- 派发瞬间把预算与禁区的生效值快照进 `metadata.contract_defaults`，事后能说清它当时按什么规则跑。
- 派发后 3 秒内给出排队位次与预计开始时间；刷新后按 `ready` 列顺序重算，不依赖本地缓存。
- 今日/长期用 `metadata.bucket` 区分，不占用 `todo` 状态。
- 有未完成 parent 时派发返回 409，提示「已转 todo」，且不重复入队。
- 看板 tab 必须是 Hermes 原生 8 列，不自建列渲染。
- 上游 `~/.hermes/hermes-agent/` 零改动；不直接写 `kanban.db`，一律走 REST。
- 首屏 ≤ 1s，5s 轮询，不引入 WebSocket。
- 自定义端点只允许加在 `workbench-app/server.py` 的 `/api/workbench/*` 下。

## Technical Decisions

- 前端源是 `~/.hermes/plugins/workbench/dashboard/index.js` 与 `style.css`；改完必须 `./sync-ui.sh` 复制到 `workbench-app/web/app.js`，直接改副本会被覆盖。仅独立版需要的能力用 `web/boot.js` 或 `sdk-shim.js` 适配。
- 派发端点 `POST /api/workbench/dispatch/{task_id}`，body 为空（契约已在卡上）：成功返回位次与预计开始时间，验收为空返 400，被 parent 阻塞返 409。服务端流程为读卡 → 校验 `contract.accept` 非空 → 写 `contract_defaults` → `PATCH status=ready` → 按 `/board` 的 `ready` 列算位次；重复派发要幂等。
- 状态机靠 Hermes REST：`running` 不可手动设置；从 blocked 恢复只能落 `ready`（落 `todo` 会丢事件）；PATCH 中未提供的字段视为不修改。
- 预算取三层默认合并（全局 → `routing.json` 路线 → 卡覆盖）；前端不写死数值，缺失时读 `workbench-app/defaults.json`，耗时估算基准也放同一文件。
- metadata 契约：`contract`（输入 + 验收）、`contract_defaults`（生效值快照）、`bucket`；仓库卡统一 `triage`。
- 验证：重启服务后 `/healthz` 返回 `{"ok":true}` 且控制台无报错；实验可用 `WORKBENCH_PORT=8788` 另起一份，不打断在用服务。

## UX & Interaction Patterns

- 流水线面板底色比仓库深一档（凹槽 vs 平面）：视觉分区即语义分区。
- 拖拽中整列高亮；被拒时卡片抖动 300ms + 红字提示。
- 回执文案「已入队 · 第 N 位 · 预计 HH:MM」，落在该卡位置或顶部 toast；不做进度条动画。
- 底部保留一句话建卡输入框，新卡落今日列，验收为空以警示色标注，不自动派发。
- hover 只换背景色不加边框；明暗双主题全走 `theme.css` 的 token。

## Cross-Story Dependencies

- 1.1 先于 1.3：先删自建看板，三区才不会与旧渲染两套并存。
- 1.2 先于 1.3：闸门决定拖拽的拒绝分支。
- 1.3 依赖 1.2 的派发端点；1.4 依赖 1.2 返回的位次与预计时间。