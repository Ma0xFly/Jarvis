---
title: '3.4 执行器契约与 claude_code 适配器'
type: 'feature'
created: '2026-11-09'
status: 'done'
route: 'dispatch'
baseline_commit: 'fe9cdcb'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 执行器只能由 Hermes worker 内部调用，没有可复用、可替换的统一入口；各家参数、输出、失败方式不同，调用方无法只依赖一套契约。

**Approach:** 新增 `bin/executor-run`、`executors.json` 与 `claude_code` 适配器。任务书走 stdin，stdout 末行输出统一 JSON，退出码只用 0/1/2/3/4；适配器只做翻译，`routing.json` 留到 3.5。

## Boundaries & Constraints

**Always:** 任务书只从 stdin 读；stdout 末行是统一 JSON；退出码只用 0 成功、2 需要人、3 超预算、4 执行器不可用、1 其它；适配器不 import Hermes、不读写 `kanban.db`、不记录任务书或密钥；缺二进制/未知执行器为 4，超时/预算中断为 3；无 session id 写 `null`；执行器不 commit/push。

**Never:** 不做 `--stream`；不接 dsh/codex/hermes；不改 `routing.json`；不做插件；不改 Hermes 内核；不把验收/状态机等业务逻辑放进适配器。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 成功 | `claude` 返回 `is_error=false` 及 session/result/cost | `ok=true`、`exit_code=0`，字段映射 | N/A |
| 需要人 | `permission_denials` 非空或结果要求权限 | `exit_code=2`、`ok=false` | 调用方转 blocked |
| 超预算 | 子进程超时，或 subtype/文本表明预算中断 | `exit_code=3`、`ok=false` | 保留 log_path |
| 不可用 | 未知 executor、registry 缺失、`claude` 不在 PATH | `exit_code=4`、`ok=false` | 换执行器 |
| 其它错误 | 非零退出、输出无法解析、空任务书、非法 cwd | `exit_code=1`、`ok=false` | summary 说明原因 |
| 只读/续跑 | `--mode readonly` / `--resume <id>` | `claude` 收到 `plan` / `--resume` | 无 id 时 `session_id=null` |

</frozen-after-approval>

## Code Map

- `bin/executor-run` -- 新建 CLI：argparse、读 stdin、加载 registry、调用适配器、subprocess 超时、写日志、末行 JSON；任务书不进 stdout/log。
- `bin/executors/claude_code.py` -- 新建适配器：`build_command(ctx)` 映射 `claude -p --output-format json` / `--permission-mode` / `--max-budget-usd` / `--resume`；`translate_result(stdout, stderr, raw_exit)` 解析末行 JSON 并映射字段与退出码。
- `executors.json` -- 项目根注册表：`claude_code -> executors.claude_code`；结构需容纳 3.5 的 dsh/codex/hermes。
- `tests/test_executor_run.py` -- 临时 PATH fake `claude`，覆盖参数、stdin、JSON、五码、resume、只读、缺二进制、未知 executor、超时、日志脱敏。
- `workbench-app/README.md`、`.gitignore` -- 契约文档与 `.runs/` 忽略；不改 server/UI/routing。

## Tasks & Acceptance

**Execution:**
- [x] `bin/executor-run` -- CLI、subprocess、超时、日志、统一 JSON -- 唯一入口
- [x] `bin/executors/claude_code.py` -- 参数/输出翻译与退出码映射 -- 首个适配器
- [x] `executors.json` -- 注册 `claude_code` -- 注册表
- [x] `tests/test_executor_run.py` -- 覆盖 I/O 矩阵与五码 -- 回归
- [x] `workbench-app/README.md` + `.gitignore` -- 文档与日志忽略 -- 可维护性

**Acceptance Criteria:**
- Given registry 注册了 `claude_code`，when 用 `--executor claude_code --cwd <dir> --mode write --budget-minutes 30 --max-usd 2` 从 stdin 读任务书，then 调用 `claude -p --output-format json`，stdout 末行 JSON 含 `ok`/`executor`/`session_id`/`summary`/`cost`/`log_path`/`exit_code`。
- Given 成功、需要人、超预算、不可用、其它错误，when CLI 退出，then 分别返回 0/2/3/4/1；缺二进制为 4，超时/预算中断为 3。
- Given `--resume <id>`，when 执行，then `claude` 收到 `--resume`；无 session id 时 JSON 为 `null`。
- Given `--mode readonly`，when 执行，then `claude` 收到只读权限模式，不获得写权限。
- Given 适配器执行，when 检查依赖，then 不 import Hermes、不读写 `kanban.db`、不把任务书或密钥写入日志。
- Given 改动完成，when 跑 `python3 tests/test_executor_run.py` 与 `py_compile`，then 全绿。

## Implementation Notes

- 2026-11-09：`tests/test_executor_run.py` 40 个测试全绿；`py_compile`、`bin/executor-run --help`、`git diff --check` 通过。只使用临时 PATH 的 fake `claude`，没有启动真实 Claude 会话。
- 运行器决定：`--budget-minutes` 省略时不设墙钟超时（调用方应显式传）；日志脱敏只处理任务书整段/整行原样回显；`files_changed` 无可靠来源时返回 `[]`；权限/预算的自由文本识别是启发式，`permission_denials` 与 `subtype` 优先。
- `--cwd` 必填；`--mode readonly` 映射 `plan`；`--mode write` 映射 `acceptEdits`；`executors.json` 同时接受对象和字符串条目，`mode != cli` 返回 4。
- 2026-11-09 review patch：R1–R15、R18、R20、R21、R23、R24 已修；`test_executor_run.py` 75 个测试全绿，`py_compile`、`--help`、`git diff --check` 通过。主要变化：`type=="result"` 校验、失败才跑文本启发式、stderr 预算/权限、任务书/密钥脱敏、默认 30 分钟超时、信号清进程组、argparse JSON、空 cwd 拒绝、binary 解析、registry 过滤、cost/ok 严格校验、git commit/push 防护、哨兵 DB 测试与文档对账。R17/R19 已记入 deferred-work；R16/R22/R25 判为 false。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| R1 Blind/Edge | 成功结果的文本含 budget/permission 时被误判为 3/2 | medium | patch | 复现 `result="Added a budget limit; tests pass"` -> 3；文本启发式应只在 `is_error` 或非零退出时启用。 |
| R2 Blind/Edge/VG | 末行任意 JSON 对象（`{}`、`system/init`）被当成功 | high | patch | 复现 `{}` -> `ok=true`；必须要求 Claude 结果 `type=="result"`。 |
| R3 Blind | stdout 不可解析时不读取 stderr 的预算/权限信号 | medium | patch | 复现 `stdout=""` + stderr `Exceeded USD budget` -> 1；解析失败分支应检查 stderr。 |
| R4 Blind/Edge/VG | 任务书脱敏只匹配 ≥16 字符整段/整行，JSON 转义和短行会泄漏；失败 payload 未脱敏 | high | patch | VG 复现 `# 任务` / `只改 x.py。` 留在日志，`hunter2` 进 summary/log；需覆盖 JSON 转义、短行和失败摘要。 |
| R5 Blind/VG | 执行器输出中的密钥原样进日志，现有测试未让密钥进入输出通道 | high | patch | VG 复现 `ANTHROPIC_API_KEY` 写入 `.runs`；需脱敏已知 secret env 值并补测试。 |
| R6 Blind/Edge/VG | `--budget-minutes` 省略即无墙钟超时；孙进程持有管道时可能无限等待 | medium | patch | 代码 `timeout_seconds=None`；默认改为 30 分钟并在 README 说明。 |
| R7 Blind/Edge | 无 SIGINT/SIGTERM 转发/清理，KeyboardInterrupt 不输出 JSON，执行器可能继续跑 | high | patch | `_terminate` 只在 TimeoutExpired 分支；`finally`/信号处理应杀进程组并输出 JSON。 |
| R8 VG | 超时测试未验证执行器真的被杀 | medium | patch | 现有 fake sleep 30s 只断言返回码；需 marker 或退出状态断言。 |
| R9 Blind/Edge | argparse 用法错误退出 1 但 stdout 无 JSON | medium | patch | `--executor` 缺 `--cwd` 时 stdout 为空；error handler 应输出 failure payload；`--help` 作为显式例外文档化。 |
| R10 Edge | `--cwd ""` 回落到调用者 cwd，可能改错工作树 | high | patch | `Path("").resolve()` 返回项目根；空/空白 cwd 应拒绝。 |
| R11 Blind | runner 把缺失 binary 默认成 `claude`，3.5 适配器会误调 | low | patch | 移除 runner 的 `or "claude"`，由各适配器决定默认。 |
| R12 Blind | `shutil.which` 在 runner cwd 解析相对 binary，Popen 在目标 cwd | low | patch | 相对路径应基于 `--cwd` 解析，或文档要求 absolute/PATH。 |
| R13 Blind | registry 的 version/description 未校验，flat mapping 把任意键当执行器 | low | patch | 过滤 `description` 等元数据键；模块约定保持。 |
| R14 Edge | cost 巨大 int/Infinity 触发 `math.isfinite` OverflowError，成功结果变内部错误 | medium | patch | 复现 `10**1000` -> OverflowError；转换需捕获并回落 `null`/0。 |
| R15 Blind | `ok` 用 `bool()` 强转，`"ok":"false"` 被当成功 | medium | patch | 复现 `ok:"false"` + exit 0 -> `ok=true`；应要求 `ok is True`。 |
| R16 Blind | `files_changed` 恒为 `[]`，下游无法依赖 | false | false | spec Design Notes 明确“不可靠时返回 []”，README 已写固定 `[]`；无 AC 要求非空。 |
| R17 Blind | summary 未限长，可能把多 KB result 塞进单行 JSON | low | defer | 当前无长度契约；3.5 可加截断策略。 |
| R18 Blind | 未强制“执行器不 commit/push” | medium | patch | spec Always；适配器应加 `--disallowedTools` git commit/push 防线并说明是 best-effort，不是沙箱。 |
| R19 Blind/Edge | 输出/日志无上限，`.runs` 落在目标仓库造成未跟踪污染 | low | defer | 规格选定 `cwd/.runs`；保留日志便于排障，后续加保留/清理策略。 |
| R20 Blind | 缺测试：env registry 优先级、adapter 异常、malformed registry、max-usd 省略、stderr 信号、mode != cli、进程终止等 | medium | patch | 补测试覆盖这些路径；与 R8/R5/R3 重叠。 |
| R21 Blind/VG | 依赖边界只查 import，未验证运行时不读写 `kanban.db` | medium | patch | 加 `HERMES_KANBAN_HOME` 哨兵 DB 测试，断言文件不变。 |
| R22 Blind | sprint/spec 状态仍 in-progress | false | false | 当前 spec frontmatter 已是 `in-review`；review diff 生成早于状态翻转。 |
| R23 Blind/VG | epics.md 说超时映射 4，spec/README/code 是 3 | medium | patch | 更新 epics 3.4：缺二进制 4，超时/预算中断 3。 |
| R24 Blind | README 漏 `--registry`/env、默认超时、`log_path` null、脱敏边界；spec AC 未列 files_changed | low | patch | 文档补全；AC 的“含”不排斥额外字段，README 已解释 files_changed。 |
| R25 Blind | docs/学习手册 dirty 且不在 diff，疑似本故事改动 | false | false | 用户既有未暂存文件；本故事未编辑，保持不动（前序约束）。 |

**分组与路由：** G1 结果分类（R1/R2/R3）；G2 脱敏与密钥（R4/R5）；G3 超时/信号/进程（R6/R7/R8）；G4 CLI 边界（R9/R10/R11/R12/R13）；G5 值校验（R14/R15）；G6 契约与文档（R18/R20/R21/R23/R24）；defer：R17/R19；false：R16/R22/R25。无 intent_gap / bad_spec。

## Design Notes

- `claude_code` 以 stdout 末行 JSON 为准：`result` 作 summary，`session_id` 原样透传，`total_cost_usd` 作 `cost.usd`，`usage` 的 input/output/cache token 求和作 `cost.tokens`；`files_changed` 不可靠时返回 `[]`。
- `write` 映射 `--permission-mode acceptEdits`，`readonly` 映射 `plan`；`--max-usd` 映射 `--max-budget-usd`，`--budget-minutes` 由 runner 做墙钟超时。
- 日志默认 `<cwd>/.runs/<UTC 时间>-<executor>-<pid>.log`，只含命令、cwd、退出码、stdout/stderr，不含任务书；cwd 内日志返回相对路径。
- 适配器接口固定为 `build_command(ctx)` + `translate_result(...)`，3.5 只加模块与 registry 条目。

## Verification

**Commands:**
- `python3 tests/test_executor_run.py` -- 全绿
- `python3 -m py_compile bin/executor-run bin/executors/claude_code.py` -- 无语法错误
- `bin/executor-run --help` -- 参数说明可读
