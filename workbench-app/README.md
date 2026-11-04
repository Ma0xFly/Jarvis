# Jarvis 工作台（独立版）

把原来只能在 Hermes dashboard 里打开的 `workbench` 插件，拆成一个**独立本地应用**：
自己的进程、自己的端口、自己的窗口。任务数据仍然读写同一份 `kanban.db`；
派发契约（验收 / 预算快照 / bucket）存在工作台自有的 `workbench.db`。

```
浏览器 / Chrome app 窗口
        │  http://127.0.0.1:8787
        ▼
workbench-app/server.py  (FastAPI)
        ├─ /api/plugins/kanban/*  → upstream kanban REST router (mounted)
        ├─ /api/workbench/*       → dispatch / metadata / ready gate
        └─ /static, /             → own frontend (workbench-ui build)
        │
        ├──▶ ~/.hermes/kanban.db          task state (upstream REST only)
        └──▶ workbench-app/workbench.db   contract metadata (workbench-owned)
```

## 为什么不是复制一套数据

`server.py` 不自己写任务 SQL，而是用 `importlib` 加载 Hermes 自带的
`plugins/kanban/dashboard/plugin_api.py`，把它原样挂到
`/api/plugins/kanban/*`。所以：

- 看板、任务、评论、完成历史全部来自 `kanban.db`
- CLI、gateway 调度器、Hermes dashboard、这个独立 app 看到的是同一份状态
- 工作台端点（`/api/workbench/*`）不 import 上游内部模块，只通过回环 REST
  读写任务；不打开、不建表、不写 `kanban.db`

`workbench.db` 只存工作台自己的 `task_metadata(task_id, metadata, write_token)`：
`bucket`、`contract`、派发瞬间的 `contract_defaults` 快照。`write_token` 是快照
归属标记（补偿时只删自己写的），不会出现在 metadata 响应里。不碰共享
`kanban.db`，上游升级重建表/加同名列都不会影响。

## 启动

```bash
cd ~/Projects/Jarvis/workbench-app
./run.sh                 # 前台，默认 127.0.0.1:8787
```

装成用户服务（已执行）：

```bash
./install.sh             # systemd --user + 桌面入口，不需要 sudo
systemctl --user status jarvis-workbench
```

打开方式：

- 浏览器：<http://127.0.0.1:8787>
- 应用菜单：**Jarvis 工作台**（Chrome `--app` 独立窗口）
- 或 `./open.sh`

## 端口 / 远程

默认只监听 `127.0.0.1:8787`。要在手机/外面访问，建议先上 Tailscale，再改：

```bash
WORKBENCH_HOST=0.0.0.0 systemctl --user edit jarvis-workbench
```

（或在 `~/.config/systemd/user/jarvis-workbench.service` 里加
`Environment=WORKBENCH_HOST=0.0.0.0`，然后 `systemctl --user restart`。）
没有内置登录，**不要直接暴露公网**。

派发自调用会解析启动时的 `--host/--port`；绑定 `0.0.0.0` / `::` 时回落到
`127.0.0.1`。如需让工作台调用另一个本机上游（测试/代理场景），可设
`WORKBENCH_UPSTREAM_BASE=http://127.0.0.1:<port>`。

隔离实例/测试可用 `WORKBENCH_DB` 与 `WORKBENCH_DEFAULTS` 覆盖 metadata 库和
defaults 文件路径；默认分别是 `workbench-app/workbench.db` 与
`workbench-app/defaults.json`。

## 派发闸门与契约 metadata

```bash
# 派发（契约已在卡上，body 为空）
curl -s -X POST http://127.0.0.1:8787/api/workbench/dispatch/<task_id> \
  -H 'content-type: application/json' -d '{}'
# 200 {"ok":true,"position":2,"eta":"14:19","status":"ready"}
# 400 missing_acceptance / 404 not_found / 409 blocked_by_parent|not_ready|not_dispatchable
# 500 invalid_defaults / 502 upstream_unavailable / 503 metadata_unavailable

# 批量读 workbench metadata（无记录的 id 省略）
curl -s 'http://127.0.0.1:8787/api/workbench/metadata?ids=<task_id>,<task_id>'
```

规则：

- `metadata.contract.accept` 为空 → 400，卡状态不变、不入队。中间件同时拦住
  `PATCH status=ready`、无 `triage` 的建卡（1.2 统一要求建卡显式 triage，宁可多拦
  也不留 ready 绕过）、`bulk status=ready` 和 `reclaim`。
  PATCH/reclaim 先 GET 卡，404 原样转发，不会误报 400；卡存在才检查验收。
- 预算生效值 = `defaults.json` 全局默认 + 卡上 `metadata.contract.budget`
  （routing 层 deferred）。派发瞬间把 `max_minutes` / `max_cost` /
  `deny_paths` 三键快照进 `metadata.contract_defaults`。
- 有未完成 parent → 409 `blocked_by_parent`，卡落 `todo`，不重复入队。
- `PATCH status=ready` 成功后会用 **GET `/tasks/{id}`** 读真实落点；只有落点
  确实是 `ready` 才读 `/board` 算位次（board 未命中会重读一次，仍不命中返回 502）。
- 已 `ready` 再派发是幂等的：返回 200，按当前 ready 队列重算位次，
  不覆盖已有快照。
- 位次按 `/board` 的 `ready` 列 1-based 顺序；`eta = now + minutes_per_task × 位次`。
  位次 1 表示第一个 7 分钟槽。
- `running` 卡重派返回 `{position:0,status:"running"}`，不 PATCH；done/archived
  直接 409 `not_dispatchable`，这一步不依赖验收/metadata 库。

全局默认只在 `defaults.json`（`max_minutes=120`、`max_cost=5`、
`deny_paths=[".git","~/.ssh","~/.hermes","node_modules",".env"]`、
`queue.minutes_per_task=7`）；前端不写死任何预算数值。上限：
`max_minutes ≤ 10080`、`max_cost ≤ 1000`、`minutes_per_task ≤ 1440`。

上游 `recompute_ready`（parent 完成自动提升）和原生 9119 dashboard 不经过本服务，
**不在闸门范围内**，属已知限制。

## 界面

- **看板**：8 列 `待整理 / 待执行 / 已排期 / 就绪 / 运行中 / 待拍板 / 待验收 / 已完成`
- 明暗主题：右上角 ☾/☀，记住在 `localStorage["wb.theme"]`
- 5 秒轮询 `/api/plugins/kanban/board`，不是 WebSocket

待办三区与拖拽派发属于 Story 1.3；契约/bucket 的写端点也留给 1.3。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI：挂 kanban router + `/api/workbench/*` + PATCH-ready 闸门 + 静态页 + `/healthz` |
| `defaults.json` | 预算与队列估算的唯一全局默认源 |
| `workbench.db` | 运行期生成的工作台 metadata 库（已 gitignore） |
| `tests/test_dispatch.py` | 派发/闸门/metadata 的可重复集成测试（临时库，不碰真实数据） |
| `web/index.js` / `web/style.css` | `workbench-ui` 的构建产物（禁止手改） |
| `web/index.html` / `web/icon.svg` / `web/theme.css` | 独立版页面骨架与主题 |
| `install.sh` | 用户级 systemd 服务 + 桌面入口 |
| `run.sh` / `open.sh` | 启动脚本 / 打开独立窗口 |

## 前端源码与构建

唯一源码在 `workbench-ui/src/`（React + TS + Vite，双产物）：

```bash
cd ~/Projects/Jarvis/workbench-ui
npm run typecheck
npm run build        # 产物分发到插件宿主与 workbench-app/web/
systemctl --user restart jarvis-workbench
```

构建会同时生成插件 IIFE 与独立版 bundle；不要再手改 `workbench-app/web/index.js`，
也不要回退到旧的 `sync-ui.sh` 流程。

## 测试

```bash
python3 workbench-app/tests/test_dispatch.py -v
```

测试会为每个套件创建临时 `HERMES_KANBAN_HOME` 与 `WORKBENCH_DB`，并用
`~/.hermes/tools/python-3.14*/bin/python3`（或 `WORKBENCH_PYTHON` 指定）启动
隔离服务。覆盖：验收闸门（PATCH/create/bulk/reclaim）、404 透传、parent 停卡、
review/todo 落点、落地 GET、`/board` 失败与重读、幂等重派、快照 `write_token`
归属、done/archived、running、非法 defaults 与卡覆盖、201-id 分块、上游挂起超时、
PATCH 404、metadata 库故障、并发派发与共享 `kanban.db` 无 workbench 对象。

## 已知限制

- 固定当前 board，不切换多 board；metadata 只按 `task_id` 键（多 board 支持 deferred）
- 1.2 只提供 metadata 读端点与派发快照；建卡/编辑契约的写端点属 Story 1.3
- 待办三区与拖拽 UI 属 Story 1.3
- 1.2 不实现 provenance / `contract.ready` 语义；只使用 `contract.input/accept/budget`
- 上游 `recompute_ready` 与原生 9119 dashboard 不经过 ready 闸门；不额外加 watcher
- 自调用仍是独立 `urllib` 请求（dispatch 在线程池执行），不共享连接池
- 已跟踪的 `web/*.bak-*` 与旧 `web/app.js` / `sync-ui.sh` 清理 deferred；本次不删已跟踪文件
- 没有任务详情抽屉（看详情仍可用 dashboard 的 Kanban 页）
- 5 秒轮询，不是 WebSocket
- 「今日/长期」归属以 `metadata.bucket` 为准；`localStorage` 只存排序与主题等 UI 偏好
- 无登录，仅限本机 / Tailscale

## 卸载

```bash
systemctl --user disable --now jarvis-workbench
rm -f ~/.config/systemd/user/jarvis-workbench.service
rm -f ~/.local/share/applications/jarvis-workbench.desktop
rm -rf ~/Projects/Jarvis/workbench-app
```