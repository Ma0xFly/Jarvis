---
title: '1.1 看板 tab 对齐 Hermes 原生 8 列'
type: 'refactor'
created: '2026-11-04'
status: 'draft'
route: 'dispatch'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 看板 tab 的 8 列语义分散在 `STATUS_ORDER`（键序数组）和 `STATUS`（键→label/color 映射）两处，`ENTER_HINT` 里还硬编码了「待整理 / 待执行 / 已排期」这些列名。列名一改就得分头找，与 Hermes 原生 8 列对齐这件事没有单点可依。

**Approach:** 把 8 列收敛成一个 `COLUMNS` 常量（`key` + `label` + `color`，顺序即 native 顺序），渲染、查找、提示文案全部从它派生；卡片字段对齐原生（id / 标题 / priority / assignee / block_kind / latest_summary / 相对时间），不自造状态徽章。**不嵌入 iframe，不打开 dashboard 页面**——看板仍由工作台自有前端渲染。

## Boundaries & Constraints

**Always:**
- 只改 `~/.hermes/plugins/workbench/dashboard/index.js` 与 `style.css`，改完必须跑 `./sync-ui.sh` 生成 `workbench-app/web/app.js`；副本绝不手改
- 8 列 key/label 固定为：`triage` 待整理 / `todo` 待执行 / `scheduled` 已排期 / `ready` 就绪 / `running` 运行中 / `blocked` 待拍板 / `review` 待验收 / `done` 已完成，顺序不变
- 列名与 8 列数组全库只出现在 `COLUMNS` 一处；由它 `map`/`reduce` 出来的查找表不算第二份定义
- 拖拽仍走原生状态机（`PATCH {status}`）：`running` 不可手动设，被拒要有中文原因
- 明暗双主题走既有 token，不硬编码色值

**Never:**
- 不嵌入 iframe、不打开 `9119` dashboard 页面
- 不改后端（`server.py` / `plugin_api.py`）、不新增端点、不引入依赖
- 不动待办三区与验收模式（属 Story 1.3 / M2）

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开看板 | 任意数据 | 8 列横排，列名/顺序与原生一致，无第 9 列、无「队列/待收」旧列 | N/A |
| 就绪 → 运行中 | 拖拽 | 拒收，卡片回原位并提示 `running` 不能手动设 | 客户端拦，不发请求 |
| 待验收 → 已完成 | 拖拽 | `PATCH status=done` 成功，刷新后仍在「已完成」 | 409 时按原逻辑 toast 原因 |
| 卡片缺字段 | `assignee`/`block_kind`/`latest_summary` 为空 | 对应片段不渲染，不占位、不显示 `null` | N/A |
| 列多到放不下 | 窄视口 | 看板横向滚动，列不被压到不可读 | N/A |

</frozen-after-approval>

## Code Map

- `~/.hermes/plugins/workbench/dashboard/index.js:23-33` — `STATUS_ORDER`(23) + `STATUS`(24-33)：两个结构合并成 `COLUMNS`，派生 `COL_BY_KEY` 与 `COL_KEYS`
- `.../index.js:38-51` — `ENTERABLE`(40) 保留；`ENTER_HINT`(46) 的句子硬编码列名，改为按 `label(key)` 生成
- `.../index.js:52` — `statusLabel(s)` 改为查 `COL_BY_KEY`
- `.../index.js:79-86` — `timeLabel(ts)` 已有相对时间格式化，卡片时间直接复用（注意 API **没有** `updated_at`，用 `completed_at || created_at`）
- `.../index.js:417-472` — `renderFlow`：列头(436) 与卡片体(443-457) 改成遍历 `COLUMNS`；卡片补齐 `block_kind` / `latest_summary` / 时间
- `.../index.js:467` — `["triage","todo","ready"]` 是「待启动」计数的状态集合，不是列定义，保留
- `.../style.css:87-110` — `/* flow */` 段：`.wb-board`(89) 横向滚动、`.wb-col`(90-94) 改固定基准宽，保证 8 列不挤
- `workbench-app/web/app.js` — `sync-ui.sh` 生成物，只读勿改；`workbench-app/server.py:79-81` 的 `/healthz` 用作验证探针

## Tasks & Acceptance

**Execution:**
- [ ] `~/.hermes/plugins/workbench/dashboard/index.js` -- 用单个 `COLUMNS` 替换 `STATUS_ORDER`+`STATUS`，派生 `COL_BY_KEY`/`COL_KEYS`；`renderFlow` 遍历 `COLUMNS`；`ENTER_HINT` 文案从 `label()` 生成；卡片按原生字段渲染（id/标题/`P{n}` 优先级/assignee/`block_kind`/`latest_summary`/`timeLabel`），空字段跳过 -- 列名单点，卡片不自造
- [ ] `~/.hermes/plugins/workbench/dashboard/style.css` -- `/* flow */` 段调整为 8 列横排 + 横向滚动，列有可读最小宽 -- 窄视口不挤压
- [ ] `workbench-app/sync-ui.sh` -- 同步到 `web/app.js`、`web/style.css` 并重启验证 -- 独立版生效

**Acceptance Criteria:**
- Given 服务已重启, when 打开「看板」tab, then 呈现 8 列，列名与顺序与原生逐项一致，无第 9 列、无「队列/待收」这类旧列
- Given 一张卡在「就绪」, when 拖到「运行中」, then 请求被拒、卡片回原位、提示 `running` 不可手动设置
- Given 一张卡在「待验收」, when 拖到「已完成」, then `PATCH status=done` 成功，刷新后仍在「已完成」列
- Given 代码库, when 检索 `待整理`/`待执行`/`已排期` 等列名, then 只命中 `COLUMNS` 定义处；不存在第二份 8 列数组
- Given 任意一张卡, when 观察内容, then 只出现 id/标题/priority/assignee/block_kind/latest_summary/时间中实际有值的字段，无自造状态徽章
- Given 改动完成, when 执行 `./sync-ui.sh && systemctl --user restart jarvis-workbench && curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz`, then 返回 `{"ok":true}`，控制台无报错

## Implementation Notes

## Spec Change Log

## Review Triage Log

## Design Notes

- **与 Story 背景的一处出入**：Story 说「现有 `app.js` 自建了 5 列（队列/运行中/待收/今日仓库/已完成）」，但当前代码实测已是 8 列，且 label 与目标表逐字一致（`index.js:23-33`）。真正缺的是「单点定义」和「卡片字段对齐」，所以本 spec 按证据写，不按背景描述去删一个并不存在的 5 列映射。PLAN §5.3 已注明那 5 列是草稿 007 的错误示例。
- PLAN §5.3 提到「后端校验复用 `COLUMNS`」——跨语言共享常量不在 Story 1.1 范围（本故事明确「不改后端」），留作后续。
- 原生卡片确实显示短 id 与 `P{n}` 优先级（实测原生看板卡片形如 `t_d8270296` + 标题 + `P2` + `unassigned` + `4h ago`），因此保留 id、把原来的「高优先」改为 `P{n}`（`priority > 0` 才显示）是「对齐」而不是「新增」。
- 拖拽相关两条 AC 的现状：`running` 拦截与 `review→done` 在现有 `flowDrop` + 后端 `complete_task(force=True)` 下已经成立，本故事只做回归验证，不重写逻辑。

## Verification

**Commands:**
- `cd ~/Projects/Jarvis/workbench-app && ./sync-ui.sh` -- expected: 打印「已同步：app.js / style.css」
- `systemctl --user restart jarvis-workbench` -- expected: 退出码 0
- `curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz` -- expected: `{"ok":true}`
- `grep -n "待整理\|待执行\|已排期" ~/.hermes/plugins/workbench/dashboard/index.js workbench-app/web/app.js` -- expected: 每处只命中 `COLUMNS` 定义行
- `diff -q workbench-app/web/app.js ~/.hermes/plugins/workbench/dashboard/index.js` -- expected: identical

**Manual checks:**
- 8787 →「看板」：8 列名称/顺序对原生，窄窗口下横向可滚动，Console 无报错
- 拿一张卡「就绪 → 运行中」：被拒并提示；「待验收 → 已完成」：成功且刷新后仍在「已完成」
- 明暗主题各看一遍：列底、卡片、标签都跟随主题