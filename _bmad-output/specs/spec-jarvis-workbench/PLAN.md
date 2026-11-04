# 本地 Agent 自动化工作台 · 总计划 v1.0

> **唯一执行入口。** v0.5 的全部需求与 v0.6 脑暴结论已合并，不再有增量补充文件。
> 历史版本：`_bmad-output/archive/自动化工作台-计划-v0.5-20261104.md`；脑暴日志：`_bmad-output/brainstorming/brainstorm-ai-automation-workbench-2026-11-04/.memlog.md`
> 原则：**不重复造轮子**——能复用的开源组件全部复用，自己只写薄胶水。
> 最后更新：2026-11-04

---

## 0. 这份文件怎么用

| 章节 | 内容 | 什么时候读 |
|---|---|---|
| §1–§5 | 为什么这么设计：定位、架构、复用、核心模型、界面 | 改设计前必读 |
| §6 | worker 分派矩阵与 `routing.json` | 加 worker / 改规则时 |
| §7 | **现状基线**：已经能跑的东西 | 开工前对照，别重做 |
| §8–§9 | **执行批次 + 逐条验收标准** | 每天照着做 |
| §10 | 硬技术事实与三个坑 | 动手前必读，会省你半天 |
| §11–§12 | 触发层、配方、远程面板 | P4 之后 |
| §13–§15 | 红线、不做清单、待拍板 | 决策时 |

### 0.1 v1.0 相对 v0.5 改了什么

| # | 变更 | 影响 |
|---|---|---|
| 1 | 待办 tab 改为三区（流水线 / 今日 / 长期），拖进流水线才算派发 | 替换原"今日左 / 仓库右"两栏 |
| 2 | 验收从"卡片抽屉"升级为**全屏验收模式** | 替换 2d-1，2d-2 回执协议降为 P5 |
| 3 | 新增**完成即固化**与 `routing.json` 承载固化物 | 新增，不改原有条目 |
| 4 | 看板 = **自有前端渲染，但 8 列与原生逐项对齐**（不是嵌入 dashboard） | 原自建 5 列在 P1 换成对齐 8 列 |
| 5 | 新增通知分级 P0/P1/P2 与验收槽 | 新增 |
| 6 | 2d-4 事件接入改为"先落候选区，人一键转卡" | 原为直接建卡 |
| 7 | 三项待拍板已定（成本聚合口径 / 手机暂不做 / worker 可改自己 skills） | 见 §15.1 |

---

## 1. 定位与边界

一句话：**把人的模糊意图编译成可执行的卡，交给本机 agent 跑完，人只做判断，跑完的活沉淀成下次不用再想的能力。**

三层分工：

- **输入侧（人）**：想事、建卡、写验收、判断收不收
- **大脑（Hermes 管家 + kanban）**：状态机 + 调度 + 记忆 + 技能 + 定时/事件触发
- **手脚（worker / dsh）**：真干活；模型经 magpie 路由

三条不能越的边界：

1. **任务优先**：先建卡，再派发，跑完为止。不做"执行优先、事后追认任务"。
2. **不自建看板模型**：看板 tab 由我们自己的前端渲染，但列名、列序、`status` 必须与 Hermes 原生 8 列逐项对齐。禁的是"另发明一套列/状态"，不是视图本身。
3. **单机单库**：kanban 是单机设计，唯一事实源是 `kanban.db`；不做跨主机共享看板。

---

## 2. 架构总览

```
【输入侧】人                  【大脑】                        【手脚】              【知识】
待办 tab（三区）        ┌──────────────────────────┐
 流水线 | 今日 | 长期 ──→│ Hermes Agent 管家 profile │──→ researcher  搜集/调研/表格   Obsidian
 拖进流水线=派发         │  · kanban 状态机 + 消息总线 │──→ housekeeper 巡检/杂务       (obsidian-mcp
 验收模式（全屏）        │  · cron 定时 / webhook 事件 │──→ coder       长编码 → dsh     只读归档)
                        │  · memory + skills         │
事件（邮件/私信/GitHub）→│  · routing.json 分派规则    │──→ dsh headless（经 tool-bash
 ↓ 候选区，人一键转卡    └───────────┬──────────────┘     调 hermes kanban 回写）
                        magpie gateway 127.0.0.1:3425
                        路由组/配额故障转移/masking/预热
                                   ↓
                    火山方舟 / 硅基流动 / 订阅 / 本地模型
```

**A2A 说明**：kanban 本身就是跨 profile 的对等协作总线（任何 worker 读写同一批 SQLite 行）。dsh 作为 worker 时通过 tool-bash 执行 `hermes kanban show/comment/complete/block` 双向通信；Hermes worker 反过来可 `dsh --profile headless "子任务" --json`。**不需要自建任何 RPC/消息协议。**

---

## 3. 复用矩阵（已核实）

| 层 | 选型 | 状态 | 备注 |
|---|---|---|---|
| 模型路由/成本 | **magpie**（本地 gateway 3425） | ✅ 已装 | 组路由/故障转移/masking/预热 |
| 任务状态机 + A2A 总线 | **Hermes kanban**（SQLite、原子认领、gateway 内嵌调度器） | ✅ 自带 | `specify`/`decompose` 原生有 REST 端点 |
| 可视化工作台 | **Hermes dashboard**（内置 Kanban 插件） | ✅ 自带 | 拖拽/REST/WS；非回环绑定强制认证 |
| 一次性调用 | `hermes -z "..."` | ✅ 实测可用 | 单发单收，用于互调用 |
| 长编码执行 | **dsh headless**（`--json`/`--session-id`） | ✅ 0.2.1-rc.5 | 经 magpie Custom model API 接模型 |
| 时间触发 | Hermes cron（`[SILENT]` 降噪） | ✅ 自带 | |
| 事件触发 | Hermes webhook（本地端口） | ✅ 自带 | |
| SOP/决策沉淀 | Hermes skills + memory | ✅ 自带 | |
| 知识归档 | Obsidian + obsidian-mcp | ✅ 已挂 | 只读面板 |

自己写的代码只剩三样：**① 工作台前端（待办三区 + 验收模式）② `routing.json` 分派规则 ③ cron 蓝图提示词**。胶水脚本不用写——kanban worker 本身就是 profile。

---

## 4. 核心模型（v1.0 定稿）

### 4.1 卡 = 契约

卡上只写两栏，缺一栏不许进流水线：

| 栏 | 含义 | 例子 |
|---|---|---|
| **输入** | 用哪些材料 / 仓库 / 分支 / 来源 | `~/Projects/Jarvis` + `sources.json` + 三个链接 |
| **验收** | 怎样算对，一句话、可判定 | `不超过三家 · 含价格 · 有 NAT 穿透实测` |

**预算与禁区不写在卡上**，走三层默认：

```
全局默认    钱/轮数/时长/文件黑名单        ← 一次定好，永不手写
  ↓ 被覆盖
路线默认    routing.json 每条 to 自带预算包（dsh 长编码 3h / housekeeper 10min）
  ↓ 被覆盖
卡覆盖      只在这次要偏离时才写
```

进 `ready` 那一刻，把三层算出的生效值**快照进 `task.metadata`**。不这么做，明天改了全局预算，上周那张卡的审计记录就变了——你再也说不清它当时按什么跑的。

### 4.2 仓库存信息，流水线产状态

- 仓库卡只有**标题 + 验收一行**：没有状态点、没有进度、没有 assignee、没有边框高亮。
- 仓库卡统一 `triage`，今日/长期用 `metadata.bucket = today|long` 区分（**不占 `todo`**——`todo` 是"等 parent 完成"的语义，手动塞会与自动提升冲突）。
- 视觉上流水线用凹槽底色、仓库用平面底色；**视觉分区即语义分区**。仓库里一旦长出状态点，人就会管理仓库，它会长成第二个看板。

状态映射（零内核改动）：

| 工作台概念 | kanban 状态 | 判定 |
|---|---|---|
| 今日 / 长期仓库 | `triage` + `bucket` | 拖进流水线前 |
| 流水线·队列 | `ready` | 契约齐全、等认领 |
| 流水线·运行 | `running` | dispatcher 已 claim |
| 流水线·待收 | `review` | worker 干完，等人判断 |
| 已收 | `done` | 人按 A，且固化物已登记 |
| 退回 | 回 `triage` | 人按 R，卡带一句退回理由 |

### 4.3 验收：全屏模式，不是 tab

- 入口：右上角「待你处理 N」+ 流水线「待收」行。
- 一张一屏：左样张、右验收标准原文 + AI 自述三行 + 本次运行；键位 `A 收` `R 不收` `S 跳过` `Esc`。
- **`R` 必填理由**（不填不能提交）——那句理由是唯一天然会变成规则/skill patch 的输入。
- 清空即退出，给一行"本次收 7、退 1"。
- 兜底：`review` 挂满 48h 无人处理 → 按规则静默通过并记一条"未验收"，否则队列会自己堵死。

**三档分流**（P5 再上，先用"每张都验"）：

| 档 | 判据 | 人做什么 | 应占 |
|---|---|---|---|
| A 机器验 | 测试通过/文件存在/HTTP 200/diff 只动允许路径 | 什么都不做，日报一行 | ~70% |
| B 抽样验 | 机器能判完整、语义要人确认 | 只看抽中的一张；不收则整批回退 | ~25% |
| C 前置看 | 不可逆/对外/花钱/动生产 | **做之前**先看方案 | ~5% |

### 4.4 完成即固化

进 `done` 的前提是抽出至少一件可复用件，三选一：

| 落点 | 抽什么 | 例子 |
|---|---|---|
| `routing.json` 一条规则 | 这类活给谁、带什么 skill | 调研类 → researcher + 必须附官方链接 |
| skill patch | 这次踩的坑、验证过的步骤 | housekeeper 盘目录别进 `vendor/`，会卡 3 分钟 |
| 可重跑脚本 | 能机械重做的那部分 | `check_feeds.py`，每周扫信源可用性 |

- 固化物有两态：**未验证 → 已验证**。被下一次任务真正引用过才转正。
- **谁写**：worker 可以直接改自己 profile 的 `skills/`（2026-11-04 授权），改完在 `kanban_complete` 的 `metadata.solidified` 登记 `{type: skill_patch, path, status: pending}`；**跨 profile 的修改仍要人合**。
- **成本口径**：`task_runs`（起止/耗时/轮次）+ `~/.config/magpie/usage.jsonl`（token/费用）按时间窗聚合，写 `task.metadata.cost`；「第 N 次成本」按 `routing.json` 的规则名分组。
- KPI 是**同类任务第 N 次的成本曲线**（40 分钟 → 8 分钟 → 0 分钟），不是"完成卡数"。
- 防假固化：只看"已验证沉淀数"，不看"写了多少"。

### 4.5 AI 自述必须可反驳

验收页上的自述固定三行，样张在上、自述在下（顺序会影响判断，介绍放上面会被它的措辞锚定）：

```
交付：report-xx.md（8.2 KB）/ deck.pptx（14 页）
依据：a b c 三个来源逐条列链；输入文件 3 个
没底：① 第 4 页市占率是二手引用，未核原始出处
      ② 只查了中文源，英文竞品未覆盖
```

「没底」是重点：它给了你优先看的靶子。"谎报追责/降权"这条**本期不展开**（2026-11-04 明确）。

---

## 5. 界面结构

四个面，按**使用时机**分，不按功能分：

| 面 | 时机 | 归属 |
|---|---|---|
| 待办（默认落地） | 天天看，派活与收活 | 自有 |
| 验收模式（全屏） | 有产出物时 | 自有 |
| 看板 | 真要改状态时 | 自有视图，**与原生 8 列逐项对齐** |
| 运行画布 | 起疑时 | 自有（排障页，非默认） |

### 5.1 待办 tab（三区）

```
┌────────────────┬────────────────┬────────────────┐
│ 流水线 6        │ 今日待办 5      │ 长期待办 6      │
│  队列 2         │ ┌────────────┐ │ ┌────────────┐ │
│  运行 1（管道）  │ │ 标题        │ │ │ 标题        │ │
│  待收 3         │ │ 验收 · …    │ │ │ 验收 · …    │ │
│                 │ └────────────┘ │ └────────────┘ │
├────────────────┴────────────────┴────────────────┤
│ ✎ 一句话建卡（默认落今日）                    ⏎   │
└───────────────────────────────────────────────────┘
```

- **拖进流水线 = 派发**（唯一承诺动作）；方向从右往左，所以**整列任何位置都是投放区**。
- 派发时验收为空 → 拦下 + 抖动 + 提示"先补一句怎样算做完"。
- 派发后 3 秒内给回执：`已入队 · 第 3 位 · 预计 14:19 开始`，否则会连点三次。
- 运行段用**管道内位置**表达进度（位置 = 已用时长 / 预算时长），不用动画；点快到头 = 要超时。
- 在跑只显示需要你注意的（超 50% 预算 / 重试过 / 卡住），其余折叠成"另有 8 条正常"。

### 5.2 验收模式（全屏）

```
┌────────────────────────────────────────────────────────┐
│ 验收  1/3   ▬▬░░░░░░░░   收完才进「已完成」    [退出 Esc]│
├──────────────────────────┬─────────────────────────────┤
│                          │ 验收标准（你当初写的）        │
│      产出物样张           ├─────────────────────────────┤
│  PPT 页 / 报告 / 表格     │ AI 自述：交付 / 依据 / 没底   │
│  （可翻页）               ├─────────────────────────────┤
│                          │ 本次运行：coder 41m ¥1.80    │
├──────────────────────────┴─────────────────────────────┤
│        [A 收]        [R 不收]        [S 跳过]           │
└────────────────────────────────────────────────────────┘
```

### 5.3 看板（对齐约束，不是嵌入）

看板 tab **由我们自己的前端渲染**（与待办页同一套组件和主题），但语义必须与 Hermes 原生逐项对齐：

| 列（顺序固定） | status | 说明 |
|---|---|---|
| 待整理 | `triage` | 刚进来 / 未整理 |
| 待执行 | `todo` | 等 parent 完成，或排队中 |
| 已排期 | `scheduled` | 定时唤醒，`not_before` 未到 |
| 就绪 | `ready` | 可被 worker 认领 |
| 运行中 | `running` | **只读**，不能手动设 |
| 待拍板 | `blocked` | 等人回答（看 `block_kind`） |
| 待验收 | `review` | 等人判断 |
| 已完成 | `done` | 终态 |

- **列定义只有一份**：前端一个 `COLUMNS` 常量（`key` + `label`），后端校验复用它；不允许多处硬编码。
- 卡片字段与原生一致：标题、`assignee`、`priority`、`block_kind`、`latest_summary`、时间；不额外发明状态徽章。
- 拖拽换列 = `PATCH {status}`，走原生状态机（`running` 不可设、`blocked→ready` 走 unblock）。
- 草稿 007 里那个 5 列（队列 / 运行中 / 待收 / 今日仓库 / 已完成）是错的示例，不要照着做。

### 5.4 运行画布（排障页）

参考 magpie 路由树的表达方式：**拓扑 + 流动 + 账**。

```
触发           管家         规则            手脚                    产出
cron ─┐
webhook ─┼→  调度器  →  routing.json  →  researcher ─→ web/exa ─┐
人手建卡 ─┘   (claim)                  housekeeper ─→ terminal ─┼→ artifact → review → 人
                                       coder ───────→ dsh ──────┘
```

- 节点上贴实时数字：队列深度 / 今日通过 / 平均耗时 / 今日花费。
- 三个时间尺度：**现在**（活跃任务点 + 队列）/ **今天**（每卡一条泳道，甘特）/ **累计**（路径热力——走过 20 次还是人肉建卡的路，就是下一根该固化的线）。
- 5s 轮询足够。它是排障页，不是监视器。

### 5.5 通知（打扰）

只有三类事配找人：**等你拍板**、**等你验收**、**异常**（越界/失败/超预算）。其余静默。

| 级 | 判据 | 手段 |
|---|---|---|
| P0 | 卡住会堵下游（有子卡或有人等） | 系统通知 + 右上角常驻 + 置顶（**手机通道暂不做**，接口预留） |
| P1 | 只卡自己 | 进收件箱，攒 3 条或 30 分钟一起推 |
| P2 | 不急（验收默认） | 日报一行 |

- **只升级一次**：同一条 30 分钟内没处理，升一档，不重复刷屏。
- **倒计时代替未读**：每张 review 显示"还有 42h 自动收"。未读数积压成焦虑，倒计时会自愈。
- 通知里就要能操作（收 / 不收 / 10 分钟后再提醒），别让人点进详情才算开始。
- 验收槽：固定容量 3，**有空槽就推、没空槽排队**（任务少时零等待，任务密时最多 3 个在眼前）；同一次 `decompose` 的兄弟卡自动成束。

---

## 6. worker 分派

### 6.1 矩阵

| 任务类型 | worker | 模型路径 | 理由 |
|---|---|---|---|
| 长编码/重构/多文件工程 | **dsh headless** | magpie strong-lane | harness 强在长任务上下文 |
| 搜集资料/调研 | **researcher**（Hermes profile） | magpie group | 有 browser/web_search/Exa 全套 |
| 做表格/Excel/PPT | **housekeeper** 或 researcher | 同上 | 有 xlsx/officecli/ppt-master skills，dsh 没有 |
| 运维巡检/服务器检查 | **housekeeper** | cheap-lane | terminal + 排障档案 skills |
| 剧烈重构前的独立 review | **coder**（只读模式） | 强模型 | 跨模型审查 |
| 信息过滤/打标签/摘要 | **housekeeper** | cheap-lane | v2 换本地 Qwen |

分派落法：`kanban create --assignee <profile名>`。dsh 的入队方式 = coder profile 代领后 `dsh --profile headless --json` 执行，结果回写。

magpie intent routing 上线后，快问快答自动落 cheap-lane，不走建卡流程。

### 6.2 routing.json（唯一可校验的规则源）

现状：`workbench-conventions` skill 里那张"任务类型 → worker"的表**是文本，LLM 可以忽略，也不会报错**。改为数据文件 `~/Projects/Jarvis/routing.json`：

```json
{
  "rules": [
    {
      "name": "调研类",
      "when": {"source": "card", "types": ["research"]},
      "to": "researcher",
      "skills": ["reliable-source-collection"],
      "budget": {"max_minutes": 60, "max_cost": 3},
      "tell": "必须附官方链接，不允许只给二手转述"
    }
  ]
}
```

- 建卡入口（workbench 新建 / ingest / cron）读同一份文件；**事件不能自己指定 assignee/cwd**（指令数据分离）。
- 改文件即生效，无需重启；解析失败保留旧版并提示。
- 同时承载 §4.1 的预算包与 §4.4 的固化物登记。
- 「定了的事 / 定了不做的事」写进 `~/Projects/Jarvis/`，由 `decisions` skill 指向。

### 6.3 分类器：三层漏斗，jev 只配做第二层

| 层 | 谁决定 | 成本 | 覆盖 |
|---|---|---|---|
| **L0 显式** | 建卡时人点类型（research / code / sheet / ops / review / summary，默认按标题预选） | 0 | ~90% |
| **L1 规则** | `routing.json` 的 `when`（source / type / 关键词）+ webhook 事件的 `type` 字段 | 0 | 自动进来的事件 |
| **L2 模型** | 仅在 `type` 缺失时调用，输出受限枚举 + `confidence` | 极低 | 剩余少数 |

- **`typesafe/jev-router` 不是分类器**：它输出"该用哪个模型、什么推理档位"，是给请求挑执行者的路由器；拿它判任务类型是问错了问题。
- **`typesafe/jev-1.13`（/1.14）可以当 L2**：TypeSafe 的 structured-decision 模型，返回 typed choice 而非散文，$0.042/M input、输出基本免费。已知短板是**只擅长边界清晰的问题**——类型含糊时它会给出自信但错的答案。
- **门槛**：L2 只在分类为空时调用；`confidence < 0.7` → 落回今日仓库让人点，不自动派发。
- **分类器永远不直接决定 assignee**，只输出 `type`；`type → worker` 由 `routing.json` 映射（与"事件不能自己选路"同一条原则）。
- **审计**：每次分类写 `metadata.classification = {by: human\|rule\|model, model, label, confidence, cost}`。`by=human` 的记录就是将来评测模型的标注集。
- **上 L2 的条件**：累计 ≥50 张带人工标签的卡，离线跑一次对比（jev-1.13 vs `group/group` 的 cheap 模型），top-1 ≥ 90% 才上线。当前 24 张真实卡，远未到。
- **打扰分级不用模型**：P0/P1/P2 是三个可判定条件（有无未完成子卡 / `block_kind` / 是否越界），必须可复现，规则就够。

---

## 7. 现状基线（已完成，别重做）

| 阶段 | 内容 | 证据 |
|---|---|---|
| Phase 1 · magpie 路由 | 网关 `127.0.0.1:3425`；Hermes provider `magpie` 挂 group/* 与单模型共 20 个名；组：`main`/`hermes-main`/`free-ds`/`简单杂活`(=`group/group`, 13 家)/`quick`/`minimax` | `POST /v1/chat/completions` 与 `hermes -m group/group -z` 均 200，`X-Magpie-Model` 可见真实承接方 |
| Phase 2a · kanban 底座 | dispatcher 60s tick；profiles：researcher / housekeeper / coder / skeptic；各自 SOUL.md、模型 pin、精简 skill 集 | 2026-11-04 三条真实任务全部 ready→running→done |
| Phase 2b · dashboard 插件 | `~/.hermes/plugins/workbench/`（manifest + index.js + style.css，无后端无构建）；三视图；今日标记存 localStorage `wb.today.v1` | 真实任务加载/勾选/恢复/拖拽/计数均通过 |
| Phase 2c · 独立版 App | `~/Projects/Jarvis/workbench-app/`；FastAPI 挂同一套 kanban router；systemd --user `jarvis-workbench.service`，`127.0.0.1:8787` | 创建/拖拽/完成/8 列/主题切换通过 |

已知限制（P1 一并解决）：固定当前 board、无任务详情、5 秒轮询、今日/排序仅浏览器本地、**看板视图是自建的（不合规，P1 移除）**。

---

## 8. 执行批次

| 批次 | 内容 | 预估 | 完成后能干什么 |
|---|---|---|---|
| **P1 界面骨架** | M5 看板对齐原生 → M1 卡契约+闸门 → M2 待办三区+拖拽+回执 | 2.5 天 | 能真派活，界面不再自建看板 |
| **P2 闭环** | M3 验收全屏模式 → M4 通知 P0（桌面+手机）→ 2d-8 会话追溯入口 | 2 天 | 派出去、跑完、推给你、你验收，闭环成立 |
| **P3 变强** | S1 自述三行 → S3 完成即固化 → 2d-3 定时唤醒 | 2.5 天 | 系统开始自己变强 |
| **P4 触发层** | 2d-4 ingest/事件接入 → 2d-5 四配方 → 2d-6 熔断+成本 | 3 天 | 不靠你手建卡也能进活 |
| **P5 持续** | 2d-2 回执协议、S2 运行画布、S4 验收槽精细化、抽样验收、热力图 | 按需 | 效率与可观测性 |

依赖关系：

```
M5 ──→ M2        先删自建 5 列，三区才不会两套并存
M1 ──→ M2        闸门决定拖拽行为
M3 ──→ M4        先有验收模式，通知才有落点
M3 ──→ S3        固化在验收那一刻触发
M2 ──→ S2        画布读同一份状态
```

---

## 9. 逐条验收标准

| ID | 事项 | 怎么算过 |
|---|---|---|
| M5 | 看板对齐原生 | 看板 tab 的 8 列名称/顺序/status 与原生一致；拖拽换列真的改状态；列定义全库只有一处常量 |
| M1 | 卡契约 + 闸门 | 建一张验收为空的卡 → 拖进流水线被拒并提示 → 补完可派发 |
| M2 | 待办三区 | 从今日拖一张到流水线 → 3 秒内出现"第 N 位 · 预计 HH:MM"；刷新后位置不丢 |
| M3 | 验收模式 | 点「待你处理 N」进全屏；按 A 后进已完成；按 R 不填理由不能提交；清空自动退出 |
| M4 | 通知 | 构造一张 P0（有子卡被堵）→ 10 秒内收到系统通知；非 P0 不打扰 |
| 2d-8 | 会话追溯 | 任取一张已完成卡 → 验收页点"打开执行会话"→ 读到该轮完整对话；dsh 任务的 `metadata.dsh_session_id` 存在且可续 |
| S1 | 自述三行 | 任一张卡验收页能看到交付/依据/没底三行，且"没底"不为空 |
| S3 | 完成即固化 | 一张卡完成后，`routing.json` 或 skill 目录出现一条新条目并标记"未验证"；下次引用后转"已验证" |
| 2d-3 | 定时唤醒 | schedule 5 分钟后到点自动回队；UI 显示"将于 HH:MM 唤醒" |
| 2d-4 | 事件接入 | 同一事件重复投递只建一张卡（idempotency_key）；新事件 5 分钟内进候选区 |
| 2d-5 | 配方 | 四套各跑一次真实事件，产出路径与跳过路径都符合预期 |
| 2d-6 | 熔断+成本 | 构造 9 层链 → 第 9 层被拒并 comment；单任务能看到估算花费 |

---

## 10. 硬技术事实与坑（动手前必读）

### 10.1 内核边界：零改源码能做到哪

| 要做的功能 | 依赖的现有能力 | 结论 |
|---|---|---|
| 任务详情 | `GET /plugins/kanban/tasks/{id}` 一次返回 task/comments/events/attachments/links/runs（实测 `t_5e3a7d36` 返回 2 评论 + 12 事件 + 1 run） | 零后端改动 |
| 显示待拍板问题 | `task.block_kind` + `tasks.result` / `latest_summary` / `block_reason` | 零改动 |
| 回答并续跑 | `POST /tasks/{id}/comments` + `PATCH /tasks/{id} {"status":"ready"}` | 零改动；blocked→ready 走 `unblock_task`，是设计好的 re-open 路径 |
| 自述/回执解析 | 前端读 `comment.body` / `run.metadata` | 零改动（解析在前端） |
| hops 计数 | 写 `task.metadata` | 零改动 |
| 分派规则 | 自家 `routing.json`，与 Hermes 无关 | 零改动 |
| **答后续原会话** | `gateway/session_kanban.py:89` `request_id = kanban:[board,task_id,run_id]` | **必须改上游内核，本计划不做** |

### 10.2 三个必须记住的坑

1. **`kanban_block` 只有 `task_id / reason / kind`，没有 metadata** —— 所以"读 `metadata.ask`"落不了地。要让 worker 在 block 前先 `kanban_comment` 写一段 ```ask 代码块（含结构化选项），前端读最近一条含该块的评论。
2. **恢复走 `ready` 不走 `todo`**：blocked→todo 是裸写状态，绕过 `unblock_task` 的 block_kind 迁移与事件；`ready` 才是正规 re-open（有未完成 parent 时会自动落 `todo`，可能返回 409，前端要处理）。
3. **`running` 不能手动设**（`_RUNNING_DIRECT_MSG`）。任何"恢复"只能落 `todo` / `ready`。

### 10.3 会话追溯链路与三处断链

```
dispatcher tick(60s) → 扫 ready（priority DESC, created_at ASC）
  → 原子 claim（claim_lock + current_run_id）
  → spawn `python -m hermes_cli.kanban_worker_client`
  → 子进程 WS 连本机 gateway → gateway/session_kanban.py:89
       request_id = "kanban:[board, task_id, run_id]"
       create_local_session(source='kanban', platform='cli')
  → 首条 user message = "# Kanban task t_xxx: <标题>"
  → kanban_complete/block → socket 关闭
```

一次 attempt 一条会话，会话 id 由 `(board, task, run)` 哈希派生。dsh 是第二层嵌套：`coder 会话 → dsh session-<uuid> → ~/.dsh/sessions/<cwd 编码>/session-<uuid>/session.v4.jsonl.zstd`。

| 位置 | 内容 | 反查 |
|---|---|---|
| `state.db → sessions` | kanban 来源会话（实测 34 条 / 总 443） | `hermes sessions list --source kanban` |
| `state.db → messages` | 单条 kanban 会话实测 32 条 | `hermes sessions export`（有 `messages_fts`） |
| `task.metadata.worker_session_id` | **Hermes 自动写入**（样本 `local-872836e0…`） | 卡片直接跳会话 |
| `kanban/logs/<task>.log` | 完整 transcript，但失败时仅 1 KB | **不可作记录档** |
| `task_events` / `task_runs` | 状态迁移 + 每轮 summary/metadata | 完整 |

`config.yaml`：`sessions.auto_prune: false`，`retention_days: 90` 仅对手动 `prune` 生效——记录不会自己消失。

三处断链的修法：

| # | 断点 | 修法 |
|---|---|---|
| 1 | 验收页无会话入口 | 读 `task.metadata.worker_session_id`（数据已在 `GET /tasks/{id}` 返回体里），前端加入口 |
| 2 | dsh session id 未回写 | `dsh-delegation` 验收条款加硬要求：`kanban_complete` 的 metadata 必须带 `dsh_session_id`（NDJSON 首行即 `{"type":"session","sessionId":"session-<uuid>"}`） |
| 3 | NDJSON 落 `/tmp` | 改到 `$HERMES_KANBAN_WORKSPACE/.dsh-runs/<ts>.ndjson`；`cd <repo>` 再跑，dsh 会话按 cwd 分组 |

### 10.4 metadata 约定

| 键 | 谁写 | 用途 |
|---|---|---|
| `bucket` | 建卡入口 | `today` / `long`，区分两个仓库 |
| `contract_defaults` | 进 ready 时 | 预算/禁区生效值快照 |
| `worker_session_id` | Hermes 自动 | 跳转执行会话 |
| `dsh_session_id` | coder worker | 续 dsh 会话 |
| `hops` | 建子卡时 | 熔断计数（超 8 层拒绝建卡，只随完成清零，不随 unblock 清零） |
| `solidified` | 验收时 | `{type, path, status: pending\|verified}` |
| `cost` | 聚合任务 | `{minutes, tokens, usd, by_rule}`，来源 `task_runs` + magpie `usage.jsonl` |

---

## 11. 触发层与配方（P4）

- **事件入口**：`POST /ingest`，统一五字段 `source / type / object / summary / link` + `confidence`，用 `idempotency_key` 去重。
- **候选区**（不是卡）：自动提取的东西先落候选，带**原文锚点**（必须能点回原邮件/原消息）；低置信度只进日报。人一键转卡时才由 AI 补契约。
- **提取器是配方，不是万能 AI**：邮件 / 私信 / 日历各一套 = 1 个 skill + 1 条 cron。
- **四条配方**：

| 配方 | 实现 | 分派 |
|---|---|---|
| morning-brief | cron 08:00 + skill，写 `briefs/YYYY-MM-DD.md`，无事 `[SILENT]` | housekeeper |
| github-issues | 5 分钟轮询 `gh api` / webhook → 候选 → 建卡 | coder |
| website-watch | 检查脚本，仅异常建卡；恢复后更新原卡 | housekeeper |
| ops-alerts | webhook → 建卡；要重启 → blocked 等回答 | coder |

- **周日大扫除**：扫过期任务、归档、清日志。
- **QQ 未来路径**：NapCat（OneBot）→ 30 行转换 → `POST /ingest`。

---

## 12. 远程面板（可选）

| 方案 | 做法 | 评价 |
|---|---|---|
| **A（推荐）** | dashboard 留 `127.0.0.1`，外出走 Tailscale | 零公网面，认证都省了 |
| B | 挂 ECS 118.31.44.30 | kanban 单机设计，ECS 上得再跑一套 Hermes，复杂度陡增 |

kanban 的单机约束决定了"工作台挂服务器"性价比低。除非要让 ECS 成为独立执行节点（跑不下机的长任务），那是 v2。

---

## 13. 安全红线

1. **masking 常开**（magpie privacy masking）
2. **高权限任务先 `block(needs_input)`**，不自动执行
3. **密钥不进 skill、不进任务书**（走环境变量或 vault）
4. **无 sudo NOPASSWD**
5. worker 不得 force push / 不得动生产库

---

## 14. 明确不做

- ❌ 自建看板后端 / 自建 todo 服务 / 任何第二套任务中台（双状态源必然漂移）
- ❌ 改上游内核实现"答后续原会话"（`run_id` 进 `request_id` 是设计，破坏它等于放弃可重试/并发安全）
- ❌ 跨主机 kanban（官方不支持）
- ❌ 引入 agent-autoloop 作为中台（只借机制：ask/later/事件去重/TTL）
- ❌ 独立任务系统（veritas-kanban / Planka / Focalboard 类）
- ❌ QQ/微信接入（只留 schema）
- ❌ 自建 CI 中台（GHA + self-hosted runner 足够）
- ❌ 本地 Qwen 分流器（v2；v1 用 magpie intent routing 顶）
- ❌ 只留一个 worker（无证据前不动分派矩阵）
- ❌ 谎报降权（本期不展开）
- ❌ state.db 备份策略（2026-11-04 明确不做；风险留档：约 2.8 GB 且为唯一存储）

**推翻"不自建看板"的三条触发条件**（任一成立才重开评估）：

1. 要接第二个执行器（Claude Code / Codex 与本机 Hermes 混跑）
2. 要手机批卡（Hermes 无好用移动端，最可能先触发）
3. 要多人使用、分权限

---

## 15. 决策账

### 15.1 已定（2026-11-04）

| # | 问题 | 结论 | 落地影响 |
|---|---|---|---|
| 1 | 第 N 次成本怎么记 | **从 `task_runs` + magpie `usage.jsonl` 聚合** | S3 写 `metadata.cost`；按 routing 规则名分组出曲线 |
| 2 | 手机通道 | **暂时不做**，P0 只走桌面系统通知 | M4 缩范围；notify 接口预留，v0.7 再接 ntfy/Bark |
| 3 | 固化物权限 | **worker 可以自己改自己 profile 的 `skills/`** | S3 不需要人合；跨 profile 仍要人合，且先标 `pending` |

### 15.2 待拍板（1 项）

1. **远程面板**：A（Tailscale + 本机，推荐）还是 B（ECS）？

> P1–P4 都不依赖此项，可以直接开工。

---

## 16. 附录 · 社区同类项目

| 项目 | 星 | 思路 | 与本方案的差异 |
|---|---|---|---|
| BradGroux/veritas-kanban | 851 | 独立任务板 + 可选 AI 编排层 | 自带存储，用就丢 Hermes 调度器——双库 |
| GumbyEnder/hermes-kanban | 277 | Obsidian 插件 + REST bridge | 多一层 bridge，Obsidian 关掉就看不到 |
| 8bit64k/cronalytics | 115 | cron 观测插件 | 只监控不录入 |
| xraysight/hermes-memory-ui | 77 | 只读 memory 仪表盘 | 同模式参考 |
| joeynyc/hermes-chronos-forge | 42 | cockpit 插件（gateway/sessions/token/cron） | 可借布局 |
| stasstepv/hermes-pwa | 37 | 手机 PWA 插件 | 手机端参考 |
| KKKKhazix/agent-autoloop | 748 | 事件→规则→CLI Agent→回执卡片，无 UI，MIT | 只借机制（ask/later/事件去重/TTL） |

**空白点**：没有人做"更好用的 Hermes kanban 人机工作台"。这正是本项目的位置，且是官方插件机制内的合法扩展。