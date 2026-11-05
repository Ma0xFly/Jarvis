---
title: '2.2 P0 通知与通知操作'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: 'ba2ee0a41942f1666571a628fc50ab3da7b15774'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 待验收与阻塞卡目前只有一个数字入口；会堵下游的卡（有未完成子卡或 worker 在等人回答）不会主动叫人，review 卡也没有升级、攒批与通知内动作，用户只能反复打开验收页或看板。

**Approach:** 新增后端通知引擎（5s 轮询 board）：把 `blocked` / `review` 卡判定为 P0/P1/P2；P0 走桌面系统通知（`notify-send`）并在右上角常驻/置顶，P1 攒 3 条或 30 分钟推一次，P2 每日一行；同一事件 30 分钟未处理只升级一次。新增通知快照与动作端点，前端新增通知中心：P0 置顶、P1 面板、P2 日报、review 卡「还有 Xh Ym 自动收」倒计时、收/不收/10 分钟后再提醒、验收槽容量 3（兄弟卡成束）。动作复用 2.1 的 verify。

## Boundaries & Constraints

**Always:**
- 分类只读 `/board` + `/tasks/{id}`：`blocked` 且有未完成子卡，或 `block_kind ∈ {needs_input, capability}` → P0（人已确认，2026-11-05）；其余 `blocked` → P1；`review` 且有未完成子卡 → P0；其余 `review` → P2；其它状态不通知。
- P0 在 5s 轮询内发桌面通知（10s 内）并右上角置顶；同一事件 30 分钟未处理只升级一次（P1→P0；P0 再提醒一次），之后不再重复。
- P1 攒批：3 条或最早 30 分钟一起推；P2 每日 08:00 一行（当天最多一次）。
- 通知动作：review 卡 `accept`/`reject`（理由必填）/`remind`（10 分钟）；非 review 卡只 `remind` + 打开看板（人已确认，2026-11-05）。accept/reject 直接调用 `verify`，不新增状态写路径。
- review 统一显示「还有 Xh Ym 自动收」；验收槽固定 3 束，同一次 decompose 的兄弟卡（共享 parent）成一束，槽满排队。
- 只在独立版 8787 提供；UI 5s 轮询，不引入 WebSocket；桌面通道只有 `notify-send`，手机通道仅在 payload 预留 `channels`。
- 新端点只加在 `/api/workbench/*`；通知引擎内存态，事件 id 稳定；通知失败不影响快照与其它功能。
- 验收槽只约束通知中心展示/推送，不改 2.1 全屏验收队列的处理能力。

**Never:**
- 不写 `kanban.db`、不改 `plugin_api.py`；不新增上游状态迁移。
- 不在插件宿主渲染通知中心；不通知 `ready`/`running`/`done`。
- 不做 WebSocket、手机推送、通知历史持久化、通知内直接改子卡。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| P0 blocked | `blocked` + needs_input/capability，或未完成子卡 | 快照 p0 + 桌面一次 + 右上角置顶；非 review 只给 remind | 通知命令缺失 → `desktop.last_error`，快照仍可用 |
| P1 blocked | 其余 blocked（dependency/transient/未分类） | 进 p1 面板；3 条或 30 分钟一次批量桌面 | 批量内容截断，不逐条刷屏 |
| P2 review | review 无未完成子卡 | 进 p2 日报；倒计时；槽位/排队 | 当天只发一次日报 |
| 30 分钟未处理 | P1/P0 仍在候选集 | P1 升 P0；P0 再提醒一次；只一次 | 已升级/已处理不重复 |
| 通知内收 | review + accept | 卡 done，通知移除 | verify 失败保留通知并回传错误 |
| 通知内不收 | review + reject 无理由 | 400 `reason_required` | 前端不提交 |
| 10 分钟后再提醒 | 任意 P0/P1 | snooze，到期下一轮再通知 | P2/未知动作 → 400 |
| 上游不可用 | board/details 失败 | sweep 502；保留上一份快照 | 不谎报、不重复通知 |
| 插件宿主 | 9119 | 无通知中心、无轮询 | N/A |

</frozen-after-approval>

## Code Map

- `workbench-app/notifications.py` — 新增：`NotificationEngine`（分类、事件 id、P0/P1/P2、批量、升级、snooze、日报、验收槽 bundling、快照、动作、`notify-send` 调用）；线程安全，时钟/命令可注入。
- `workbench-app/server.py` — 新增 `service.board_snapshot()`；`_inbox_item` 增加 `parent_ids`；创建 engine + 后台 poller；`GET /api/workbench/notifications`、`POST /api/workbench/notifications/sweep`、`POST /api/workbench/notifications/{id}/action`；env 开关。
- `workbench-app/tests/test_verify.py` — 扩展 FakeKanban 的 board/detail 字段（block_kind/link_counts/progress/links），保持既有 34 项通过。
- `workbench-app/tests/test_notifications.py` — 新增：分类、P0 通知、升级、P1 批量、P2 日报、槽/兄弟卡、倒计时、动作、snooze、通知命令失败、后台 10s 内。
- `workbench-ui/src/api/workbench.ts` — 通知类型 + `getNotifications` / `sweepNotifications` / `notificationAction`；错误码。
- `workbench-ui/src/notify/NotificationCenter.tsx` — 新增：P0 置顶卡、P1 面板、P2 日报、倒计时、动作、验收槽摘要；5s 轮询。
- `workbench-ui/src/styles/notify.css` — 新增：右上角固定层、置顶/面板/明暗 token、窄屏。
- `workbench-ui/src/App.tsx` — 独立版挂载通知中心；保留 2.1 验收入口；动作后刷新计数。
- `workbench-ui/scripts/notification-probe.mjs` — 新增 headless 探针；`package.json` 加 `probe:notify`。
- `workbench-app/README.md` — 端点、env、分类规则、通知命令、动作。
- 不改：`plugin_api.py`、`kanban.db`、`workbench-app/web/` 手改产物、`docs/学习手册-工作台架构.md`。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/notifications.py` -- 引擎与分类/批量/升级/日报/槽/动作/桌面通知 -- 通知核心
- [x] `workbench-app/server.py` -- board_snapshot、parent_ids、engine/poller、三个端点、env -- 接入
- [x] `workbench-app/tests/test_verify.py` -- FakeKanban 增加 board 字段与 links -- 共享夹具
- [x] `workbench-app/tests/test_notifications.py` -- 上述场景的后端回归 -- 测试
- [x] `workbench-ui/src/api/workbench.ts` -- 通知客户端与错误码 -- 前端调用
- [x] `workbench-ui/src/notify/NotificationCenter.tsx` -- 置顶/面板/动作/倒计时/槽 -- UI
- [x] `workbench-ui/src/styles/notify.css` -- 右上角固定层与明暗 token -- 视觉
- [x] `workbench-ui/src/App.tsx` -- 独立版挂载与刷新 -- 入口
- [x] `workbench-ui/scripts/notification-probe.mjs` + `package.json` -- headless 探针与脚本 -- UI 回归
- [x] `workbench-app/README.md` -- 端点/env/分类/动作 -- 文档
- [x] `workbench-ui` -- typecheck + build + distribute -- 双宿主产物
- [x] `workbench-app/server.py` -- 重启 + `/healthz` -- 不破坏现有

**Acceptance Criteria:**
- Given blocked needs_input 或未完成子卡, when 5s 轮询/sweep, then 桌面通知一次且右上角置顶，非 review 只显示提醒动作
- Given 同一 P0/P1 30 分钟未处理, when 再次采集, then 恰好一次升级/再提醒，之后不再重复
- Given 3 条 P1, when 采集, then 一条批量桌面通知；不足 3 条且未到 30 分钟不推
- Given review 卡, when 打开通知中心, then 显示倒计时、验收槽 3 束（兄弟卡同束）与排队数；P2 日报当天一次
- Given review 通知, when 收/不收, then 复用 verify 语义（不收理由必填）；remind 后 10 分钟再提醒
- Given notify-send 不可用, when 采集, then 快照与动作仍可用，`desktop.last_error` 可见
- Given 插件宿主, when 打开, then 无通知中心/轮询
- Given 改动完成, when 跑现有后端测试/探针与新增探针, then 全绿；`/healthz` → `{"ok":true}`

## Implementation Notes

- 2026-11-05 review 修复：通知的 parent ids 只来自 `server.board_snapshot()`
  （review 卡经 `GET /tasks/{id}` 的 `links.parents` 填充）；`_inbox_item` 不再
  携带 `parent_ids`，`failed_ids` 与 board slice 的未用字段一并移除。

- 2026-11-05: P1 的 30 分钟规则落在“升级”上：到期事件先升 P0 并发一条 critical 升级推送，再处理 3 条攒批；升级每事件只一次。P0/P1 的 30 分钟从 `first_seen_at` 起算。P1 的 remind 使用 normal 文案，不借用 P0 的 critical。
- 2026-11-05: 按 I/O 矩阵，remind 只对 P0/P1 开放；P2 review 卡只提供 accept/reject（倒计时替代未读）。
- 2026-11-05: 通知快照是内存态；`GET /notifications` 纯读，采集走 guarded `POST /notifications/sweep` + 后台 5s poller；上游失败返回 502 并保留上一份快照。
- 2026-11-05 review patch: 发送失败按事件 + 通道两级 60s 退避，一次采集最多触发一次失败命令；P0/P1 升级/批量/日报只在发送成功后落状态。补丁后 140 后端测试与 115/31/22 探针全绿。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| EC1 | `WORKBENCH_NOTIFY_COMMAND` 引号不配对时 `shlex.split` 抛 `ValueError`，`create_app` 直接失败 | medium | patch | `notifications.py` 的 `_resolve_command` 无异常处理；改为捕获后标记桌面通道不可用，`_send` 空命令直接失败并写 `last_error`。 |
| EC2 | notify-send 失败/超时仍设置 `notified_at` 与 `_last_daily_date`，首推丢失、日报当天不再补 | medium | patch | `_send` 返回 bool 但调用方忽略；改为成功后才落状态，并加 60s 退避避免每 5s 重试。 |
| EC3 | `_dispatch` 在引擎锁内同步 `notify-send`，P0 突发可让快照/动作等待 N×5s | medium | patch | 退避把突发收敛为一次超时；单次 `_send` 仍可能占锁至 notify 超时，记录为已知上限。 |
| EC4 | 首次快照后 UI 轮询失败仍显示旧数据且无提示 | medium | patch | `NotificationCenter` 仅在无快照时 setError；改为失败始终提示并保留旧数据。 |
| BH1 | P1 30 分钟攒批与 P1→P0 升级冲突；review remind 仅 P0 与 Always 文本不一致；Spec Change Log 为空 | false | — | 矩阵明确“30 分钟未处理 P1 升 P0”与“remind 任意 P0/P1”；实现以一次 group escalation push 满足 30 分钟“一起推”，P2 review 不给 remind；`batch_seconds < escalate_seconds` 分支测试在 BH15 补齐。 |
| BH2 | 发送失败被当作成功 | medium | patch | 同 EC2。 |
| BH3 | 重启后 P0 重推、P1 重新计时、P2 可能重发日报 | low | patch | 内存态是 spec 设计；README 补重启语义即可，不持久化。 |
| BH4 | `failed_ids` 是死字段；单卡详情失败让整轮 502 并沉默无关 P0 | low | patch | 移除 `failed_ids`；502 保留是矩阵“board/details 失败 → 502”的明确行为，不做逐卡降级。 |
| BH5 | item `channels` 与 root 形状不一致，且 item 硬编码 `notify-send` | low | patch | 删 item.channels，root 用实际命令；同步前端类型与测试。 |
| BH6 | `board_snapshot` 产出多个未消费字段；`_inbox_item.parent_ids` 未被使用 | low | patch | 删除未用字段；parent_ids 由 `board_snapshot` 提供；现有集成测试已覆盖真实 board→collect 契约。 |
| BH7 | accept/reject 未绑定 review 轮次，陈旧通知可能处理新一轮 | medium | patch | `verify` 增加 `expected_review_at` 比对，返回 409 `stale_review`；引擎传 `review_requested_at`；补测试与 UI 处理。 |
| BH8 | P0 突发在锁内同步发送 | medium | patch | 同 EC3。 |
| BH9 | 后台 poller 忽略 502；快照无采集新鲜度；UI 首次 sweep 失败被吞 | medium | patch | poller 记录非 200；快照加 `sweep.last_at/last_error`；UI 显示采集错误。 |
| BH10 | 刷新按钮只 GET；poller 关闭时不再采集；5s interval 可堆叠 | medium | patch | 刷新按钮执行 sweep+GET；refresh 加 in-flight guard；错误可见。 |
| BH11 | review 通知不能打开验收页；P0 review 只显示倒计时不显示原因 | low | patch | App 传 `onOpenVerify`；review 卡加“去验收”；meta 同时显示 reason 与倒计时。 |
| BH12 | `not_found` 落通用错误；busy 时其他卡按钮静默丢弃 | low | patch | 处理 `not_found` 刷新；busy 时禁用全部动作按钮。 |
| BH13 | 无 aria-live；空中心隐藏 desktop error | low | patch | counts 加 `role=status aria-live=polite`；有 desktop/sweep error 时也渲染中心。 |
| BH14 | 探针 “48h” 断言空转、插件宿主固定 6s、缺 blocked+未完成子卡真板检查 | medium | patch | 改按 `auto_collect_in_seconds` 校验倒计时；等待 11s；补 blocked+child P0 检查。 |
| BH15 | 缺多事件升级组、batch_seconds<escalate、截断、槽晋升、失败退避、阈值 env、5s poller、stale review、日报失败测试 | medium | patch | 全部补入 `test_notifications.py`。 |
| BH16 | README 环境变量自相矛盾；快照示例缺 `daily.count/hour` | low | patch | 明确 sweep 例外；补示例字段与重启语义。 |
| BH17 | `npm run verify` 未包含 `probe:notify` | medium | patch | package.json 加 `probe:notify`；README 同步。 |
| BH18 | spec `in-review` 而 sprint-status 仍 `in-progress` | low | patch | 已改 sprint-status 为 `review`。 |
| BH19 | `_review_ids_on_board` 与 `board_snapshot` 重复走列 | low | patch | 抽 `_iter_board_cards` 共用。 |
| VG1 | `npm run verify` 漏 `probe:notify` | medium | patch | 同 BH17。 |
| VG2 | 5s 刷新无探针覆盖 | medium | patch | 挂载后新增 review 卡 + 外部 sweep，等 5s 轮询出现。 |
| VG3 | 通知阈值 env 与默认 5s poller 无测试 | medium | patch | 加 env threshold 断言 + 5s poller 集成测试。 |
| VG4 | 非零退出分支无测试 | medium | patch | 加 failing logger 测试（last_error、last_sent_at、退避一次调用）。 |
| VG5 | desktop.last_error 面板无探针覆盖 | medium | patch | 探针加 flag 失败阶段，断言 `.wb-notify-desktop-error`。 |
| VG6 | sweep 与 action 并发无 guard 测试 | defer | defer | `notification_lock` 已覆盖；窗口为一次 board 读取，下一轮自愈；记入 deferred-work。 |
| VGX | 现有 todo/verify 探针会真的发桌面通知 | medium | patch | 两个探针设 `WORKBENCH_NOTIFY_COMMAND=true`。 |

**结论：** 无 intent_gap / bad_spec；BH1 判定误报（矩阵优先于 Always 的概括），VG6 按审查建议 defer，其余 28 条定向 patch。补丁后重跑 140 后端测试（39+38+29+34）、三套探针（115+31+22）、构建与隔离 healthz；全部通过。

## Design Notes

- Event id: `n:{task_id}:review:{review_requested_at}` / `n:{task_id}:blocked:{block_kind}:{block_recurrences}`；同 id 已 resolved 后再次出现视为新事件，重置通知/升级/snooze。
- P1 batch: 待推集合达到 `batch_size` 或最早 `batch_seconds`；P1 到期优先升 P0，再推剩余批次。
- 状态每轮全量重算：离开候选集 → resolved，从列表移除；snooze 期间不发通知但仍展示。
- 验收槽 bundling：parent 集合相同的 review 卡成束；无 parent 各自成束；取前 3 束，其余 queued。
- 快照 pure read；采集走 guarded POST + 后台 5s；测试用 env 关闭后台并缩短阈值。
- `notify-send` 参数：`--app-name=Jarvis 工作台 --urgency=<critical|normal|low>`；命令用 `WORKBENCH_NOTIFY_COMMAND` 覆盖（测试用 logger 脚本）。

## Verification

**Commands:**
- `python3 workbench-app/tests/test_notifications.py -v` -- 全绿
- `python3 workbench-app/tests/test_verify.py` / `test_dispatch.py` / `test_todo.py` -- 回归全绿
- `cd workbench-ui && npm run typecheck && npm run build` -- 类型检查 + 双产物
- `node workbench-ui/scripts/notification-probe.mjs` -- 通知中心探针全绿
- `node workbench-ui/scripts/verify-probe.mjs` / `todo-probe.mjs` -- 既有探针全绿
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`

**Manual checks:**
- 8787：P0 置顶、动作、倒计时、槽位；明暗主题；插件宿主无通知中心