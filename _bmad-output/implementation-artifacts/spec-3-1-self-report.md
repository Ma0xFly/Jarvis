---
title: '3.1 验收页自述三行'
type: 'feature'
created: '2026-11-06'
status: 'done'
route: 'dispatch'
baseline_commit: '3c244389a294090dcd94c387fe9d465164af2eed'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 验收页只有 worker 的 `summary`，没有固定的「交付 / 依据 / 没底」三行，用户容易被总结措辞锚定，也看不到 worker 自己承认的不确定处。

**Approach:** worker 在 `kanban_request_review` metadata 写 `self_report = {delivered, evidence, uncertain}`，Hermes 落到最新 `task_runs.metadata`；验收页在样张之后、验收标准右侧渲染三行；缺失或畸形只警示，不阻断 A/R/S。

## Boundaries & Constraints

**Always:** 只读最新 run 的 `metadata.self_report`；三行都是字符串；`uncertain` 为空必须单独警示；缺失/畸形降级展示，不 500；保持样张在前、A/R/S 键位与队列语义不变。

**Never:** 不因自述缺失阻断 accept；不从 `latest_summary` 伪造；不新建表或改上游 schema；不写第二份真源到 workbench.db。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 完整自述 | 最新 run 有三个字符串字段 | 右侧按交付/依据/没底顺序显示，没底强调 | N/A |
| 没底为空 | `uncertain` 空白 | 显示「未填写」警示，仍可 A/R/S | 不阻断 |
| 缺失/畸形 | 无 `self_report` 或字段非字符串 | 显示「自述缺失」，不伪造、不 500 | 不阻断 |
| 多轮/旧卡 | 最新 run 无自述、旧 run 有 | 只读最新 run，显示缺失警示 | 不回溯猜测 |

</frozen-after-approval>

## Code Map

- `workbench-app/server.py` -- `_inbox_item`（1287）读 `detail["runs"]` 最新 run 的 `metadata.self_report`，归一化后挂到 item；不改 `_task_detail`。
- `workbench-ui/src/api/workbench.ts` -- `InboxTask`（477）加 `self_report` 类型。
- `workbench-ui/src/verify/VerifyPage.tsx` -- 右侧 `wb-verify-accept` 在验收标准后插入自述块；左侧 summary/产出物不动。
- `workbench-ui/src/styles/verify.css` -- 自述行/标签/没底警示样式，走现有 token。
- `workbench-app/tests/test_verify.py` -- FakeKanban detail 的 runs metadata 覆盖完整/缺失/畸形/没底为空。
- `workbench-ui/scripts/verify-probe.mjs` -- seed 带 self_report 的 review 卡，断言三行与缺失态。
- `/home/myx/.hermes/skills/workbench-conventions/SKILL.md` -- 补 worker self_report 契约；先备份，不改 Hermes 内核。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- latest-run self_report 归一化挂到 inbox item -- 数据源
- [x] `workbench-ui/src/api/workbench.ts` -- 加 `SelfReport` / `InboxTask.self_report` -- 前端契约
- [x] `workbench-ui/src/verify/VerifyPage.tsx` + `styles/verify.css` -- 三行/缺失态 -- UI
- [x] `workbench-app/tests/test_verify.py` -- 完整/缺失/畸形/没底为空 -- 回归
- [x] `workbench-ui/scripts/verify-probe.mjs` -- UI 三行/缺失态 -- 探针
- [x] `/home/myx/.hermes/skills/workbench-conventions/SKILL.md` -- worker 契约 + 备份 -- 跨系统
- [x] `workbench-ui` -- `npm run build` 分发 -- 双宿主

**Acceptance Criteria:**
- Given 最新 run 有完整 `self_report`，when 打开验收，then 样张在前，右侧显示交付/依据/没底三行。
- Given `uncertain` 为空，when 打开，then 显示「未填写」警示，A/R/S 仍可用。
- Given `self_report` 缺失或畸形，when 打开，then 显示「自述缺失」，不 500、不伪造。
- Given worker 按契约写 `kanban_request_review`，when 工作台读详情，then 最新 run metadata 可读到三行。
- Given 改动完成，when 跑 test_verify + verify-probe + build，then 全绿，`/healthz` ok。

## Implementation Notes

- 2026-11-06：`python3 workbench-app/tests/test_verify.py` 39 项、`npm run typecheck`、`npm run probe:verify` 36 项、`/healthz` 全绿；`workbench-conventions` 已备份并补 self_report 契约，Hermes 内核未改。
- Review diff 收窄：排除 `workbench-app/web/` 的 minified 构建产物、sprint-status/epic context，包含产品源码、测试、探针与 worker skill diff；构建产物由 build/probe 覆盖。
- Review patch 已应用（14 条 patch，1 条上游 tool schema defer）；复审后 `test_verify` 40、`probe:verify` 36、typecheck/build、`/healthz` 全绿。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1 | 页面有三个“自述”标签，分别来自 `latest_summary`、最新 run `self_report`、最新 run `summary`，可能显示不同轮次 | medium | patch | `VerifyPage.tsx:615,680,728`；左侧标签仍是“本轮自述”，与 Design Notes 的“运行摘要”不一致 |
| BH2 | 有 `self_report` 但无 summary 时，左侧写“worker 没有留下自述”，与右侧自述矛盾 | medium | patch | `VerifyPage.tsx:619` |
| BH3 | 三个字段都为空字符串仍被当成完整自述，UI 显示 `—/—/未填写` 且没有“自述缺失” | medium | patch | `server.py:1287-1314`；`VerifyPage.tsx:684-694` |
| BH4 | UI `latestRun` 只按 `started_at` 排序，服务端按 `(started_at,id)`；同时间戳的 synthesized run 可能显示另一轮 | medium | patch | `VerifyPage.tsx:140-143` vs `server.py:1953-1962` |
| BH5 | 探针用全局 `.wb-verify-self` 选择器，未断言自述在右侧、验收标准之后 | medium | patch | `verify-probe.mjs:556-570` |
| BH6 | 测试未覆盖“最新 run 畸形、旧 run 有效”、`metadata` 非 dict、同时间戳 tie-break | medium | patch | `test_verify.py:756-812` |
| BH7 | 测试用 dashboard PATCH 模拟 worker entry，未走 `kanban_request_review` 工具（差异是 worker_session/redaction） | low | patch | `test_verify.py:586-604`；`verify-probe.mjs:344-352` |
| BH8 | `.wb-verify-self` 与 `.wb-verify-run` 的 grid 规则几乎重复 | low | patch | `verify.css:175-189` vs `:268-282` |
| BH9 | `_item` 每个 case 启一个 workbench server，`addCleanup` 延迟 stop，六个 case 并发持有 server | low | patch | `test_verify.py:701-712,756-782` |
| BH10 | `fake_run` 的 `task_id` 参数无调用方使用 | low | patch | `test_verify.py:114-133` |
| BH11 | README inbox 示例未展示 `self_report` / `null` 语义 | medium | patch | `workbench-app/README.md:165-174` |
| BH12a | worker 契约只在机器本地 skill，仓库内不可复现/发现 | medium | patch | `/home/myx/.hermes/skills/workbench-conventions/SKILL.md`；仓库无 pointer |
| BH12b | 上游 tool schema 的 `metadata` 描述仍只列 changed_files/tests_run/commit/decisions | low | defer | `~/.hermes/hermes-agent/tools/kanban_tools_schemas.py:242-249`；改上游超出本 story 边界 |
| VG1 | 「没底」强调只断言了类名，未断言 computed style；删 CSS 仍 36/36 通过 | medium | patch | `verify-probe.mjs:556-569`；`verify.css:190-193` |
| VG2 | 右侧摆放未断言；把块移到左侧/验收标准前仍 36/36 通过 | medium | patch | `verify-probe.mjs:557-558,638,789-792`；`verify.css:91-97` |

**分组与路由：** G1 标签/空态（BH1/BH2）；G2 空字段（BH3）；G3 latest-run 排序与测试（BH4/BH6）；G4 探针摆放/强调（BH5/VG1/VG2）；G5 测试质量（BH6/BH9/BH10）；G6 文档/契约（BH7/BH11/BH12a）；G7 CSS 复用（BH8）；G8 上游 tool schema（BH12b，defer）。无 intent_gap / bad_spec；其余全部 patch。

## Design Notes

- 自述放右侧验收标准之后，避免与左侧产出物互相锚定；左侧现有 summary 保留为运行摘要。
- 最新 run 按 `started_at`/`id` 倒序；只读该 run，不跨轮拼接。`self_report` 不参与 accept 闸门。

## Verification

**Commands:**
- `python3 workbench-app/tests/test_verify.py` -- 全绿
- `cd workbench-ui && npm run typecheck && npm run build` -- 类型 + 双产物
- `cd workbench-ui && npm run probe:verify` -- 三行/缺失态通过
- `systemctl --user restart jarvis-workbench`；`curl /healthz` -- `{"ok":true}`