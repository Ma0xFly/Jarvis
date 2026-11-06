---
title: '3.2 固化登记与验收门禁'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
baseline_commit: 'bc20af8d7ddceb05bd0cb89dea9debce30c3fb03'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** review 卡可以被直接收下，即使 worker 没有登记任何可复用件；验收页也看不到这张卡沉淀了什么，完成卡只留下结果。

**Approach:** worker 在 `kanban_request_review` metadata 写 `solidified`（`{type,path,status}`，可单条或数组），Hermes 落到最新 `task_runs.metadata`；验收页展示类型/路径/状态，accept 前要求至少一条合法记录；缺失返回 400 `missing_solidified`。手动「标记已验证」写 workbench.db 的 `solidified_verified` overlay，不 PATCH 上游 run metadata；48h 自动收保持现状。

## Boundaries & Constraints

**Always:** 只读最新 run 的 `metadata.solidified`；合法 `type` 为 `routing|skill_patch|script`，`path` 非空，`status` 为 `pending|verified`；accept 门禁必须有至少一条合法记录；reject/skip 不受影响；手动 verified 写 workbench.db overlay，按 path 幂等；缺失/畸形降级，不 500。

**Never:** 不因固化缺失阻断 reject/skip；不 PATCH 上游 run metadata；不由工作台编辑 profile skill 文件；不改 Hermes 内核；不新建记忆库；不把 48h 自动收纳入门禁。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 合法固化 | 最新 run 有一条 valid `pending/verified` | 验收页显示类型/路径/状态；accept 允许 | N/A |
| 缺失/全非法 | 无 `solidified`、空数组、字段非法 | accept 返回 400 `missing_solidified`，卡留在 review；reject/skip 正常 | 不 500 |
| 手动 verified | pending 条目 + path | workbench `solidified_verified` 按 path 追加，重复点击幂等；UI 显示 verified | path 不在最新 run → 400 `missing_solidified` |
| 单条/数组/畸形 | object / list / 混合 | 归一化为列表，忽略非法项；全非法按缺失 | 不 500 |
| 48h 自动收 | 超时 review 卡 | 按现有规则收，不要求 solidified | 不改 |

</frozen-after-approval>

## Code Map

- `workbench-app/server.py` -- 复用 `_ordered_runs` 增加 `_latest_solidified(detail)`；`_inbox_item` 返回 `solidified`；`verify` 的 accept 分支加门禁；新增 `POST /api/workbench/solidified/{task_id}/verify` 写 workbench overlay。
- `workbench-app` metadata store -- `task_metadata.solidified_verified` 为 `{path, verified_at}[]`，用 `store.get/merge`；不新增表。
- `workbench-ui/src/api/workbench.ts` -- `SolidifiedEntry` / `SolidifiedVerifiedEntry`；`InboxTask.solidified`；`TaskMetadata.solidified_verified`；`verifyTask` 错误码 `missing_solidified`；新增 `markSolidifiedVerified`。
- `workbench-ui/src/verify/VerifyPage.tsx` -- 右侧自述之后渲染固化块；pending 条目「标记已验证」；accept 缺失时显示阻断原因；成功后本地更新队列 item 的 overlay。
- `workbench-ui/src/styles/verify.css` -- 固化块/状态/按钮样式，走现有 token。
- `workbench-app/tests/test_verify.py` -- 归一化、accept 门禁、overlay 幂等/path 缺失、48h 不变、reject/skip 不受影响。
- `workbench-app/tests/test_notifications.py` -- accept 门禁共用路径的 review 夹具。
- `workbench-ui/scripts/verify-probe.mjs` -- seed 合法/缺失/单条/数组/上游 verified；断言展示、accept 阻断、手动 verified。
- `workbench-ui/scripts/notification-probe.mjs` / `session-probe.mjs` -- review 夹具补合法 solidified；notification probe 覆盖缺固化阻断。
- `/home/myx/.hermes/skills/workbench-conventions/SKILL.md` + `workbench-app/README.md` -- 补 `solidified` 契约；skill 先备份，README 保留 repo 内 pointer。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- `_latest_solidified` 归一化挂到 inbox item -- 数据源
- [x] `workbench-app/server.py` -- accept 前门禁与 `missing_solidified` -- 闸门
- [x] `workbench-app/server.py` -- `POST /api/workbench/solidified/{task_id}/verify` + overlay 合并 -- 手动 verified
- [x] `workbench-ui/src/api/workbench.ts` -- 类型、错误码、client -- 前端契约
- [x] `workbench-ui/src/verify/VerifyPage.tsx` + `styles/verify.css` -- 固化块/阻断/按钮 -- UI
- [x] `workbench-app/tests/test_verify.py` -- 归一化/门禁/overlay/回归 -- 后端测试
- [x] `workbench-app/tests/test_notifications.py` -- accept 门禁共用路径夹具 -- 回归
- [x] `workbench-ui/scripts/verify-probe.mjs` -- 展示/阻断/verified -- UI 探针
- [x] `workbench-ui/scripts/notification-probe.mjs` + `session-probe.mjs` -- 共用门禁/缺固化阻断 -- UI 探针
- [x] worker skill + README -- `solidified` 契约与 pointer -- 跨系统
- [x] `workbench-ui` -- `npm run build` 分发 -- 双宿主

**Acceptance Criteria:**
- Given 最新 run 有一条合法 solidified，when 打开验收，then 显示类型/路径/状态，accept 可提交。
- Given 无合法 solidified，when 按 A 或调 accept，then 返回 400 `missing_solidified`，卡仍在 review；reject/skip 不受影响。
- Given pending 条目，when 点「标记已验证」，then workbench overlay 记录 verified，UI 刷新且重复点击幂等。
- Given solidified 为单条/数组/混合，when 读取，then 归一化为合法列表；全非法按缺失，不 500。
- Given worker 按契约写 `kanban_request_review`，when 工作台读最新 run，then 能读到 solidified。
- Given 48h 超时 review 卡，when 自动收，then 行为与现状一致。
- Given 改动完成，when 跑 test_verify + test_notifications + verify-probe + notify/session probes + build，then 全绿，`/healthz` ok。

## Implementation Notes

- 2026-11-06：`test_verify` 55、`test_notifications` 39、`test_dispatch` 38、`test_todo` 39、`test_session` 27；`probe:verify` 44；typecheck/build、`/healthz` 全绿。
- `workbench-conventions` 已备份为 `SKILL.md.bak-20261106-3.2` 并补 solidified 契约；Hermes 内核未改；README 有 repo 内 pointer。
- 通知/会话/待办探针 fixture 已补合法 solidified，确保共享 accept 门禁不误伤。
- 2026-11-06 review round：`test_verify` 66、`test_notifications` 39、`probe:verify` 45、`probe:notify` 29；typecheck/build 全绿。overlay NaN/Infinity 降级、solidified cap/dedupe/path 上限、mark review 守卫、UI overlay 轮询合并、通知缺固化文案均已修复；SKILL 二次备份为 `SKILL.md.bak-20261106-3.2-review`。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| E1/BH5 | overlay 的 `verified_at` 为 NaN/Infinity 时 `int()` 抛错，mark 端点 500 | medium | patch | `server.py:1374-1394`；`_is_number` 已有 `math.isfinite` 可复用 |
| E2 | UI 对非数组 `solidified_verified` 调 `.map` 会崩 | low | patch | `VerifyPage.tsx:541-543`；metadata 可被外部污染 |
| E3 | mark 成功后 5s 轮询可能用旧 inbox 覆盖本地 overlay，verified 回退 | medium | patch | `VerifyPage.tsx:416-425`；`syncQueue` 无 merge/sequence guard |
| E4/BH4 | overlay 只按 path 幂等，rework 后同一 path 直接显示 verified；不可撤销、旧 path 不清 | medium | defer | `server.py:2052-2072`；spec Design Notes 明确 path 幂等，属已选设计的已知边界 |
| E5/BH1/VG2 | 通知中心 accept 未处理 `missing_solidified`，只显示原始错误码 | medium | patch | `NotificationCenter.tsx:163-185`；verify 页已有本地化分支 |
| E6 | worker 可提交超大/重复 solidified 数组，inbox/DOM 随之上长 | low | patch | `server.py:1326-1355`；无 cap/dedupe |
| BH2 | README 通知动作文档未列 `missing_solidified` | low | patch | `README.md:353-366` |
| BH3 | worker 可写 `status:"verified"` 自证，绕过人工 verified | medium | patch | `SKILL.md:62`；FR-15 要求 worker 登记 pending，verified 应只由工作台/后续引用产生 |
| BH6 | 非法 solidified 被静默丢弃，UI 仍说“没有登记” | low | patch | `server.py:1326-1355`；`VerifyPage.tsx:799-801` |
| BH7 | `path` 无长度/相对路径约束，worker 可写绝对/`..`/虚构路径 | low | patch | `server.py:1348-1349`；path 仅展示，但应有上限并注明 opaque |
| BH8 | mark 端点不检查当前是否 review，done/triage 卡也能写 overlay | medium | patch | `server.py:2024-2077` vs verify 的 review 校验 |
| BH9 | “让 worker 补登记 / 刷新后重试”在 review 交接后不可执行 | medium | patch | `VerifyPage.tsx:396-397,799-801`；实际恢复是不收/打回或等自动收 |
| BH10 | accept 只要求登记、不要求 verified，pending 可被收且之后无 UI 补 verified | — | false | spec I/O/AC 明确 `pending/verified` 均可 accept；手动 verified 是可选审计，不是硬门槛 |
| BH11 | mark 端点缺 502/503、非字符串 path、上游失败测试 | medium | patch | `test_verify.py:1295-1394`；README 承诺 502/503 |
| BH12 | 无真实 `request_review` 写入 solidified 的落点测试 | medium | patch | `test_verify.py:607-628` 只测 self_report |
| BH13 | accept 门禁未测“旧 run 有、最新 run 缺失/非法” | medium | patch | `test_verify.py:921-1099` 只测展示 |
| BH14 | spec Verification/Code Map 未列 test_notifications、probe:notify、probe:session | low | patch | `spec-3-2:82-86`；这些 fixture 已改 |
| BH15 | spec 写“追加/更新”，实现是追加幂等；文件缺尾换行 | low | patch | `spec-3-2:32`；`server.py:2063-2066` |
| BH16 | spec status in-review，sprint-status 仍 in-progress | — | false | 本轮 step-04 结束时才同步 review，当前状态正确 |
| BH17 | review diff 未含构建产物/status/docs | — | false | 构建产物由 build/probe 覆盖；docs 是用户既有未提交修改，story 不触碰 |
| BH18 | `docs/学习手册` 有拼写/过期固化描述 | — | false | 用户既有未提交文件，超出本 story 边界；不修改 |
| BH19 | solidified 枚举在 server/UI/README/SKILL/spec 五处重复 | low | defer | 无跨语言 schema；后续可加 cross-check 测试 |
| VG1 | 未测 verified-only 登记能否 accept | medium | patch | `test_verify.py` accept 成功用例全是 pending |
| VG3 | 多个 pending 条目时只禁用当前 path，其他按钮点击被静默丢弃 | low | patch | `VerifyPage.tsx:757-793`；`markVerified` 在 markingPath 时 early return |

**分组与路由：** G1 overlay 健壮性（E1/E2/E3/E6/BH5/BH7）；G2 通知面（E5/BH1/BH2/VG2）；G3 契约/文案（BH3/BH6/BH9/BH15）；G4 mark 端点状态与测试（BH8/BH11/BH12/BH13/VG1）；G5 规格/文档（BH14/BH15）；G6 UI 并发（E3/VG3）。defer：E4/BH4（path 幂等 rework 限制）、BH19（跨语言枚举）。false：BH10/BH16/BH17/BH18。无 intent_gap / bad_spec；其余 patch。

## Design Notes

- 固化块放右侧自述之后；上游 status 与 workbench overlay 合并显示，overlay 只负责「已验证」判断。
- accept 门禁只读上游最新 run；48h 自动收是队列保底，不走该门禁。
- 手动 verified 不改变上游 pending 记录，保留原始登记；path 作为幂等键。

## Verification

**Commands:**
- `python3 workbench-app/tests/test_verify.py` -- 全绿
- `python3 workbench-app/tests/test_notifications.py` -- 全绿（共用 accept 门禁）
- `cd workbench-ui && npm run typecheck && npm run build` -- 类型 + 双产物
- `cd workbench-ui && npm run probe:verify` -- 展示/阻断/verified 通过
- `cd workbench-ui && npm run probe:notify && npm run probe:session` -- 共用门禁回归
- `systemctl --user restart jarvis-workbench`；`curl /healthz` -- `{"ok":true}`
