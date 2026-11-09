---
title: '3.5 其余适配器与路由切换'
type: 'feature'
created: '2026-11-09'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
baseline_commit: 'ac891b1d37fb97157eea0cf8e2e32382c2febade'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 3.4 只有 `claude_code` 适配器；`dsh` / `codex` 未注册，`hermes` 没有 in-process 语义，coder profile 仍写死 dsh。`routing.json` 不存在，执行器选择只靠 skill 里的文字表；会话抽屉只显示 `executor_session_id`，没有复制/续跑入口。

**Approach:** 增加 `dsh` / `codex` 适配器，把 `hermes` 注册为 `mode: in-process` 并让 CLI 返回 4；新建 `routing.json` 与 `bin/route` 解析器，worker 只认 routing 的 `executor`；更新 coder SOUL/profile 与 worker skill；会话抽屉显示执行器 id 并提供 `--resume` 复制入口。

## Boundaries & Constraints

**Always:** 适配器只做参数/输出翻译；`dsh` 的 `--resume` 映射 `--session-id`，从 NDJSON 首个 `session` 事件取 `session-<uuid>`；`codex` 解析 `codex exec --json` 的 JSONL；`hermes` 的 CLI 调用必须返回 4 并说明其在 worker 进程内运行；routing 的 `executor` 只来自 `routing.json`，卡/事件 metadata 的同名字段一律忽略；无 `executor` 时 coder 默认 `dsh`、其它 profile 默认 `hermes`；UI 无 id 时不渲染执行器块；改外部 skill/profile 前先备份。

**Never:** 不做自动 fallback 链；不做 `--stream`；不做执行器管理页/插件；不做建卡入口的 assignee 路由（仍 deferred）；不实际启动续跑，只展示/复制；不改 `~/.hermes/hermes-agent/`；不写 `kanban.db`；不把任务书或密钥写进日志/argv。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| dsh 成功 | NDJSON 首行 `session`，末行 `final` | `ok=true`、`exit_code=0`，`session_id`/`summary`/tokens 映射 | 输出不可解析 → 1 |
| dsh 续跑 | `--resume session-…` | argv 含 `--session-id session-…`；session id 透传 | 无 session 事件 → `session_id=null` |
| dsh 不可用 | PATH 无 `dsh` | `exit_code=4` | 不启动子进程 |
| codex 成功 | JSONL `thread.started` + `item.completed(agent_message)` | `ok=true`、`session_id=thread_id`、summary 取最后一条 agent_message | 无识别事件 → 1 |
| codex 不可用 | PATH 无 `codex` | `exit_code=4` | 不启动子进程 |
| hermes CLI | registry `mode=in-process` | `exit_code=4` | 文案说明“worker 进程内运行”，不伪装成 CLI |
| routing 命中 | `routing.json` 的 coder 规则 `executor=claude_code` | `bin/route --profile coder` 返回 `claude_code`、`mode=cli` | 规则缺 `executor` → 默认 `dsh` |
| 卡覆盖 | `task.metadata.executor=dsh` | 被忽略，仍按 routing 选择 | 不提供覆盖参数 |
| UI 有/无 id | `executor_session_id` 有值/为 null | 有值显示 id + 复制 `--resume <id>`；无值不渲染块 | 剪贴板失败显示可见状态 |

</frozen-after-approval>

## Code Map

- `bin/executors/dsh.py` -- 新建：`build_command` 发 `dsh --profile headless --json [--session-id]`；`translate_result` 解析 NDJSON `session`/`status`/`final`，tokens 取 `step_end.usage`。
- `bin/executors/codex.py` -- 新建：`codex exec --json [--sandbox read-only|workspace-write]`；resume 用 `codex exec resume --json … <id> -`；解析 `thread.started`/`item.completed`/`turn.completed`。
- `bin/executors/hermes.py` -- 新建：仅作文档/防线标记，函数明确拒绝 CLI 调用；runner 在导入前就因 `mode=in-process` 返回 4。
- `bin/executor-run` -- `resolve_entry` 对 `in-process` 给专门错误；其余 CLI 行为不变。
- `executors.json` -- 注册 `dsh`、`codex`、`hermes`（`mode: in-process`）。
- `routing.json` -- 新建：`default.executor=hermes`；coder → `claude_code`；researcher/housekeeper/skeptic → `hermes`；规则带 `name`/`when`/`to`（可选 profile 回退）/`skills`/`budget`/`tell`。
- `bin/route` -- 新建：读 `routing.json` + `executors.json`，按 `--profile/--source/--type/--title` 匹配首条规则，输出末行 JSON（含 `mode` / `adapter_mode` / `rule_index` / `rule_name`）；不提供 `--executor` 覆盖；坏输入 fail-closed 返回 1。
- `tests/test_executor_run.py` -- 扩 fake binary harness：dsh NDJSON、codex JSONL、hermes E2E 4、缺二进制 4、依赖边界。
- `tests/test_routing.py` -- 新建：默认/规则/无 executor/关键词/未知执行器/坏 JSON/不接受覆盖参数。
- `workbench-ui/src/session/SessionViewer.tsx` -- 保留 `.wb-session-executor`/`.wb-session-id`，加 `.wb-session-copy` 与 `.wb-session-resume`。
- `workbench-ui/src/styles/session.css` -- 复制按钮、状态与 `--resume` 片段的样式。
- `workbench-ui/scripts/session-probe.mjs` -- 更新“续跑属 Epic 3”断言；补复制/片段/无 id 断言。
- `workbench-app/tests/test_session.py` -- 补 `executor_session_id` 非字符串/空白 → null。
- `workbench-app/README.md`、`_bmad-output/specs/spec-jarvis-workbench/ARCHITECTURE-SPINE.md` -- 适配器/路由/会话入口文档；routing 不再是 deferred。
- 外部 `/home/myx/.hermes/skills/workbench-conventions/SKILL.md`（实际 symlink 目标 `/home/myx/.config/magpie/library/skills/workbench-conventions/SKILL.md`）与 `references/executor-contract.md` -- 先备份，再改为 routing/适配器/回滚说明。
- 外部 `/home/myx/.hermes/profiles/coder/profile.yaml`、`SOUL.md` -- 先备份；coder 默认 `claude_code`、dsh 备用，写 `kanban_request_review` metadata。

## Tasks & Acceptance

**Execution:**
- [x] `bin/executors/dsh.py` -- 参数/NDJSON 翻译与五码映射 -- 备用执行器
- [x] `bin/executors/codex.py` -- 参数/JSONL 翻译与五码映射 -- 已装执行器
- [x] `bin/executors/hermes.py` + `bin/executor-run` -- in-process 标记与专门错误 -- 语义清晰
- [x] `executors.json` -- 注册三个执行器 -- registry
- [x] `routing.json` + `bin/route` -- 唯一可校验的 executor 规则源 -- 路由接线
- [x] `tests/test_executor_run.py`、`tests/test_routing.py` -- 覆盖 I/O 矩阵与路由边界 -- 回归
- [x] `workbench-ui/src/session/SessionViewer.tsx` + `session.css` + `session-probe.mjs` -- id/复制/续跑片段 -- 会话入口
- [x] `workbench-app/tests/test_session.py` -- 畸形 executor id 降级 -- 容错
- [x] 外部 skill/profile 备份并更新 -- coder 切到 claude_code，记录回滚 dsh 步骤 -- 跨系统前置
- [x] README/ARCHITECTURE-SPINE -- 文档对账 -- 可维护性

**Acceptance Criteria:**
- Given `executors.json` 注册 dsh/codex/hermes，when 分别执行或检查 registry，then dsh/codex 走 CLI、hermes 返回 4 并说明在 worker 进程内运行。
- Given `dsh --resume session-…`，when 运行适配器，then argv 含 `--session-id`，session id 来自 NDJSON 首个 `session` 事件，不伪造。
- Given fake `codex exec --json` 返回 `thread.started` + `agent_message`，when 运行，then `session_id`/`summary` 映射；缺二进制返回 4。
- Given `routing.json` 的 coder 规则，when `bin/route --profile coder`，then 返回 `claude_code`；规则缺 executor 返回 `dsh`；卡/事件 metadata 的 executor 不能覆盖。
- Given coder 收到编码任务，when 按 SOUL/profile 派发，then 用 `bin/executor-run --executor claude_code`；claude_code 不可用时按文档回滚 dsh，不伪装成功。
- Given 会话有 `executor_session_id`，when 打开抽屉，then 显示 id 与复制 `--resume <id>`；无 id 时不显示空块。
- Given 改动完成，when 跑 executor/routing/session 测试、UI build 与 session 探针，then 全绿。

## Implementation Notes

- 2026-11-09：新增 `bin/executors/dsh.py`（NDJSON `session` / `status.phase=step_end` / `final`）、`bin/executors/codex.py`（JSONL `thread.started` / `item.completed(agent_message)` / `turn.completed`）、`bin/executors/hermes.py`（in-process 防线）。`executors.json` 注册四执行器；`bin/executor-run` 对 `mode=in-process` 返回 4，summary 说明「在 worker 进程内运行」。
- `routing.json` + `bin/route`：文件顺序首条命中；`when` 支持 `profiles` / `sources` / `types` / `keywords`，缺省按 `to` 匹配 profile；规则缺 `executor` 时 coder → `dsh`、其它 profile → `hermes`，无规则命中用 `default.executor`；不接受 `--executor`；未知执行器、坏 JSON、缺失文件 fail-closed 返回 1。
- 会话抽屉保留 `.wb-session-executor` / `.wb-session-id`，新增 `.wb-session-copy` 与 `.wb-session-resume`；显示执行器名，复制片段为 shell 引号处理过的 `--executor <name> --resume <id>`；复制成功/失败都有可见状态并在 3s 后复位；无 id 不渲染执行器块。`session-probe.mjs` 30 项检查通过。
- 外部 `~/.hermes/skills/workbench-conventions`（symlink → magpie library）、coder `profile.yaml` / `SOUL.md` / `skills/dsh-delegation/SKILL.md` 均已先备份（`*.bak-20261109-3.5` 及 review 备份），再改为 routing 入口、claude_code 默认 / 单次 dsh 回滚与 `kanban_request_review` metadata；新增 `tests/test_worker_contract.py` 做跨系统契约回归（外部文件缺失时 skip）。
- 验证：`tests/test_executor_run.py` 130 项、`tests/test_routing.py` 39 项、`workbench-app/tests/test_session.py` 31 项、`tests/test_worker_contract.py` 2 项全绿；`py_compile`、`bin/route --profile coder --json`、`npm run build`、`npm run probe:session`（30 checks）通过。
- 2026-11-09 review patch：dsh/codex 的 `error`/`fatal`/`turn.failed` 事件即使退出 0 也强制 `1`；codex 成功必须至少一条 `item.completed(agent_message)`；非零退出 summary 保留退出码 + 首行 stderr（final/agent 文本只作详情）；`dsh --mode readonly` 与 `codex --mode readonly --resume` 明确拒绝。`bin/route` 增加 `rule_name` / `rule_index` / `adapter_mode`，`to` 返回实际 profile；未知 `when` 键、空 allowlist（匹配不到）、非有限预算、非字符串 `default.executor`、非法 registry `mode`、非 UTF-8 与无法展开的 `~user` 全部 fail-closed；session API 增加 `executor` 并随抽屉展示。
- 真实 dsh/codex 账号 smoke 未执行（本次约束为不运行远程操作）；fake binary 回归保留，不伪装成功。
- 已知限制：dsh headless 没有只读沙箱、codex `exec resume` 不能重设 `--sandbox`，所以两类只读请求会被拒绝而不是静默降级；dsh/codex 不支持 `--max-usd`，`cost.usd` 为 `null`；in-process hermes 的 exit 4 不是 fallback 信号。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| Edge/Blind | codex `turn.failed`/`error` 无 message、exit 0 时被当成功 | high | patch | 复现 `{"type":"turn.failed"}` -> `ok=true`；失败事件必须强制 `1`。 |
| Edge/Blind | dsh `error`/`fatal` 无 message、后面有 `final` 时被当成功 | high | patch | 复现 `error`+`final` -> `ok=true`；失败事件必须强制 `1`。 |
| Blind | codex 只有 `thread.started` 或 `turn.completed` 时返回成功 | high | patch | 复现截断流 -> `ok=true`/`执行完成`；成功必须至少一条 `item.completed(agent_message)`。 |
| Blind | dsh/codex 非零退出的 summary 优先 final/agent_message，掩盖真实错误 | medium | patch | 复现 `all done` + 非零退出 -> summary 无 exit/stderr；补退出码与 stderr 首行。 |
| Edge/Blind | route 读非 UTF-8 文件或 `~unknownuser` 时 traceback，无 JSON 末行 | medium | patch | 复现 `UnicodeDecodeError`/`RuntimeError`；必须 fail-closed 输出 JSON 并退出 1。 |
| Edge/Blind | route 的 `when` 未知键被忽略，拼错后变成 catch-all | high | patch | 复现 `when.profile` + `--profile researcher` 命中 codex；未知键必须 `routing_invalid`。 |
| Edge | route 的 `profiles`/`sources`/`types`/`keywords` 为空数组时被当未设置 | medium | patch | 空 allowlist 应 match nothing，不能退化成 catch-all。 |
| Edge | route 接受 `NaN`/`Infinity` 预算，输出非法 JSON | medium | patch | 复现 exit 0 输出 `NaN`；load 拒绝非有限常量，emit 用 `allow_nan=False`。 |
| Blind | route 的 `default.executor` 非字符串时静默回退 dsh | medium | patch | 复现 `{"executor":42}` -> dsh；应 `routing_invalid`。 |
| Blind | route 把非字符串 registry `mode` 强转 cli，executor-run 却返回 4 | medium | patch | 两处校验必须一致；非 `cli`/`in-process` 或非字符串应报错。 |
| Blind/VG | dsh/codex 忽略 `--max-usd`，coder 的 `max_cost` 在回滚时失效 | medium | patch | routing coder 有 `max_cost: 2`；适配器不支持 USD 上限，需在 README/SOUL 说明并在 dsh 调用中省略。 |
| Blind | dsh/codex 的 `cost.usd` 恒为 null，与“适配器上报费用”不一致 | low | patch | 两家 CLI 只稳定提供 tokens；契约/README 明确 USD 未知，留后续成本故事。 |
| Blind | coder SOUL 第 4 步只对 exit 4 回滚，边界却说 2/3/4 都 blocked | medium | patch | 改为 2/3 转 blocked、4 才允许一次 dsh fallback，禁止 fallback 链。 |
| Blind | dsh 忽略 `--mode readonly`；codex `resume` 不能重设 sandbox | high | patch | 只读任务回滚 dsh 会可写；dsh readonly 与 codex readonly+resume 必须拒绝并文档化。 |
| Blind | route 只按 profile 调用，`when.sources/types/keywords` 不可达；`skills`/`tell`/缺 budget 无消费方 | medium | patch | SOUL 补 `--source/--title/--type`、消费 `tell`/`skills`、budget 缺省时省略对应 flag。 |
| Blind | route 的 `mode`（cli/in-process）与 executor-run 的 `--mode`（write/readonly）同名不同义 | medium | patch | 加 `adapter_mode` 别名并在 README/SOUL/SKILL 澄清；`--mode write` 不取 route.mode。 |
| Blind | route 在 `when.profiles` 命中时仍回显规则的 `to` | low | patch | 复现 researcher 命中 to=coder；输出实际使用的 profile。 |
| Blind | routing.json 每条规则同时写 `to` 与同值 `when.profiles`，改一个无效 | low | patch | 删除冗余 `to`，保留 `to` 仅作无 `when.profiles` 时的 fallback。 |
| Blind | routing.json 无规则名，route 只回索引；PLAN 要求按规则名成本分组 | low | patch | 加 `name`，route 输出 `rule_name` 与 `rule_index`；README 示例同步。 |
| Blind | sprint-status 仍是 in-progress，spec 已 in-review | low | patch | 同步 3.5 为 review 并更新 `last_updated`。 |
| Blind | epic-3-context 说 dsh/codex “注册并 smoke”，实际只有 fake binary | low | patch | 改为 fake binary smoke 必跑、真实账号 smoke 手动可选。 |
| Blind | 用户 docs/学习手册 被本故事改了 3 行 routing 内容且未纳入 review diff | false | false | 预置 dirty 用户文件；本故事加的 3 行将撤销，保留用户自己的改动，不提交。 |
| Blind | 会话抽屉只显示 id，复制的 `--resume` 不知道用哪个执行器 | medium | patch | run metadata 已有 `executor`；服务端暴露、UI 显示并复制完整 `--executor … --resume …`。 |
| Blind | 复制成功/失败状态永不复位 | low | patch | 定时复位并在抽屉重开时清零。 |
| Blind | session-probe 的“复制失败”check 是 `true`，没有断言 | medium | patch | 改为断言可见文本包含“复制失败”。 |
| Blind | 缺少上述失败模式的测试 | medium | patch | 补 codex 截断流/error 无消息/非零+final、dsh error 无消息/readonly、route 配置错误等。 |
| Blind | executor-run 的 4 同时表示缺二进制和 in-process，调用方无法区分 | low | patch | route 的 `adapter_mode` 区分；README 说明 in-process 的 4 不触发 dsh fallback。 |
| Blind | SKILL.md 的 `metadata={"executor", ...}` 是 set 字面量，不是 mapping | medium | patch | 改成实际 dict/字段列表，避免 worker 复制出非法 metadata。 |
| Blind | 新文件缺少 EOF newline | low | patch | 所有新文件补末尾换行。 |
| VG | coder profile 已加载的 `dsh-delegation` skill 仍直调 dsh 并 `kanban_complete`，绕过 routing/验收 | high | patch | 备份并改写该 profile skill；补 fixture/doc 检查，确保 coder skill 不再出现直调 dsh 或 `kanban_complete`。 |
| Edge | UI 把 `executor_session_id` 原样拼进 `--resume`，含 shell 元字符时可注入/破坏 | medium | patch | 对 executor/id 做 shell quote 或安全字符校验，补探针。 |

**分组与路由：** G1 结果分类（codex/dsh 失败事件、截断流、非零摘要）；G2 route 配置失败闭合（UTF-8、未知 when、空 allowlist、NaN、default/registry 类型）；G3 适配器只读语义（dsh/codex）；G4 worker 契约（dsh-delegation、SOUL、SKILL metadata）；G5 会话抽屉（executor 名、复制状态、探针、注入）；G6 路由 schema/文档（rule name、to、adapter_mode、max_usd/cost）；G7 流程与文件（sprint、epic context、EOF newline）。无 intent_gap / bad_spec；docs/学习手册 为用户预置 dirty 文件，撤销本故事改动后不提交。

## Design Notes

- dsh `--json` 事件：首行 `{"type":"session","sessionId":"session-…"}`，中间可有 `status`/`text`/`tool_*`，末行 `{"type":"final","text":"…"}`；tokens 从 `status.phase=step_end` 的 `usage.inputTokens+outputTokens` 累加，usd 为 null。
- codex `--json` 事件：`thread.started` 的 `thread_id` 作 session；`item.completed` 且 `item.type=agent_message` 的 `text` 作 summary；`turn.completed.usage` 的 input/output tokens 求和。
- `bin/route` 是 worker 的可校验入口，不把 routing 逻辑塞进 `executor-run`；匹配顺序为文件顺序，首条命中；`when` 支持 `profiles`/`sources`/`types`/`keywords`，缺省按 `to` 匹配 profile。
- 默认映射：coder → dsh（保持现状），其它 profile → hermes；`routing.json` 缺 `executor` 时走该默认。解析失败 fail-closed 返回 1，worker 保留当前默认并在 comment 记录，不缓存旧规则。
- `budget.max_minutes` 映射 `--budget-minutes`，`budget.max_cost` 映射 `--max-usd`；route 只透传，不解释金额。
- `hermes` 不是外部 CLI：registry 只表达“worker 内置工具”，`executor-run` 在导入模块前拒绝；`hermes.py` 只防误配。
- UI 只展示/复制 `--resume` 片段，不启动进程；实际续跑由调用方用 `bin/executor-run --resume` 完成。

## Verification

**Commands:**
- `python3 tests/test_executor_run.py` -- 全绿
- `python3 tests/test_routing.py` -- 全绿
- `python3 workbench-app/tests/test_session.py` -- 全绿
- `python3 tests/test_worker_contract.py` -- 全绿（外部 coder skill 缺失时 skip）
- `python3 -m py_compile bin/executor-run bin/executors/*.py bin/route` -- 无语法错误
- `bin/route --profile coder --json` -- 输出末行 `executor=claude_code`
- `cd workbench-ui && npm run build && npm run probe:session` -- 构建与探针全绿
- `git diff --check` -- 无空白错误

**Manual checks (if no CLI):**
- 若 dsh/codex 账号可用，用一条“只回复 OK、不改文件”的只读任务做真实 smoke；不可用则记录为环境不可用，不伪装成功，并保留 fake binary 回归。
