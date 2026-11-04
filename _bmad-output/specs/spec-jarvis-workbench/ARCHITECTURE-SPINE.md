---
id: ARCH-jarvis-workbench
status: final
updated: 2026-11-04
spec: SPEC.md
---

# 架构基线：Jarvis 人机工作台

> 给执行体（dsh / coder）看的施工图。**先读完这份再动代码**；改架构先改这份文件。

## 1. 运行时全景

```
┌──────────────────────────── 本机（单机单库） ────────────────────────────┐
│                                                                         │
│  Hermes gateway (常驻)              workbench-app (systemd --user)        │
│  ├─ kanban dispatcher 60s tick      ├─ FastAPI 127.0.0.1:8787           │
│  ├─ WS 会话服务                      ├─ 挂 /api/plugins/kanban/*（复用） │
│  └─ 派发 worker → kanban_worker      ├─ 挂 /static（自有前端）           │
│        │                            └─ /healthz                         │
│        ▼                                     ▲                          │
│   researcher / housekeeper / coder           │ HTTP（同源，无 CORS）     │
│        │                                     │                          │
│        └─ coder → dsh headless              浏览器 / Chrome --app 窗口    │
│                                                                         │
│  Hermes dashboard 127.0.0.1:9119 ── 原生 kanban 页（看板 tab 的 iframe）  │
│  magpie gateway 127.0.0.1:3425 ── 模型路由（worker 的模型来源）           │
└─────────────────────────────────────────────────────────────────────────┘

唯一状态源：~/.hermes/kanban.db
会话记录：~/.hermes/hermes-agent/state.db（只读反查）
成本流水：~/.config/magpie/usage.jsonl（只读聚合）
```

## 2. 代码地图

| 路径 | 角色 | 能不能改 |
|---|---|---|
| `workbench-app/server.py`（99 行） | FastAPI 外壳：importlib 加载 Hermes 的 `plugins/kanban/dashboard/plugin_api.py`，原样挂到 `/api/plugins/kanban` | **可改**，只加不改 |
| `workbench-app/web/app.js`（581 行） | 前端主逻辑 —— **它是副本** | 不要直接改，见 §3 |
| `workbench-app/web/style.css` | 样式副本 | 同上 |
| `workbench-app/web/theme.css`、`index.html`、`boot.js`、`sdk-shim.js` | 独立版外壳（主题 token、页面骨架、把 dashboard SDK 映射成 fetch） | 可改 |
| `~/.hermes/plugins/workbench/dashboard/index.js`、`style.css` | **前端源文件**（dashboard 插件形态） | **改这里** |
| `~/.hermes/plugins/workbench/plugin.yaml` | 插件清单（frontend-only，无后端能力） | 尽量不动 |
| `~/.hermes/hermes-agent/plugins/kanban/dashboard/plugin_api.py` | 上游 kanban REST（4000+ 行，提供全部读写） | **禁止改** |
| `~/.hermes/kanban.db` | 唯一状态源 | **禁止直接写 SQL**，一律走 REST/CLI |
| `_bmad-output/specs/spec-jarvis-workbench/` | SPEC + 本文件 + PLAN | 文档 |
| `.agents/skills/`、`_bmad/` | BMad 工具链 | 禁止改 |

## 3. 双向同步的方向（最容易搞错的一条）

```
~/.hermes/plugins/workbench/dashboard/index.js   ← 源（先改这里）
              │  workbench-app/sync-ui.sh（单向复制）
              ▼
workbench-app/web/app.js                          ← 副本（下次 sync 会被覆盖）
```

- 改 UI：改插件那份 → `./sync-ui.sh` → `systemctl --user restart jarvis-workbench`。
- 只改 `web/app.js` 会在下次同步时丢掉。
- 若某个能力只属于独立版（例如调用 `/api/workbench/*`），用 `web/boot.js` 或 `sdk-shim.js` 做适配，不要污染共享的 `app.js`。

## 4. REST 契约（已核实）

基址 `http://127.0.0.1:8787/api/plugins/kanban`。

| 方法 | 路径 | 关键字段 | 用途 |
|---|---|---|---|
| GET | `/board` | — | 当前 board 全量（列 + 卡） |
| GET | `/tasks/{id}` | — | 单卡全量：task / comments / events / attachments / links / runs |
| POST | `/tasks` | `title` `body` `assignee` `priority` `metadata` | 建卡 |
| PATCH | `/tasks/{id}` | `status` `assignee` `priority` `title` `body` `result` `summary` `metadata` `block_reason` | 改卡；`metadata` 可整块写 |
| POST | `/tasks/{id}/comments` | `body` | 加评论（结构化提问走 ```` ```ask ```` 块） |
| POST | `/tasks/bulk` | `ids` `status` `metadata` … | 批量改 |
| GET | `/workers/active` | — | 当前在跑的 worker |
| GET | `/stats` · `/assignees` · `/diagnostics` | — | 统计/可派工 profile/自检 |
| GET | `/runs/{run_id}` · `/runs/{run_id}/inspect` | — | 单轮详情与 transcript |

**状态硬规则**（踩过）：

1. `running` **不能**通过 PATCH 设置（会有 `_RUNNING_DIRECT_MSG`）。
2. 从 `blocked` 恢复必须落 `ready`（走 `unblock_task`）；落 `todo` 是裸写，会丢事件。
3. 有未完成 parent 时 `→ready` 可能返回 **409**，前端要处理成"已转 todo"。
4. `PATCH` 里 `None` = 不修改；要清空用显式 `clear_*` 字段。

## 5. 数据契约（metadata）

| 键 | 写方 | 结构 |
|---|---|---|
| `bucket` | 建卡入口 | `"today"` \| `"long"` |
| `contract` | 建卡/派发 | `{input: string, accept: string, ready: bool}` |
| `contract_defaults` | 派发瞬间 | `{max_minutes, max_cost, deny_paths[]}` —— 生效值快照 |
| `solidified` | 验收 | `{type: "routing"\|"skill"\|"script", path, status: "pending"\|"verified"}` |
| `cost` | 聚合任务 | `{minutes, tokens, usd, by_rule}` |
| `hops` | 建子卡 | `int`，> 8 拒绝 |
| `dsh_session_id` | coder worker | `"session-<uuid>"` |

`bucket` 用 `metadata` 而不是新状态：**不占用 `todo`**，仓库卡统一 `triage`。

## 6. 扩展点（本期新增代码只允许落在这里）

1. **`workbench-app/server.py` 增加 `/api/workbench/*` 路由**
   - `POST /api/workbench/dispatch/{task_id}` —— 校验 `contract.accept` 非空 → 写 `contract_defaults` 快照 → `PATCH status=ready`；返回 `{position, eta}`。
   - `GET /api/workbench/inbox` —— 聚合 `review` 卡 + P0 blocked 卡，供右上角计数。
   - `POST /api/workbench/verify/{task_id}` —— `{action: accept|reject, reason?}`；accept 校验 `solidified` 已登记 → `PATCH status=done`；reject 写 comment + 回 `triage`。
   - `GET /api/workbench/cost/{task_id}` —— 读 `metadata.cost`，缺失时按需聚合。
   实现方式：与现有 router 平级，**不要改 `plugin_api.py`**。

2. **前端新视图**放在插件 `index.js` 里新增渲染函数；独立版通过 `sync-ui.sh` 取到。

3. **规则与固化物**落 `~/Projects/Jarvis/routing.json`（单一文件，仓库内）。

## 7. 三条不改的边界

- **上游零改动**：`hermes-agent/` 下任何文件不动。需要的字段若 REST 不返回，从前端二次请求补，不改后端。
- **数据库只读**：`kanban.db` / `state.db` 只允许通过 REST、CLI 或 `sqlite3 -readonly` 查询。
- **看板不重画**：看板 tab = iframe `http://127.0.0.1:9119/kanban` + 外链兜底；不自建列渲染。

## 8. 本地开发与验证

```bash
# 起服务（改动后重启）
cd ~/Projects/Jarvis/workbench-app
systemctl --user restart jarvis-workbench
curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz          # {"ok":true}

# 另开一个端口做实验，不打断正在用的服务
WORKBENCH_PORT=8788 ./run.sh &

# 造一张测试卡（真实写库，用完记得删）
curl -s -X POST http://127.0.0.1:8787/api/plugins/kanban/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"[测试] 契约闸门","body":"验证用","metadata":{"bucket":"today"}}'

# 读单卡全量
curl -s http://127.0.0.1:8787/api/plugins/kanban/tasks/<id>
```

运行 Python：`~/.hermes/tools/python-3.14*/bin/python3`（`run.sh` 自动找），依赖由 `hermes_bootstrap` 注入，**不要**自建 venv。

## 9. 已知限制（别当成 bug 去修）

- 固定单一 board，不做多 board 切换。
- 5 秒轮询，不是 WebSocket。
- 「今日」与排序存 `localStorage`（`wb.today.v1` / `wb.repoOrder.v1` / `wb.theme`），跨浏览器不同步。
- 无登录，只监听回环；远程走 Tailscale。
- 插件形态没有后端，只能做前端与 kanban REST；需要自定义端点时只能走独立版 8787。

## 10. 版本基线

| 组件 | 版本/位置 |
|---|---|
| Hermes | v0.22.7（`~/.hermes/hermes-agent`） |
| magpie | `127.0.0.1:3425`，组 `group/group`（杂活）、`group/main`（长编码） |
| dsh | 0.2.1-rc.5，`dsh --profile headless --json` |
| Python | 3.14（Hermes 运行时） |
| OS | Ubuntu 26.04.1 LTS，GNOME 50.1 Wayland |