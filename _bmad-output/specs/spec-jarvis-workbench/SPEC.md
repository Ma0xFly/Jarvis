---
id: SPEC-jarvis-workbench
companions:
  - PLAN.md                    # 详细设计与执行批次：状态映射、界面、schema、坑、验收标准全文
  - ARCHITECTURE-SPINE.md      # 施工图：代码地图、REST 契约、metadata 约定、扩展点、禁止事项
sources:
  - ../../brainstorming/brainstorm-ai-automation-workbench-2026-11-04/.memlog.md
  - ../../archive/自动化工作台-计划-v0.5-20261104.md
---

> **Canonical contract.** 本 SPEC 与 `companions:` 中的文件是本次要建、要测、要验的完整契约。`sources:` 仅供追溯，下游不必读。

# Jarvis 人机工作台（待办三区 + 全屏验收 + 完成即固化）

## Why

**痛点 + 机会。** 本机已经有一套能跑的 agent 流水线（Hermes kanban 调度 + magpie 路由 + researcher/housekeeper/coder 三个 profile + dsh 长编码通道），24 张真实任务跑过，但**人机闭环只完成了"派活"这一半**：卡建完就派出去，跑完只看到一句 summary，验收靠翻日志，同样的活下次还要从零建卡。结果是系统在动、能力不增长。

要做的是把这条链补成闭环：**人负责把意图写成可执行的卡（输入 + 验收），机器负责跑，人只做判断，跑完的活沉淀成下次不用再想的东西**。当这一步成立，任务量才会从"一天三张手动建"走向"cron 和事件自己进活"，而人只在验收和拍板时出现。

## Capabilities

- **CAP-1 待办三区与拖拽派发**
  - **intent:** 用户能在一条竖向流水线上看到「流水线 / 今日待办 / 长期待办」三区，并把仓库里的卡拖进流水线完成派发。
  - **success:** 从今日拖一张卡到流水线，3 秒内出现「已入队 · 第 N 位 · 预计 HH:MM」，刷新页面后位置不丢；仓库卡片无状态点、无进度、无 assignee。

- **CAP-2 派发闸门（卡 = 契约）**
  - **intent:** 系统拒绝派发没有验收标准的卡，逼出"怎样算对"这一句。
  - **success:** 验收栏为空的卡拖进流水线被拒并给出提示，补完验收后可正常派发；`ready` 时预算/禁区的生效值被快照写入 `task.metadata`。

- **CAP-3 全屏验收模式**
  - **intent:** 用户能在一个全屏界面里逐张判断产出物，收完才算完成，不收必须说明理由。
  - **success:** 从右上角计数或待收行进全屏；按 `A` 后该卡进 `done`，按 `R` 不填理由无法提交，清空后自动退出并给出本批统计；`review` 超 48h 未处理按规则静默通过并记录。

- **CAP-4 P0 通知**
  - **intent:** 只有会堵住下游的卡会打扰用户，其余静默。
  - **success:** 构造一张有子卡被堵的卡，10 秒内收到系统通知；非 P0 事件不产生通知；同一事件 30 分钟内只升级一次。

- **CAP-5 看板与原生逐项对齐**
  - **intent:** 用户在工作台里看到的看板与 Hermes 原生是同一套语义，不必记两套列名和状态。
  - **success:** 看板 tab 的 8 列名称、顺序、对应 `status` 与原生完全一致；拖拽换列真实改变状态；列定义在全代码库只有一处常量，无第二套映射。

- **CAP-6 AI 自述三行**
  - **intent:** 验收时用户能看到可反驳的自述（交付 / 依据 / 没底），而不是推销式总结。
  - **success:** 任一卡的验收页显示三行，且「没底」不为空；样张排在自述之上。

- **CAP-7 完成即固化**
  - **intent:** 每张完成的卡抽出至少一件可复用件并登记，使同类任务第 N 次成本下降。
  - **success:** 卡完成后 `routing.json` 或对应 profile 的 `skills/` 出现新条目，`metadata.solidified` 标记 `pending`；被下次任务引用后转 `verified`。

- **CAP-8 定时唤醒**
  - **intent:** 挂起的卡能在指定时间自动回队，不靠人记得。
  - **success:** 设定 5 分钟后唤醒，到点自动进入 `ready`（或按 parent 落 `todo`）；卡片显示唤醒时间且可手动提前。

- **CAP-9 事件进候选区**
  - **intent:** 外部事件（邮件 / 私信 / GitHub）自动变成带原文锚点的候选，人一键转卡，不污染待办。
  - **success:** 同一事件重复投递只产生一条候选（`idempotency_key`）；候选能点回原始来源；一键转卡后走 CAP-2 的契约流程。

- **CAP-10 四条配方**
  - **intent:** 早报 / GitHub / 网站巡检 / 告警四类常规活能无人值守产生结果或候选。
  - **success:** 四套各跑一次真实事件：有料时产出文件或卡，无料时 `[SILENT]` 不打扰。

- **CAP-11 熔断与成本**
  - **intent:** 防止无限派活，并让每张卡的花费可查、同类任务成本可比。
  - **success:** 构造 9 层子任务链，第 9 层被拒并留 comment；单卡能看到 `metadata.cost`（分钟 / token / 费用），按规则名可聚合。

- **CAP-12 会话追溯**
  - **intent:** 每张卡都能跳回它的执行会话，dsh 那层也能续上。
  - **success:** 已完成卡点「打开执行会话」能读到该轮完整对话；执行器任务的 `metadata.executor_session_id` 存在，且能用该执行器的续跑参数接上。

- **CAP-13 运行画布（排障页）**
  - **intent:** 用户起疑时能一屏看清拓扑、当前流动与花费，而不必翻日志。
  - **success:** 画布显示触发→管家→规则→worker→产出整条链，运行中的任务以管道内位置表达进度（已用时长/预算），节点带队列深度与今日花费。

- **CAP-14 执行器可插拔**
  - **intent:** 同一个任务可以交给不同执行器（Claude Code / dsh / 内置工具 / 将来别的），换执行器不改业务流程。
  - **success:** 存在统一契约 `bin/executor-run`（stdin 任务书 → stdout JSON + 五个退出码）；新增执行器只需加一个适配器并在 `executors.json` 注册；任一执行器跑完都回写 `metadata.executor_session_id`。

## Constraints

- **唯一状态源是 `kanban.db`**：仓库/流水线/验收全部映射到原生状态（`triage` / `ready` / `running` / `review` / `done`），不新建库、不双写。
- **执行器不耦合 Hermes 插件体系**：统一走 CLI 契约，任何调用方（worker / dsh / Claude Code / 人）都能用；插件只能当薄壳调用，不得成为唯一入口。
- **前端必须能装进经典脚本宿主**：插件用 `<script>`（无 `type=module`）加载，产物须为 IIFE；前端源码统一在 `workbench-ui/`，两个宿主目录只放构建产物。
- **看板不得发明自己的状态模型**：自有前端可渲染，但列名、列序、`status` 字符串必须与 Hermes 原生 8 列逐项对齐，且全库只允许一处列定义常量。
- **上游内核零改动**：只用插件 / CLI / REST；已知"答后续原会话"需改 `gateway/session_kanban.py`，明确不做。
- **`running` 不可手动设置**；从 blocked 恢复只能落 `ready` 或 `todo`。
- **`kanban_block` 无 metadata 参数**：结构化提问必须先写 ```ask 评论块。
- **仓库卡不承载状态**：无状态点、无进度、无 assignee；今日/长期用 `metadata.bucket` 区分，不占用 `todo`。
- **单机单库**：不做跨主机共享看板。
- **手机通道本期不做**：P0 只走桌面系统通知，接口预留。
- **安全**：密钥不进卡与 skill；高权限任务先 `block(needs_input)`；无 sudo NOPASSWD；worker 不得 force push、不得动生产库。

## Non-goals

- 自建看板后端、todo 服务或任何第二套任务中台（含把 agent-autoloop 当中台用）。
- 改上游内核实现"答后续原会话"。
- 跨主机 kanban / 把工作台挂到 ECS 当执行节点。
- QQ / 微信接入（只留 schema）。
- 本地 Qwen 分流器、只留一个 worker、谎报降权、state.db 备份策略。
- 验收三档分流与抽样（P5 再议，本期每张都验）。

## Success signal

真实工作连续一周全部走这条路：**建卡 → 拖进流水线 → 跑完 → 收到 P0 通知 → 全屏验收 → 登记固化物**，且至少有一条同类任务的成本记录显示第二次明显低于第一次（例如 40 分钟 → 8 分钟）。若一周后仍存在"绕开工作台直接 `hermes -z` 干活"的常态，视为未达成。

## Assumptions

- 派发链路基于实测的 Hermes v0.22.7（`request_id = kanban:[board,task_id,run_id]`，一次 attempt 一条会话）。
- magpie gateway `127.0.0.1:3425` 常驻，`group/group` 与 `group/main` 可用。
- 现有 profiles（researcher / housekeeper / coder）与 `workbench-conventions` skill 保留不动。
- worker 有权修改自己 profile 下的 `skills/`；跨 profile 修改仍需人合。

## Open Questions

- **远程面板**：A（Tailscale + 本机 dashboard，推荐）还是 B（ECS 独立部署）？不影响 P1–P4。