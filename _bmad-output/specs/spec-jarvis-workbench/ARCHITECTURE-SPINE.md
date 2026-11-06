---
id: ARCH-jarvis-workbench
status: final
updated: 2026-11-05
spec: SPEC.md
---

# 架构基线：Jarvis 人机工作台

> 给执行体（dsh / coder）看的施工图。**先读完这份再动代码**；改架构先改这份文件。

## 1. 运行时全景

```
┌───────────────────────────────────────────────────────────────────────────────┐
│   Hermes gateway (always on)        workbench-app (systemd --user)            │
│   ├─ kanban dispatcher 60s tick     ├─ FastAPI 127.0.0.1:8787                 │
│   ├─ WS session service             ├─ /api/plugins/kanban/* (mount)          │
│   └─ worker dispatch → kanban_worker├─ /api/workbench/* (dispatch/metadata/    │
│                                      │   inbox/count/verify/sweep)             │
│         │                           ├─ /static (own frontend)                 │
│         │                           └─ /healthz                               │
│         ▼                                    ▲                                │
│   researcher / housekeeper / coder           │ HTTP (same origin, no CORS)    │
│         │                                    │                                │
│         └─ coder → dsh headless              browser / Chrome --app           │
│                                                                               │
│   Hermes dashboard 127.0.0.1:9119 ── native board (reference only)            │
│   magpie gateway 127.0.0.1:3425 ── model routing (worker source)              │
└───────────────────────────────────────────────────────────────────────────────┘

唯一任务状态源：~/.hermes/kanban.db（只经 REST）
工作台契约 metadata：workbench-app/workbench.db（workbench 自有，按 task_id）
会话记录：~/.hermes/hermes-agent/state.db（只读反查）
成本流水：~/.config/magpie/usage.jsonl（只读聚合）
```

## 2. 代码地图

| 路径 | 角色 | 能不能改 |
|---|---|---|
| `workbench-app/server.py` | FastAPI 外壳：挂载上游 kanban router；自有 `/api/workbench/*`（dispatch、metadata、inbox/count、verify、reviews/sweep）与 ready 闸门；只经回环 REST 碰任务 | **可改**，只加不改 |
| `workbench-app/defaults.json` | 全局预算 + `queue.minutes_per_task`（唯一默认源） | 可改 |
| `workbench-app/workbench.db` | 工作台自有 `task_metadata`（运行期生成，gitignore） | 可改（只经 `server.py`） |
| `workbench-app/tests/test_dispatch.py` | 派发/闸门/metadata 集成测试（临时库） | 可改 |
| `workbench-ui/src/` | **前端唯一源码**（React + TS） | **只改这里** |
| `workbench-ui/src/api/workbench.ts` | `/api/workbench/*` 客户端 + 错误码映射 | 可改 |
| `workbench-app/web/index.js`、`style.css` | 独立版构建产物 | 产物，禁止手改 |
| `workbench-app/web/theme.css`、`index.html`、`icon.svg` | 独立版外壳（主题 token、页面骨架） | 可改 |
| `~/.hermes/plugins/workbench/dashboard/index.js`、`style.css` | 插件构建产物（`manifest.entry` / `.css` 指向它们） | 产物，禁止手改 |
| `~/.hermes/plugins/workbench/plugin.yaml` | 插件清单（frontend-only，无后端能力） | 尽量不动 |
| `~/.hermes/hermes-agent/plugins/kanban/dashboard/plugin_api.py` | 上游 kanban REST（4000+ 行，提供全部读写） | **禁止改** |
| `~/.hermes/kanban.db` | 唯一任务状态源 | **禁止直接写 SQL**，一律走 REST/CLI |
| `_bmad-output/specs/spec-jarvis-workbench/` | SPEC + 本文件 + PLAN | 文档 |
| `.agents/skills/`、`_bmad/` | BMad 工具链 | 禁止改 |

## 3. 构建流水线（唯一源 → 两份产物）

```
workbench-ui/src/                 ← 唯一源码（React + TS）
        │  npm run build
        ├─ index.js + style.css → ~/.hermes/plugins/workbench/dashboard/
        └─ index.js + style.css → workbench-app/web/
```

- **只改 `workbench-ui/src/`**。两个宿主目录里的 `index.js` / `style.css` 是产物，手改下一次构建就丢。
- 宿主差异只允许出现在 `src/main.tsx`：有 dashboard SDK → 注册 tab；有 `#workbench-root` → 整页渲染。
- 旧 `sync-ui.sh` 作废。

### 3.1 两套 build 的差异（关键）

dashboard 宿主已经把 React 注入到 `window.__HERMES_PLUGIN_SDK__`（`React` / `hooks` / `fetchJSON` / `authedFetch`）。
**插件包不能再打包一份 React**——双 React 会让 hooks 直接崩。

| | plugin build | standalone build |
|---|---|---|
| react / react-dom | `external` + `output.globals` 指到宿主 | 正常打包 |
| 输出 banner | `var React = window.__HERMES_PLUGIN_SDK__.React;` | 无 |
| 数据层 | `SDK.fetchJSON`（带 loopback token） | 原生 `fetch` |
| 出口 | `PLUGINS.register("workbench", App)` | `createRoot(#workbench-root).render(<App/>)` |

源码只有一份，运行期按宿主选择数据层：

```ts
const sdk = window.__HERMES_PLUGIN_SDK__
export const api = sdk ? sdk.fetchJSON : plainFetch
```


## 4. REST 契约（已核实）

基址 `http://127.0.0.1:8787/api/plugins/kanban`。

| 方法 | 路径 | 关键字段 | 用途 |
|---|---|---|---|
| GET | `/board` | — | 当前 board 全量（列 + 卡） |
| GET | `/tasks/{id}` | — | 单卡全量：task / comments / events / attachments / links / runs |
| POST | `/tasks` | `title` `body` `assignee` `priority` `triage` `idempotency_key` | 建卡；workbench 建卡固定 `triage=true` 并用 `idempotency_key` 去重（同一 key 返回已有未归档卡） |
| PATCH | `/tasks/{id}` | `status` `assignee` `priority` `title` `body` `result` `summary` `metadata` `block_reason` | 改卡；`metadata` 可整块写 |
| POST | `/tasks/{id}/comments` | `body` | 加评论（结构化提问走 ```` ```ask ```` 块） |
| POST | `/tasks/bulk` | `ids` `status` `metadata` … | 批量改 |
| GET | `/workers/active` | — | 当前在跑的 worker |
| GET | `/stats` · `/assignees` · `/diagnostics` | — | 统计/可派工 profile/自检 |
| GET | `/runs/{run_id}` · `/runs/{run_id}/inspect` | — | 单轮详情与 transcript |

工作台**只依赖公开 REST**：`GET /board`、`GET /tasks/{id}`、`PATCH /tasks/{id}`、`POST /tasks`、
`POST /tasks/{id}/comments`、`GET /runs/{run_id}`（terminate 映射）；闸门另行拦截
`reclaim` / `reassign` / `runs/{id}/terminate` 的转发。`server.py` 不 import
`kanban_db` / `kanban_db_connect`，不依赖私有函数。

### 4.1 工作台自有 REST（`/api/workbench/*`，仅独立版 8787）

| 方法 | 路径 | 关键字段 | 用途 |
|---|---|---|---|
| POST | `/dispatch/{task_id}` | `{}` | 派发：校验 `contract.accept` → 写 `contract_defaults` 快照 → `PATCH ready` → 回读落点/位次 |
| GET | `/metadata?ids=a,b` | — | 读 `workbench.db` 的 `bucket` / `contract` / `contract_defaults`；>200 id 返回 400 `too_many_ids` |
| PATCH | `/metadata/{task_id}` | `bucket` / `contract` | 合并写；`contract_defaults` 与未知键不可写 |
| POST | `/tasks` | `title` `bucket` `idempotency_key?` | 建待办卡（上游 `triage=true`），再写默认 `bucket` + 空 `contract` |
| GET | `/inbox` | — | review 队列（纯读）：并行拉详情、按最早 `review_requested` 排序；失败详情进 `failed_ids` |
| GET | `/inbox/count` | — | 顶栏计数：只读一次 `/board`（1s 短缓存 + in-flight 合并） |
| POST | `/reviews/sweep` | `{}` | 48h 自动收扫描（同源 + JSON guard）；后台 5 分钟 sweeper 共用同一逻辑 |
| POST | `/verify/{task_id}` | `action: accept\|reject\|skip`, `reason?` | accept → `done`；reject 理由 1–2000 字 → comment + `triage`；skip 不改状态 |

`/inbox`、`/inbox/count` 只读；48h 写动作只能走 `/reviews/sweep` 或后台 sweeper。
验收/待办只在独立版 8787 提供；插件宿主没有这些端点，只渲染看板 tab。

**ready 闸门覆盖的入口**（都在 workbench 中间件里）：

| 入口 | 规则 |
|---|---|
| `PATCH /tasks/{id}` + `status=ready` | 先 GET 卡：404 原样转发上游；存在再查 `workbench.db` 的 `contract.accept`，缺失才 400 `missing_acceptance`，不转发 |
| `POST /tasks` | 无 `triage` 一律 400 `missing_acceptance`，不创建（1.2 要求建卡显式 triage，避免任何 ready 绕过） |
| `POST /tasks/bulk` + `status=ready` | 任一 id 缺 accept → 整批 400 `missing_acceptance`，不走上游 |
| `POST /tasks/{id}/reclaim` | 可能恢复 ready/review → 与 PATCH ready 相同的验收检查 |

dispatch 自身的 PATCH 因为卡上已有验收文本，走同一检查后放行。上游
`recompute_ready`（parent 完成后自动提升）和原生 9119 dashboard 不经过本服务，
**不在闸门范围内**，是已知限制。

**状态硬规则**（踩过）：

1. `running` **不能**通过 PATCH 设置（会有 `_RUNNING_DIRECT_MSG`）。
2. 从 `blocked` 恢复必须落 `ready`（走 `unblock_task`）；落 `todo` 是裸写，会丢事件。
3. 有未完成 parent 时 `→ready` 可能返回 **409**，前端要处理成"已转 todo"。
4. `PATCH` 里 `None` = 不修改；要清空用显式 `clear_*` 字段。
5. **worker 收尾必须用 `kanban_request_review`**；`kanban_complete` 会直接进 `done`，绕过整个验收环节。

## 5. 数据契约（metadata）

工作台契约字段存 **`workbench-app/workbench.db`** 的
`task_metadata(task_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, updated_at INTEGER NOT NULL, write_token TEXT)`；
不写共享 `kanban.db`。读取走 `GET /api/workbench/metadata?ids=a,b`（无记录省略该 id，
`write_token` 是内部列，不进入响应）。1.2 提供读端点与派发快照；1.3 已实现写端点：

- `POST /api/workbench/tasks` —— 上游 `POST /tasks`（`triage=true`，可带
  `idempotency_key`）建卡，再写默认 `bucket` + 空 `contract`；metadata 失败返回
  503 + `task_id`，同 key 重试不会重复建卡。
- `PATCH /api/workbench/metadata/{task_id}` —— 先 GET 上游任务（不存在 404），
  再按 key 合并 `bucket` / `contract`；`contract_defaults` 与未知键不可写。
- 写端点只接受 `application/json`（64KB 上限）；带 `Origin` 时必须与 Host 同源，
  否则 403。`accept` ≤2000、`input` ≤4000、`deny_paths` 1–100 个非空字符串。
- 同一 task 的 dispatch（闸门读取→PATCH）与 metadata 写共用一把 per-task 锁，
  避免验收在派发中途被清空；落点 ready 后再读一次验收作为兜底。

| 键 | 存储 | 写方 | 结构 |
|---|---|---|---|
| `bucket` | workbench.db | Story 1.3 建卡入口 | `"today"` \| `"long"` |
| `contract` | workbench.db | Story 1.3 建卡/编辑 | `{input: string, accept: string, ready: bool, budget?: {...}}` |
| `contract_defaults` | workbench.db | 派发瞬间（1.2） | `{max_minutes, max_cost, deny_paths[]}` —— 生效值快照 |
| `solidified` | `task_runs.metadata`（最新 run） | P3 worker（`kanban_request_review`） | `{type: "routing"\|"skill_patch"\|"script", path, status: "pending"\|"verified"}`；workbench 读最新 run |
| `self_report` | `task_runs.metadata`（最新 run） | P3 worker（`kanban_request_review`） | `{delivered, evidence, uncertain}`；workbench 读最新 run |
| `solidified_verified` | workbench.db `task_metadata` | P3 workbench 手动标记 | `{path, verified_at}[]` overlay；与上游 pending 合并展示 |
| `wake_at` | workbench.db `task_metadata` | P3 待办/详情设置 | epoch seconds；只处理带该键的卡，原生 `scheduled` 不受影响 |
| `cost` | 尚未落地（deferred） | 后续成本故事 | `{minutes, tokens, usd, by_rule}` |
| `hops` | 尚未落地（deferred） | 后续建子卡故事 | `int`，> 8 拒绝 |
| `executor` | `routing.json` | P3 worker/routing | `"hermes"\|"claude_code"\|"dsh"\|"codex"` |
| `executor_session_id` | `task_runs.metadata`（最新 run） | P3 executor/worker | 执行器回写的会话 id（dsh 为 `session-<uuid>`，claude 为其 session id） |
| `classification` | 尚未落地（deferred） | 后续分类故事 | `{by: "human"\|"rule"\|"model", model?, label, confidence, cost?}` |

- 上表是目标契约；`solidified` / `self_report` / `executor_session_id` 落在
  **`task_runs.metadata`**（`kanban_request_review` 写入），不是 `tasks.metadata`。
- `bucket` 用 metadata 而不是新状态：**不占用 `todo`**；仓库卡停 `scheduled`，
  `triage` / `todo` 会被上游 triage specifier / `recompute_ready` 接走。
- `contract_defaults` 是**派发瞬间**的生效值：全局默认（`workbench-app/defaults.json`）
  + 卡上 `contract.budget` 覆盖；routing 层当前不实现（deferred）。
- 快照用 SQLite JSON1 `json_set` 按 key 写入、用 `json_remove` 按 key 补偿；
  **非 ready/running 状态派发会重拍 `contract_defaults`**（新 `write_token`），ready
  重派保留。`write_token` 记录本次写入归属，补偿只在 token 仍匹配时删除，避免误删
  他人/后续写入的快照。
- **2.1 验收不新增 metadata**：验收读 `contract` + 上游 `result` / `latest_summary` /
  attachments / events / runs；accept 写上游 `status=done`，reject 先写 comment 再
  `status=triage`；48h 自动收在落点 `done` 后写 `author=workbench` 的记录 comment。

## 6. 扩展点（本期新增代码只允许落在这里）

1. **`workbench-app/server.py` 增加 `/api/workbench/*` 路由**
   - `POST /api/workbench/dispatch/{task_id}` —— 读卡（running → 200 `position:0`；
     done/archived → 409）→ 校验 `contract.accept` 非空 → 合并 `defaults.json` +
     卡上 `contract.budget` → 写 `contract_defaults` 快照（带 `write_token`）→
     `PATCH status=ready` → **再 GET `/tasks/{id}` 读取真实落点** → 只有 ready 才
     读 `/board` 算位次；board 未命中重读一次，仍不命中返回 502。落点不是 ready 时
     409 并补偿本次快照。
   - `GET /api/workbench/metadata?ids=a,b` —— 读 `workbench.db`，无记录省略，
     `write_token` 不进入响应；单请求最多 200 个 id。
   - `POST /api/workbench/tasks` —— 建待办卡：上游 `POST /tasks`（`triage=true`，
     可选 `idempotency_key`）→ GET 校验 → `triage → todo → scheduled` 停车 → 写
     `bucket` + 空 `contract`；任何一步失败回退 triage，返回 502 + `task_id` 供同 key
     重试。`scheduled` 是唯一稳定的仓库状态。
   - `PATCH /api/workbench/metadata/{task_id}` —— 先 GET 上游任务（404 不 upsert），
     再 JSON1 `json_patch` 合并 `bucket` / `contract` / `wake_at`；`contract_defaults`
     不可写。
   - ready 中间件 —— 覆盖 PATCH ready、非 triage 建卡、bulk ready、reclaim；
     与 dispatch 共用验收检查，空验收 400 不转发。
   - `GET /api/workbench/inbox` —— review 队列（2.1 已落地）：纯读；详情并行拉取，
     单张失败进 `failed_ids`；按最早 `review_requested` 排序。P0 blocked 聚合留给 2.2。
   - `GET /api/workbench/inbox/count` —— 顶栏计数（2.1 已落地）：只读一次 `/board`，
     1s 短缓存 + in-flight 合并。
   - `POST /api/workbench/reviews/sweep` —— 48h 自动收扫描（2.1 已落地）：同源 + JSON
     guard；后台 5 分钟 sweeper 调用同一逻辑。`GET /inbox` 本身不写卡。
   - `POST /api/workbench/verify/{task_id}` —— `{action: accept|reject|skip, reason?}`
     （2.1 已落地）：accept → `done`；reject 理由 1–2000 字 → comment + `triage`；
     skip 不改状态；PATCH 后重读，仍 review 返回 502，已离开 review 返回 409。
     P3 在 accept 前读最新 run `metadata.solidified`，缺失返回 400 `missing_solidified`。
   - `POST /api/workbench/solidified/{task_id}/verify` —— P3：`{path}` 写
     workbench.db 的 `solidified_verified` overlay；不 PATCH 上游 run metadata。
   - `GET /api/workbench/cost/{task_id}` —— 读 `metadata.cost`（后续故事，P3）。
   实现方式：与现有 router 平级，**不要改 `plugin_api.py`**；dispatch 路径只经回环公开 REST。

2. **前端**只改 `workbench-ui/src/`，经 `npm run build` 分发到插件与独立版；
   `sync-ui.sh` 已作废。`workbench-ui/src/api/workbench.ts` 负责 dispatch/metadata/inbox/verify 调用与错误码。
   插件宿主没有 `/api/workbench/*`，只渲染看板 tab（就绪列客户端只读）；待办三区与
   全屏验收仅在独立版 8787 提供。

3. **规则与固化物**落 `~/Projects/Jarvis/routing.json`（1.2 不读取，deferred）。
4. **执行器适配器**：`bin/executor-run` + `executors.json`。适配器只做翻译（统一参数 → 各家 CLI，各家输出 → 统一 JSON），不放业务逻辑、不读 `kanban.db`（见 PLAN §6.3）。

## 7. 三条不改的边界

- **上游零改动**：`hermes-agent/` 下任何文件不动。需要的字段若 REST 不返回，从前端二次请求补，不改后端。
- **数据库边界**：`kanban.db` / `state.db` 只允许通过 REST、CLI 或 `sqlite3 -readonly` 查询；
  `workbench-app/workbench.db` 是工作台自有库，可读写但只经 `server.py` 的
  `task_metadata`（不 import 上游模块、不在 `kanban.db` 建任何 workbench 表/列）。
- **闸门不可绕过（本服务范围内）**：dispatch、PATCH ready、非 triage 建卡、bulk ready、
  reclaim 走同一验收检查；上游 `recompute_ready` 与原生 9119 dashboard 不经过本服务，
  属范围外（已知限制，不额外加 watcher）。
- **看板不发明状态模型**：看板 tab 由自有前端渲染（不 iframe、不嵌 dashboard），但列名 / 列序 / `status` 必须与原生 8 列逐项对齐，列定义只允许一处常量（见 PLAN §5.3）。

## 8. 本地开发与验证

```bash
# 起服务（改动后重启）
cd ~/Projects/Jarvis/workbench-app
systemctl --user restart jarvis-workbench
curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz          # {"ok":true}

# 另开一个端口做实验，不打断正在用的服务
WORKBENCH_PORT=8788 ./run.sh &

# 造一张测试卡（无 triage 会被 ready 闸门拒绝；真实写库，用完记得删）
curl -s -X POST http://127.0.0.1:8787/api/plugins/kanban/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"[测试] 契约闸门","body":"验证用","triage":true}'

# 读单卡全量
curl -s http://127.0.0.1:8787/api/plugins/kanban/tasks/<id>

# 派发（契约在 workbench.db，body 为空）
curl -s -X POST http://127.0.0.1:8787/api/workbench/dispatch/<id> \
  -H 'content-type: application/json' -d '{}'

# 读 workbench metadata
curl -s 'http://127.0.0.1:8787/api/workbench/metadata?ids=<id>'

# 建待办卡（可带 idempotency_key；metadata 失败时用同 key 重试）
curl -s -X POST http://127.0.0.1:8787/api/workbench/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"一句话","bucket":"today","idempotency_key":"<uuid>"}'

# 可重复测试（临时 HERMES_KANBAN_HOME / WORKBENCH_DB，不碰真实库）
python3 workbench-app/tests/test_dispatch.py -v
python3 workbench-app/tests/test_todo.py -v
python3 workbench-app/tests/test_verify.py -v
cd workbench-ui && npm run verify   # build + headless 三区探针 + 验收探针
```

运行 Python：`~/.hermes/tools/python-3.14*/bin/python3`（`run.sh` 自动找），依赖由 `hermes_bootstrap` 注入，**不要**自建 venv。

## 9. 已知限制（别当成 bug 去修）

- 固定单一 board，不做多 board 切换；workbench metadata 只按 `task_id` 键（多 board deferred）。
- 5 秒轮询，不是 WebSocket。
- 「今日/长期」归属以 `metadata.bucket`（workbench.db）为准；今日/长期固定
  `created_at` 倒序，不做栏内拖动或排序持久化。当前 React UI 不写 `localStorage`；
  明暗主题由宿主 `data-theme` / `theme.css` token 驱动。
- 上游 `recompute_ready`（parent 完成自动提升）和原生 9119 dashboard 不经过本服务的 ready
  闸门；不额外加 watcher，属已知限制。
- 1.2/1.3 不实现 provenance / `contract.ready` 语义；契约只有 `input` / `accept` / `budget`
  被使用（1.3 只写入空 `ready` 占位）。
- workbench 对上游 REST 的自调用仍走独立 `urllib` 请求（dispatch 在 FastAPI 线程池里执行）；
  当前仍不改成进程内调用，也不改共享连接池。
- 已跟踪的 `workbench-app/web/*.bak-*` 与旧 `web/app.js` / `sync-ui.sh` 清理是 deferred；
  本次不删除任何已跟踪文件。
- 无登录，只监听回环；远程走 Tailscale。
- 插件形态没有后端（没有 `/api/workbench/*`），所以插件宿主只显示看板 tab；
  待办三区与全屏验收只在独立版 8787 提供（界面会提示）。插件宿主就绪列只读是客户端
  约束，绕过 UI 直接调用 9119 dashboard API 仍不在 ready 闸门范围。
- `/inbox` 的单卡详情失败会跳过并记入 `failed_ids`，顶栏 `/inbox/count` 仍按 `/board`
  计数，两者可能短暂不一致；`/board` 的 review id 有 1s 缓存
  （`WORKBENCH_INBOX_CACHE_SECONDS`），验收动作后顶栏最多延迟 1s 更新。
- 48h 自动收的记录 comment 是 best-effort：PATCH 落 `done` 后 comment 失败只写 stderr，
  不阻塞收卡，也不会单独退避重试（5 分钟退避只针对 PATCH/重读失败）。
- 48h 判定取最近一次 `review_requested` 事件；没有该事件的卡永不自动收。
  后台 sweeper 每 5 分钟对全部 review 卡逐张拉详情（O(N)），单机单用户规模下可接受。

## 10. 版本基线

| 组件 | 版本/位置 |
|---|---|
| Hermes | v0.22.7（`~/.hermes/hermes-agent`） |
| magpie | `127.0.0.1:3425`，组 `group/group`（杂活）、`group/main`（长编码） |
| dsh | 0.2.1-rc.5，`dsh --profile headless --json` |
| Python | 3.14（Hermes 运行时） |
| OS | Ubuntu 26.04.1 LTS，GNOME 50.1 Wayland |