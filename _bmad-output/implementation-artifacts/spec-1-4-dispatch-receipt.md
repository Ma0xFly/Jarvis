---
title: '1.4 派发回执与排队位次'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: '5ae0da25e9edcd16f69a86b6332c006f450bdbf9'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 派发成功后只显示「已派发」，用户看不到「第几位、什么时候开始」；队列位次也没有在流水线上持续可见，刷新后无法确认，容易因为没反馈而连点。

**Approach:** 新增 `Receipt`：派发成功用 1.2 返回的 `{position, eta}` 在 3 秒内显示「已入队 · 第 N 位 · 预计 HH:MM」；流水线 `ready` 段卡片按 `/board` 顺序显示 1-based「第 N 位」，每次轮询重算，不依赖本地缓存；running 卡显示「已在运行」。

## Boundaries & Constraints

**决策（2026-11-05，人已确认）：** 顶部回执条 + 流水线行内位次徽标；刷新后只显示「第 N 位」，不显示 ETA、不新增后端端点。

**Always:**
- 回执只消费 dispatch 返回值；前端不重算 ETA、不写死 7 分钟。
- 位次只按 `/board` 的 ready 段顺序计算（1-based），每次渲染/轮询重算。
- 回执 3 秒内出现；文案「已入队 · 第 N 位 · 预计 HH:MM」。
- running（position 0）→「已在运行」；409/400 走既有 toast/抖动，不出回执。
- 只在独立版待办页生效（插件宿主没有待办）。

**Never:**
- 不做进度条动画、不做 ETA 模型、不新增 WebSocket。
- 不持久化位次/ETA；不改派发闸门与 metadata。
- 不改后端；刷新后的位次纯前端从 `/board` 派生。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 派发成功 | dispatch 200，position>0 | 3 秒内回执「已入队 · 第 N 位 · 预计 HH:MM」 | N/A |
| 已在运行 | dispatch 200，position=0 | 回执「已在运行」 | N/A |
| 刷新 | 重新读 `/board` | ready 卡显示 1-based「第 N 位」，与 board 顺序一致；无本地缓存 | N/A |
| 队列变化 | 新卡派发/其他客户端改队列 | 下一次 5s 轮询重算位次 | N/A |
| 派发被拒 | 409 parent / 400 空验收 | 既有「已转为 todo」/抖动提示，不出回执 | 不谎报 |
| 回执超时 | dispatch 超过 3s | 等响应后显示；若失败按错误 toast | N/A |

</frozen-after-approval>

## Code Map

- `workbench-ui/src/todo/Receipt.tsx` — 新增：回执条/位次徽标组件。
- `workbench-ui/src/todo/TodoPage.tsx` — `dispatchToFlow` 捕获 `DispatchResult`；ready 段渲染 1-based 位次；回执状态。
- `workbench-ui/src/styles/todo.css` — 回执条、徽标、动效（无进度条）。
- `workbench-ui/src/api/workbench.ts` — `DispatchResult` 已含 `position`/`eta`/`status`，无需改契约。
- `workbench-app/server.py` — 不改（刷新位次由前端从 `/board` 派生）。
- `workbench-app/defaults.json` — `queue.minutes_per_task: 7`（服务端唯一来源；前端不读）。
- `workbench-ui/scripts/todo-probe.mjs` — 增加回执/位次/刷新检查。
- `workbench-app/README.md` — 回执寿命、latest-attempt、快照 vs 实时徽标、ETA 近似与三列术语。
- `_bmad-output/implementation-artifacts/epic-1-context.md` — running 例外、顶部专用回执条、三列术语。
- `_bmad-output/implementation-artifacts/deferred-work.md` — 共享 hook 证据补回执定时器。
- `workbench-app/web/index.js` / `workbench-app/web/style.css` — 独立版构建分发产物。
- `~/.hermes/plugins/workbench/dashboard/index.js` / `style.css` — 插件宿主构建分发产物（非仓库文件）。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-ui/src/todo/Receipt.tsx` -- 回执条与位次徽标 -- 即时反馈
- [x] `workbench-ui/src/todo/TodoPage.tsx` -- 用 dispatch 返回值出回执；ready 段按 board 顺序渲染位次 -- 回执与队列
- [x] `workbench-ui/src/styles/todo.css` -- 回执/徽标样式，明暗主题 token -- 视觉
- [x] `workbench-ui/scripts/todo-probe.mjs` -- 回执文案、running、刷新后位次、队列变化 -- 回归
- [x] `workbench-ui` -- `npm run build` 并分发 -- 双宿主产物
- [x] `workbench-app/server.py` -- 重启 + `/healthz`（后端无改动）-- 不破坏现有

**Acceptance Criteria:**
- Given dispatch 成功返回 `{position, eta}`, when UI 收到响应, then 3 秒内显示「已入队 · 第 N 位 · 预计 HH:MM」
- Given 页面刷新, when 重新加载 `/board`, then ready 卡显示按 board 顺序重算的 1-based 位次，不依赖本地缓存
- Given 队列已有 2 张, when 再派发 1 张, then 显示的预计时间与 `/api/workbench/dispatch` 返回值一致（±1 分钟）
- Given 卡已 running, when 重派, then 回执显示「已在运行」，不显示位次
- Given 派发被 409/400 拒绝, when UI 收到错误, then 既有提示保留，不出回执
- Given 改动完成, when `npm run build` + 重启, then 双宿主产物一致，`/healthz` → `{"ok":true}`

## Implementation Notes

- `Receipt.tsx`：`ReceiptBar` 为顶部瞬时条（成功后 6s 自动消失），`PositionBadge` 为流水线卡行内徽标；文案由 `receiptText` 单点生成。回执只透传 `DispatchResult.position/eta`：前端不读 `defaults.json`、不重算 ETA、不持久化。
- `TodoPage`：`dispatchToFlow` 发起前清掉旧回执，成功后 `showReceipt(result.position, result.eta)`；409/400/其他错误保持既有 toast/抖动，不产生回执。`ready` 段用 `segment.cards.map((task, index))` 派生 1-based 位次，每次渲染与 5s 轮询重算，不写 state/localStorage；`running` 卡显示「已在运行」。刷新后只保留行内位次，无 ETA、无回执。
- `todo.css`：回执条/徽标/入场动画全部走 `base.css` token（含 `color-mix`），明暗主题均成立；无进度条。
- `todo-probe.mjs` 扩到 64 项断言：回执出现 ≤3s、文案与 dispatch 响应逐字一致、ready 位次与 `/board` 顺序逐项一致、外部高优先级卡插队后 5s 轮询重算、刷新后位次重派生且无回执/ETA、running 重派 `position=0` →「已在运行」、409 不出回执、空验收零请求且不出回执。running/priority 场景直接写隔离 `kanban.db`（上游 REST 不允许手工置 running），不触碰真实库。
- 验证：`npm run typecheck`、`npm run build`（双产物分发，宿主文件与 `dist` 哈希一致）、`npm run probe:todo` 64/64；`systemctl --user restart jarvis-workbench` 后 `curl /healthz` → `{"ok":true}`。

### 2026-11-05 定向加固

- 回执状态机：drop 即 `pending`「正在派发…」，响应后替换为 final；失败分支先清 pending 再走既有 toast/抖动。全局串行派发（`dispatchInFlightRef`），忙时新 drop 忽略并 toast「正在派发上一张」，消除 `dispatchingId` 并发竞态与回执串台。
- `receiptText` 校验 position 为有限非负整数：0 →「已在运行」，>0 且 `eta=null` →「已入队 · 第 N 位」，NaN/Infinity/负数/小数 → `null`（不渲染谎报文案）。
- `ReceiptBar` 的 `role="status"` live region 常驻，只替换文本子节点；新增关闭按钮（`aria-label="关闭派发回执"`），关闭后同一次尝试的迟到响应不再显示；回执槽位始终预留 32px，出现/消失不重排三列；`prefers-reduced-motion` 关闭过渡；徽标文字与前景色 `color-mix`，探针验证明暗两主题 AA 对比度 ≥4.5。
- 回执 6 秒自动消失（`RECEIPT_MS`），采用 latest-attempt 语义；回执位次/ETA 是派发瞬间快照，行内徽标是每 5s 轮询实时值；回执不含卡片标题/ID；ETA 为队列槽位近似（位次 1 = 7 分钟），不扣除运行已用时间。
- 探针扩到 94 项：pending/串行/受控延迟响应（不测墙钟）、回执存活期 409/空验收拒绝清回执、关闭与 6s 自动消失、live-region 常驻与节点复用、回执不改变三列布局、卡片自身徽标 = dispatch position、`/board` 与 UI 同 query、刷新只查 receipt/badge 的 ETA、回执/徽标明暗对比度；running 场景暂停/恢复 5s 轮询、server 与测试共用 `HERMES_KANBAN_DB`、UPDATE 断言恰好 1 行。
- 文档：README（回执寿命/latest-attempt/无卡身份/快照 vs 实时/ETA 近似/三列术语）、epic context（running 例外、顶部专用回执条、三列）、deferred-work（共享 hook 证据补回执定时器）。

## Hardening Matrix (2026-11-05)

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| drop 即反馈 | 验收非空，drag drop | 立即显示「正在派发…」pending 回执 | N/A |
| 成功 | dispatch 200，position>0，eta 有值 | pending 替换为「已入队 · 第 N 位 · 预计 HH:MM」 | N/A |
| 位次无 ETA | dispatch 200，position>0，eta=null | 「已入队 · 第 N 位」 | 不显示「预计 null」 |
| 已在运行 | dispatch 200，position=0 | 「已在运行」，不显示位次 | N/A |
| 非 parent 的 409 | dispatch 409 `not_ready` / `not_dispatchable` | 既有「派发失败：…」toast；pending 清空 | 不谎报成功 |
| 上游不可用 | dispatch 502 / 503 | 既有错误 toast；pending 清空 | 不谎报成功 |
| 并发派发 | 一张在途时再 drop | 第二次 drop 忽略，toast「正在派发上一张」；只发一次请求 | 保留当前 pending |
| 关闭回执 | 点回执关闭按钮 | 立即隐藏；同一次尝试迟到的响应不再显示 | N/A |
| 刷新时派发在途 | reload | 新页面无回执/ETA，仅按 `/board` 派生行内位次 | N/A |
| 非法位次 | position 为 NaN/Infinity/负数/小数 | `receiptText` 返回 null，不渲染文案 | 拒绝 |
| 拒绝时有旧回执 | 回执显示中拖 409 / 空验收卡 | 回执立即清空，原 toast/抖动保留 | 不保留旧成功回执 |

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1 | 3 秒要求与“超时后显示”自相矛盾 | medium | patch | 改为 drop 时 pending、响应后最终回执（服务端预算 <3s） |
| BH2 | 无 pending 视觉/aria-busy | medium | patch | 加「正在派发…」回执 |
| BH3 | 探针用墙钟量 3s，易 flake | low | patch | 改为 pending + 受控响应/宽松 slack |
| BH4 | 两张卡并发派发，回执串台 | medium | patch | 全局串行化，忙时忽略新 drop |
| BH5 | 回执 6s 与实时徽标可能不一致 | low | patch | 文档标注“派发时快照”；徽标实时 |
| BH6 | 新尝试清掉上一条成功回执 | low | patch | 采用 latest-attempt 语义并写文档/测试 |
| BH7 | 拒绝无回执断言在无回执时运行 | high | patch | 回执仍存活时测 409/空验收拒绝 |
| BH8 | running 场景直接写库与轮询竞态 | medium | patch | 暂停轮询、同一 DB、校验 rowcount |
| BH9 | 探针 expected 用 include_archived，UI 不用 | low | patch | 统一查询 |
| BH10 | 未断言派发卡自身徽标；刷新搜索过宽 | medium | patch | 卡级断言 + 限定 receipt/badge |
| BH11 | eta null 分支未测；position 接受 NaN/Infinity | medium | patch | 有限整数校验 + 文案兜底 |
| BH12 | live region 动态挂载；无 dismiss/reduced-motion/对比度 | medium | patch | 常驻 live region + 关闭按钮 + 动效/对比度 |
| BH13 | 主题探针只测 flow 背景 | low | patch | 加 receipt/badge 主题对比 |
| BH14 | 回执条改变网格高度，列抖动 | low | patch | overlay/预留高度 |
| BH15 | ETA 是队列槽位近似，未文档化 | medium | patch | README/spec 说明 position 1=7 分钟、不减运行已用 |
| BH16 | README 漏回执寿命/身份/快照/ETA 口径 | low | patch | 补文档 |
| BH17 | epic context 仍是旧回执/三区措辞 | low | patch | 同步 running 例外/顶部条/三列 |
| BH18 | spec 矩阵/Code Map/Verification 不完整 | low | patch | 补行/路径/哈希命令 |
| BH19 | deferred-work 未含回执 timer；文件缺尾随换行 | low | patch | 更新证据 + 换行 |
| EC1 | 同 BH4 | carried | carried | 同一主张 |
| EC2 | 同 BH8 | carried | carried | 同上 |
| EC3 | 探针 server env 未固定 HERMES_KANBAN_DB | medium | patch | spawn env 固定同一文件 |
| EC4 | 同 BH12 | carried | carried | 同上 |
| VG1 | 拒绝无回执断言无效 | high | patch | 同 BH7 |
| VG2 | 6s 自动消失未测 | medium | patch | 等 >RECEIPT_MS 断言消失 |

**分组与路由：** 无 bad_spec/intent_gap；全部定向 patch。采用 latest-attempt 回执语义：新尝试先替换为 pending，成功替换为最终回执，失败清 pending + 错误提示；不保留上一条成功回执。全局串行派发，避免回执身份歧义。

## Design Notes

- 位次是派生值：ready 段内按 `/board` 顺序的 index+1；不存 state，不写 localStorage。
- 回执是瞬时 UI：顶部回执条显示 dispatch 返回的位次 + ETA；刷新后只保留行内「第 N 位」，不显示 ETA。
- 前端不读 `defaults.json`、不计算 ETA；ETA 只来自 dispatch 响应。
- 插件宿主没有待办页，回执自然不出现。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck && npm run build`
- `cd workbench-ui && npm run probe:todo`（扩展后）-- 全绿
- `cd workbench-ui && npm run build && cmp -s dist/standalone/index.js ../workbench-app/web/index.js && cmp -s dist/standalone/style.css ../workbench-app/web/style.css && cmp -s dist/plugin/index.js ~/.hermes/plugins/workbench/dashboard/index.js && cmp -s dist/plugin/style.css ~/.hermes/plugins/workbench/dashboard/style.css`
- 产物哈希：`sha256sum workbench-ui/dist/{plugin,standalone}/{index.js,style.css} workbench-app/web/{index.js,style.css} ~/.hermes/plugins/workbench/dashboard/{index.js,style.css}`
- 隔离实例：派发两张卡，断言 pending/最终回执位次/ETA 与 dispatch 响应一致；刷新后位次重算
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`

**Manual checks:**
- 8787 待办：派发回执、刷新位次、running 卡、明暗主题
