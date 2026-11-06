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
        ├──▶ ~/.hermes/state.db           worker sessions (read-only, 2.3)
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
`bucket`、`contract`、派发瞬间的 `contract_defaults` 快照，以及定时唤醒的
`wake_at` / `wake_error` / `wake_next_try_at`。`write_token` 是快照
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

# 建卡：走上游 POST /tasks（triage=true），默认 bucket=today，不自动派发
# idempotency_key 可选（≤128）；同一 key 重试返回同一张卡，metadata 失败重试不重复建卡
curl -s -X POST http://127.0.0.1:8787/api/workbench/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"一句话","bucket":"today","idempotency_key":"<uuid>"}'
# 200 {"ok":true,"task":{...},"metadata":{"bucket":"today","contract":{"input":"","accept":"","ready":false}}}
# 400 invalid_title / invalid_bucket / invalid_request；413 payload_too_large
# 502 upstream_unavailable（非 triage 落点会带 status）；503 metadata_unavailable + task_id

# 写 metadata：bucket 只允许 today|long；contract 按 key 合并
# 任务必须已存在（上游 404 → 404 not_found），不会为任意 id 建孤儿 metadata
curl -s -X PATCH http://127.0.0.1:8787/api/workbench/metadata/<task_id> \
  -H 'content-type: application/json' -d '{"contract":{"accept":"跑通测试"}}'
# 200 {"ok":true,"metadata":{...}}；404 not_found
# 400 invalid_metadata / invalid_bucket / invalid_contract / invalid_request
```

规则：

- `metadata.contract.accept` 为空 → 400，卡状态不变、不入队。中间件同时拦住
  `PATCH status=ready`、无 `triage` 的建卡（1.2 统一要求建卡显式 triage，宁可多拦
  也不留 ready 绕过）、`bulk status=ready`、`bulk assignee+reclaim_first`、
  `reclaim`、`reassign` + `reclaim_first`，以及 `POST /runs/{run_id}/terminate`
  （先经上游 `GET /runs/{run_id}` 解析 task 再拦）。任一 id 缺验收时整批拒绝。
  `/runs/{run_id}/terminate` 解析不到 run→task 映射时**失败关闭**：返回
  403 `gate_unavailable`，不转发未过闸门的 terminate。
  PATCH/reclaim 先 GET 卡，404 原样转发，不会误报 400；卡存在才检查验收。
  闸门从读验收前就持有 per-task 锁，并保持到转发的状态变更完成；
  `update_metadata` 无法在窗口内清空验收。
- 预算生效值 = `defaults.json` 全局默认 + 卡上 `metadata.contract.budget`
  （routing 层 deferred）。派发瞬间把 `max_minutes` / `max_cost` /
  `deny_paths` 三键快照进 `metadata.contract_defaults`。
- 有未完成 parent → 409 `blocked_by_parent`，卡落 `todo`，不重复入队。
- `PATCH status=ready` 成功后会用 **GET `/tasks/{id}`** 读真实落点；只有落点
  确实是 `ready` 才读 `/board` 算位次（board 未命中会重读一次，仍不命中返回 502）。
- 快照生命周期：从非 `ready`/非 `running` 状态发起的派发（含 rework 后再次
  派发）会**重拍** `contract_defaults`（新值 + 新 `write_token`）；已 `ready`
  再派发是幂等的，返回 200，按当前 ready 队列重算位次，不覆盖已有快照。
  重拍后若 PATCH 失败，补偿会恢复上一份快照及其 token，不丢历史。
- `GET /api/workbench/metadata` 单请求最多 200 个 id；超过返回
  400 `too_many_ids`，不静默截断。前端按 100 个 id/请求分块，长列表仍完整。
- metadata 写端点接受 `bucket`、`contract` 与 `wake_at`（epoch seconds，
  0..2100；`null` 清除），`bucket` 只允许 `today|long`；用 JSON1
  `json_patch` 按 key 合并，派发快照 `contract_defaults` 不会被客户端覆盖。
  `wake_error` / `wake_next_try_at` 由服务端写，客户端不可写。未知键、空
  patch、非法 contract / wake_at 一律 400；建卡 `title` 与 `accept` 落库前
  都会 trim。
- 校验上限：`accept` ≤2000、`input` ≤4000、`deny_paths` 1–100 个非空字符串、
  请求体 ≤64KB。POST/PATCH 必须 `Content-Type: application/json`（否则 415）；
  带 `Origin` 时必须与 Host 同源（否则 403）。
- 建卡先上游后 metadata，非单事务：metadata 写失败返回 503 + `task_id`；客户端
  用同一 `idempotency_key` 重试即可补齐 metadata，不会建出第二张卡。上游 4xx
  映射为 400（带 `detail`/`upstream_status`），5xx/不可达映射为 502。
- 同一 task 的 dispatch 与 metadata 写共用 per-task 锁，验收不会在派发中途被清空；
  落点 ready 后会再读一次验收作为兜底。
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

## 定时唤醒（3.3）

仓库卡可在 workbench metadata 写顶层 `wake_at`（epoch seconds）；后台
`WakeScheduler` 只扫描带该键的卡。Hermes 原生 `scheduled` 卡没有这个键，
不会被读取或修改，也不使用上游 `not_before`。

```bash
# 设置 / 清除（null 为清除；wake_error / wake_next_try_at 客户端不可写）
curl -s -X PATCH http://127.0.0.1:8787/api/workbench/metadata/<task_id> \
  -H 'content-type: application/json' -d '{"wake_at":1730000000}'
curl -s -X PATCH http://127.0.0.1:8787/api/workbench/metadata/<task_id> \
  -H 'content-type: application/json' -d '{"wake_at":null}'

# 手动提前唤醒：与后台到点共用同一 wake_task 逻辑，不受退避窗口限制
curl -s -X POST http://127.0.0.1:8787/api/workbench/wake/<task_id> \
  -H 'content-type: application/json' -d '{}'
# 200 {"ok":true,"position":2,"eta":"14:19","status":"ready"}   # 无 parent
# 200 {"ok":true,"status":"todo"}                                # 有未完成 parent
# 400 missing_acceptance / 404 not_found / 409 not_in_store
# 502 upstream_unavailable / 503 metadata_unavailable
```

规则：

- 到期且有验收、无未完成 parent：复用 dispatch 进 `ready`，写
  `contract_defaults`；有未完成 parent：PATCH `todo`。成功后清空
  `wake_at` / `wake_error` / `wake_next_try_at`。
- 缺验收：留在 `scheduled`，写 `wake_error=missing_acceptance`、
  `wake_next_try_at=now+300`，保留 `wake_at`，不自动派发；补验收后自动重试，
  也可手动提前唤醒。
- 上游 502：保留 `wake_at`，写 `wake_error=upstream_unavailable`、
  `wake_next_try_at=now+60`，下一轮重试。重复扫描幂等；卡已离开仓库时清掉
  残留 wake 字段。
- `wake_at` 只接受非负整数（bool / 浮点 / 字符串 / 负数一律 400
  `invalid_metadata`）；设置新时间会清掉上一轮的 `wake_error` /
  `wake_next_try_at`。

## 验收 inbox / verify / sweep

验收队列只含上游 `review` 卡。所有端点只读写上游公开 REST，不碰 `kanban.db`。

```bash
# 右上角计数：只读 /board，不拉每张卡详情（短缓存 + in-flight 合并）
curl -s http://127.0.0.1:8787/api/workbench/inbox/count
# 200 {"ok":true,"count":3,"review_ttl_seconds":172800}

# 完整验收队列：pure read，不自动收；详情并行读取，单张失败只跳过
curl -s http://127.0.0.1:8787/api/workbench/inbox
# 200 {"ok":true,"count":3,"review_ttl_seconds":172800,
#      "tasks":[{"id":"t_…","title":"…","status":"review",
#                "metadata":{"contract":{"accept":"…","input":"…"}},
#                "result":null,"latest_summary":"…",
#                "self_report":{"delivered":"…","evidence":"…","uncertain":""},
#                "solidified":[{"type":"script","path":"scripts/x.sh",
#                               "status":"pending"}],
#                "review_requested_at":1730000000,"review_age_seconds":120,
#                "auto_collect_due":false,"auto_collect_error":null}, …],
#      # self_report / solidified 都只取最新 run；缺失/畸形降级（自述 null、
#      # 固化 []），不 500；固化 [] 会让 accept 被 400 阻断
#      "failed_ids":[]}
# 502 upstream_unavailable / 503 metadata_unavailable

# 显式触发 48h 自动收（POST 受同源 + JSON Content-Type 守卫）
curl -s -X POST http://127.0.0.1:8787/api/workbench/reviews/sweep \
  -H 'content-type: application/json' -d '{}'
# 200 {"ok":true,"collected":1,"failed":0,"skipped":2,
#      "collected_ids":["t_…"],"failed_ids":[]}
# 502 upstream_unavailable（/board 不可读）

# 验收动作：accept | reject | skip
curl -s -X POST http://127.0.0.1:8787/api/workbench/verify/<task_id> \
  -H 'content-type: application/json' -d '{"action":"accept"}'
# 200 {"ok":true,"action":"accept","status":"done","comment_written":false}
# accept 额外要求最新 run 至少一条合法 solidified，否则：
#   400 {"ok":false,"error":"missing_solidified","comment_written":false}
# reject 必填 reason（trim 后 1–2000 字）：
#   {"action":"reject","reason":"没跑测试"}
#   200 {"ok":true,"action":"reject","status":"triage","comment_written":true}
#   400 reason_required / reason_too_long / invalid_action
#   404 not_found（带 comment_written）
#   409 not_in_review（重读后卡已离开 review）
#   502 upstream_unavailable（重读后卡仍在 review；带 comment_written）

# 手动「标记已验证」：只写 workbench.db overlay，不 PATCH 上游 run metadata
curl -s -X POST http://127.0.0.1:8787/api/workbench/solidified/<task_id>/verify \
  -H 'content-type: application/json' -d '{"path":"scripts/x.sh"}'
# 200 {"ok":true,"path":"scripts/x.sh",
#      "solidified_verified":[{"path":"scripts/x.sh","verified_at":1730000000}]}
# 400 invalid_request（path 空）/ missing_solidified（path 不在最新 run）
# 404 not_found / 502 upstream_unavailable / 503 metadata_unavailable
```

规则：

- `accept` = `PATCH status=done` + summary「验收通过」；`reject` = 先写理由
  comment、再 `PATCH status=triage`（comment 失败绝不改状态）；`skip` 不改上游，
  仅在本批前进。所有动作都在 verify 端点内完成，前端不直接 PATCH。
- reject 在同一 review 轮次内按「author=workbench + 同理由 + comment 时间不早于
  最近 `review_requested`」去重；重试不会重复写 comment。
- PATCH 非 200 或 200 但落点不对时会重读卡：仍在 `review` → 502
  `upstream_unavailable`（卡留在队列）；已离开 review → 409 `not_in_review`。
  每个响应都带 `comment_written`。
- 48h 自动收：时长取**最近一次** `review_requested` 事件（新事件重置时钟）；
  没有该事件的卡永不自动收。后台 sweeper 默认每 5 分钟跑一次，前端进入验收时
  还会显式调用一次 sweep 端点；轮询/读取本身不写卡。
- 自动收顺序：先 `PATCH status=done`（尽量带原 worker summary，避免覆盖
  `latest_summary`；上游要求 summary 时回退到带审计文案的 summary），落点确认
  `done` 后才写 `48h 自动收（review:<event id>）` 记录 comment。标记按 review
  轮次隔离，二次 review 超时会写新的记录，不会误判为已收。
- 自动收失败（PATCH/重读失败）会记录 `auto_collect_error` 并进入退避窗口；
  窗口内 sweeper 不再重试，inbox item 暴露 `auto_collect_due` 与
  `auto_collect_error:{message,attempts,retry_in_seconds}`。
- 队列顺序：最早进入 review 的在前；无 `review_requested` 的卡排在最后。
- **worker 自述契约**：worker 调 `kanban_request_review` 时在
  `metadata.self_report` 写 `{"delivered": "...", "evidence": "...", "uncertain": "..."}`
  三个字符串（交付 / 依据 / 没底）。inbox 只读**最新 run** 的该字段，不跨轮拼接：
  `delivered` / `evidence` 去空白后为空按缺失处理；`uncertain` 为空字符串是合法值
  （验收页显示「未填写」）；缺失或畸形时为 `null`（验收页显示「自述缺失」）。
  两种降级都只警示，不阻断 A/R/S。本仓库不改上游 Hermes 工具 schema；完整约定见
  `~/.hermes/skills/workbench-conventions/SKILL.md`。
- **固化登记契约**：worker 在 `kanban_request_review(metadata={...})` 里写
  `solidified`（单条对象或数组），每条形如
  `{"type":"routing|skill_patch|script","path":"...","status":"pending|verified"}`。
  工作台只读**最新 run** 的该字段；`path` 非空、`type`/`status` 在枚举内才算合法，
  非法项忽略，全部非法按缺失处理（返回 `[]`，不 500）。`path` 只作不透明标识
  （工作台不读取/不编辑对应文件），单条 ≤500 字符；最多读取前 50 条，按
  `type:path` 去重。accept 要求至少一条合法
  记录，否则 400 `missing_solidified` 且卡留在 review；reject/skip 与 48h 自动收
  不受影响。手动「标记已验证」按 `path` 幂等写 workbench.db 的
  `task_metadata.solidified_verified`（`{path, verified_at}[]`），不 PATCH 上游
  run metadata——上游 `pending` 保留为原始登记，overlay 只负责验收页显示。
- 环境变量覆盖（均为有限正数，非法值回落默认）：
  - `WORKBENCH_REVIEW_TTL_SECONDS` 自动收窗口，默认 `172800`（48h）
  - `WORKBENCH_REVIEW_SWEEP_SECONDS` 后台扫描间隔，默认 `300`；`0`/非法值关闭
  - `WORKBENCH_WAKE_SWEEP_SECONDS` 定时唤醒扫描间隔，默认 `30`；`0` 关闭，非法值告警后关闭
  - `WORKBENCH_REVIEW_BACKOFF_SECONDS` 失败退避，默认 `300`
  - `WORKBENCH_INBOX_CACHE_SECONDS` count/inbox 的 /board 短缓存，默认 `1`；
    `0` 关闭缓存（仍保留 in-flight 合并），测试用

## 会话追溯（2.3）

验收页的「打开执行会话」和看板已完成卡的「会话」都会打开应用内只读抽屉；
不跳 9119、不发消息、不写 `state.db`。

```bash
curl -s http://127.0.0.1:8787/api/workbench/session/<task_id>
# 200 {"ok":true,"task_id":"t_…","session":{"id":"…","source":"kanban",
#      "title":"…","model":"…","started_at":…,"ended_at":…,"message_count":N,
#      "input_tokens":…,"output_tokens":…,"profile_name":"…"},
#      "source":"run","run_id":48,"executor_session_id":null,
#      "messages":[{"id":123,"role":"user","content":"…","content_chars":42,
#                   "content_truncated":false,"compressed":false,
#                   "tool_calls":[],"tool_name":null,"timestamp":…}, …],
#      "returned_count":16,"total_count":16,"message_limit":200,
#      "truncated":false}
# 404 no_session / 404 session_missing / 404 not_found / 502 upstream_unavailable
# 503 session_unavailable（state.db 缺失、锁定、损坏或读取超时）
```

规则：

- 会话解析按「最新一轮」优先：run 列表按 `started_at`（同值按 run id）倒序，
  第一个带 `metadata.worker_session_id` 的 run 优先；该会话在 `state.db` 中
  不存在时继续尝试更早的 run，最后回退 `tasks.session_id`（标注为「发起会话」）。
  `executor_session_id` 只从**实际解析出会话的那个 run** 返回（Epic 3 才续跑）。
- 只读打开 `~/.hermes/state.db`（`mode=ro` + `PRAGMA query_only` + busy timeout），
  按 `messages.id`（不是 timestamp，时钟会回退）倒序取窗口，再正序返回。
  若表里有对应列则遵守 Hermes 的显示规则：`(active = 1 OR compacted = 1)`、
  跳过 `display_kind='hidden'`、按 `display_identity` 去重保留最新一行、
  `_compressed_summary` 行按 Hermes 的显示投影裁剪（只保留 prior tail / live ask）。
- 不返回 `sessions.system_prompt`；role=`system` 以及内容以 `[System:` 或
  `[CONTEXT COMPACTION — REFERENCE ONLY]` 开头的内部 user 行会被跳过。
- 每条消息内容上限 ~20KB（带截断标记与 `content_chars` / `content_truncated`），
  整个响应内容上限 ~1MB；`returned_count` / `total_count` 与
  `sessions.message_count` 解耦（记录数不一致时以实际可显示行为准）。UI 显示
  「显示 X / 共 Y 条消息」与 truncated 提示。
- 工具调用只返回名称与「参数已折叠」；参数（可能含命令、路径、token）不离开
  服务端。响应带 `Cache-Control: no-store`。
- 环境变量覆盖：
  - `WORKBENCH_STATE_DB` Hermes state DB 路径，默认 `~/.hermes/state.db`
  - `WORKBENCH_SESSION_MESSAGE_LIMIT` 单次消息窗口，默认 `200`（1–1000）
- **敏感性 / 远程绑定**：会话接口返回真实对话正文。服务默认只监听
  `127.0.0.1` 且没有登录鉴权；不要把它绑定到 `0.0.0.0` 或暴露到不可信网络。
  需要远程访问时先走 Tailscale 等私有网络，并自行加访问控制。
- 已知延后：compaction 的 `parent_session_id` 祖先会话合并、`display_kind`
  的更多变体（当前只处理 hidden + display_identity 去重）、以及多轮 run 选择器，
  均不在本故事内；需要时另开故事。

## 通知中心 / P0 通知与动作

通知引擎（`notifications.py`）只读 `/board` + `GET /tasks/{id}` 分类，不写
`kanban.db`、不新增状态迁移；状态全部在内存里，重启后重新采集。

分类规则：

- `blocked` 且有未完成子卡，或 `block_kind ∈ {needs_input, capability}` → **P0**
- 其余 `blocked`（`dependency` / `transient` / 未分类）→ **P1**
- `review` 且有未完成子卡 → **P0**；其余 `review` → **P2**
- 其它状态（`ready` / `running` / `done` 等）不通知

事件 id 稳定并绑定一轮处理：
`n:{task_id}:review:{review_requested_at}` /
`n:{task_id}:blocked:{block_kind}:{block_recurrences}`。事件离开候选集即
resolved；同一 id 再次出现视为新事件，通知/升级/snooze 全部重置。

推送：

- **P0**：5s 轮询内发桌面 `notify-send`（`--urgency=critical`）并在右上角置顶；
  30 分钟未处理再提醒一次，之后不再重复。
- **P1**：攒批；待推达到 3 条时合并为一条（`--urgency=normal`，内容截断）。
  最早一条等满 30 分钟仍未处理 → 升级 P0 一次（`--urgency=critical`；
  同一轮到期的事件合并成一条升级），之后不再重复。
- **P2**：每日 08:00 一条汇总（`--urgency=low`），当天最多一次。
- review 卡统一显示「还有 Xh Ym 自动收」倒计时；验收槽固定 3 束，同一
  `parent` 集合的兄弟卡（同一次 decompose）成一束，超出的束排队。
- reminder/升级只对 P0/P1；P2 只支持 accept/reject。

```bash
# 快照（pure read，内存态；UI 每 5s 轮询）
curl -s http://127.0.0.1:8787/api/workbench/notifications
# 200 {"ok":true,"generated_at":…,"counts":{"p0":1,"p1":0,"p2":2,"total":3},
#      "p0":[{…}],"p1":[],"p2":[…],
#      "slots":{"capacity":3,"active":[…],"queued":[…],"queued_count":1,
#               "queued_cards":1},
#      "daily":{"date":"2026-11-05","sent":true,"count":2,"hour":8,
#               "line":"P2 待验收 2 张：…"},
#      "sweep":{"last_at":1762345678,"last_error":null},
#      "desktop":{"available":true,"last_error":null,"last_kind":"p0",…},
#      "channels":{"desktop":"notify-send","mobile":"reserved"}}

# 采集一轮（guarded POST；服务端后台每 5s 也跑同一轮）
curl -s -X POST http://127.0.0.1:8787/api/workbench/notifications/sweep \
  -H 'content-type: application/json' -d '{}'
# 200 快照；502 upstream_unavailable（保留上一份快照，不重复通知）

# 动作：accept | reject（reason 必填）| remind（10 分钟后）
curl -s -X POST \
  'http://127.0.0.1:8787/api/workbench/notifications/<event_id>/action' \
  -H 'content-type: application/json' -d '{"action":"remind"}'
# 200 {"ok":true,"id":"n:…","action":"remind","snooze_until":…}
# 400 invalid_action / reason_required / reason_too_long
# 404 not_found
# 409 stale_review（accept/reject 可带 expected_review_at；与最新
#     review_requested 不一致时拒绝，避免旧通知操作新一轮 review）
# accept/reject 直接调用 2.1 verify：
#   200 {"ok":true,"action":"accept","status":"done","comment_written":false}
#   400 missing_solidified（accept 时最新 run 无合法固化；卡留在 review，
#       通知也保留，用户可改走不收/打回）
#   verify 失败按 2.1 状态码回传，通知保留以便重试
```

规则：

- `POST /notifications/sweep` 与 accept/reject 都受 JSON Content-Type +
  同源 Origin 守卫；`GET /notifications` 是纯读，不访问上游。快照里的
  `sweep.last_at/last_error` 记录最近一次采集结果，采集失败时保留上一份
  数据并把错误显示在通知中心。
- 通知命令缺失、解析失败或退出码非 0 只写进快照的 `desktop.last_error`，
  不影响快照、动作或其它端点；失败的推送不会标记 `notified_at`/升级/
  日报已发，60 秒退避后重试（桌面通道不可用时服务仍可启动）。命令可整体
  由 `WORKBENCH_NOTIFY_COMMAND` 覆盖（测试用 logger 脚本），参数追加
  `--app-name=Jarvis 工作台 --urgency=<critical|normal|low> <summary> <body>`。
- 手机通道只在根 payload 预留 `channels.mobile`（固定 `reserved`），不做
  真实推送；`channels.desktop` 是实际通知命令。
- 验收槽只约束通知中心的展示/推送，不改变 2.1 全屏验收队列的处理能力。
- 引擎是内存态：重启后当前 P0 会重新推送一次，P1 重新计时攒批，P2 当天
  可能再次发送日报（没有持久化去重）。
- 只在独立版 8787 提供；插件宿主不渲染通知中心、也不轮询该端点。
- 环境变量：时间阈值均为有限正数，非法值回落默认；例外是
  `WORKBENCH_NOTIFY_SWEEP_SECONDS` 的 `0`/非法值表示关闭后台采集。
  - `WORKBENCH_NOTIFY_SWEEP_SECONDS` 后台采集间隔，默认 `5`；`0`/非法值关闭
  - `WORKBENCH_NOTIFY_BATCH_SIZE` P1 攒批条数，默认 `3`
  - `WORKBENCH_NOTIFY_BATCH_SECONDS` P1 最早等待，默认 `1800`
  - `WORKBENCH_NOTIFY_ESCALATE_SECONDS` P0/P1 未处理升级，默认 `1800`
  - `WORKBENCH_NOTIFY_SNOOZE_SECONDS` remind 延后，默认 `600`
  - `WORKBENCH_NOTIFY_DAILY_HOUR` P2 汇总小时，默认 `8`
  - `WORKBENCH_NOTIFY_COMMAND` 覆盖通知命令

## 界面

- **待办**（独立版默认 tab）：三列「流水线（302px 凹槽）/ 今日 / 长期」。
  今日/长期只显示 `triage` 且 `metadata.bucket` 匹配的卡，仓库卡只有标题 +
  验收一行；验收在卡上就地编辑（失败保留输入）。拖进流水线调用 1.2 的
  `dispatchTask`；验收为空时回原位、抖动 300ms、红字提示且不发请求。
  顶部回执条在 drop 时显示「正在派发…」，响应后显示「已入队 · 第 N 位 ·
  预计 HH:MM」（running 卡显示「已在运行」）；只保留最近一次派发尝试
  （latest-attempt；新尝试会替换旧回执），6 秒后自动消失，可用关闭按钮提前
  隐藏（本次尝试迟到的结果不再弹出），不显示卡片标题/ID。
  回执位次/ETA 是 dispatch 响应的快照；流水线 `ready` 卡上的「第 N 位」是每次
  5s 轮询按 `/board` 顺序重算的实时值，刷新后只保留行内位次、不显示 ETA。
  ETA 是队列槽位近似：位次 1 = 7 分钟，不扣除卡已运行时间。底部输入框回车
  建卡，今日/长期可切换（默认今日），不自动派发。
- **验收**（独立版全屏覆盖层，不是 tab）：右上角「验收 N」与流水线「待收」
  段/行进入。一张一屏显示验收标准、输入、运行摘要、worker 三行自述
  （交付/依据/没底）、`result`、附件（文本/图片内联）与本轮运行信息
  （profile/状态/耗时/运行摘要/错误）；顶栏
  `1/N`、进度与后端 `review_ttl_seconds` 倒计时。`A` 收、`R` 不收（理由必填，
  Ctrl/Cmd+Enter 提交）、`S` 跳过、`Esc` 退出；清空后展示本批统计并约 3 秒
  自动返回。队列 5s 轮询，跳过卡不会被重新加回；新 review 卡会自动追加。
  `initialTaskId` 不在队列时给出提示而不是静默换卡；2.3 会话入口仅占位。
- **通知中心**（独立版右上角固定层，不是 tab）：P0 置顶常驻；P1 攒批面板；
  P2 每日一行 + 当天 review 卡列表；review 卡显示自动收倒计时；验收槽摘要
  展示当前 3 束（兄弟卡成束）与排队束/张数。review 卡可直接「收」「不收」
  （理由必填）或「10 分钟后再提醒」；非 review 卡只有提醒与「打开看板」。
  首次进入显式采集一轮，之后每 5s 拉 `GET /notifications`（不轮询写接口）。
  桌面通知失败时在面板顶部显示 `desktop.last_error`，不影响卡片操作。
  插件宿主不挂载该组件，也不发通知请求。
- **看板**：8 列 `待整理 / 待执行 / 已排期 / 就绪 / 运行中 / 待拍板 / 待验收 / 已完成`。
  独立版把卡拖到「就绪」会走 `dispatchTask`（写快照、算位次、过验收闸门），
  其他列仍是原生 PATCH；闸门拒绝显示中文提示（空验收 / parent / 不可派发）。
- 派发请求客户端最多等 5 秒；超时 abort 并清空 pending 回执与 in-flight 串行锁，
  显示「派发超时，请刷新后重试」。
- 明暗主题由宿主 `data-theme` / `theme.css` token 驱动；当前 React UI 不写
  `localStorage`。
- 5 秒轮询 `/api/plugins/kanban/board`，不是 WebSocket
- **插件宿主**没有 `/api/workbench/*`，只显示看板 tab；「就绪」列客户端只读
  （不接受投放、不发 dispatch/PATCH），并显示「插件宿主只读；请在
  127.0.0.1:8787 独立版派发」。其他状态列仍可拖拽。宿主检测统一为
  `sdk && sdk.fetchJSON`；有 SDK 但没有 `fetchJSON` 时直接抛出明确错误，
  不会降级渲染错 UI。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI：挂 kanban router + `/api/workbench/*` + PATCH-ready 闸门 + 静态页 + `/healthz` |
| `notifications.py` | 通知引擎：分类 / 事件 id / P1 攒批 / 升级 / snooze / P2 日报 / 验收槽 / 桌面通知 + 后台 poller |
| `defaults.json` | 预算与队列估算的唯一全局默认源 |
| `workbench.db` | 运行期生成的工作台 metadata 库（已 gitignore） |
| `tests/test_dispatch.py` | 派发/闸门/metadata 的可重复集成测试（临时库，不碰真实数据） |
| `tests/test_todo.py` | 待办建卡 / metadata 写端点的可重复集成测试 |
| `tests/test_wake.py` | 定时唤醒：到期 ready / parent todo / 缺验收退避 / 原生 scheduled / 手动提前 / 非法值 / 后台调度器 / 上游 502 |
| `tests/test_verify.py` | inbox / count / sweep / verify / solidified 归一化与 accept 门禁 / overlay / 48h 自动收与退避的可重复集成测试 |
| `tests/test_notifications.py` | 通知分类/攒批/升级/日报/槽/动作/snooze/命令失败/后台 10s 的测试 |
| `tests/test_session.py` | 会话解析/只读 state.db/窗口与过滤/截断/脱敏/no-store 的集成测试 |
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
python3 workbench-app/tests/test_todo.py -v
python3 workbench-app/tests/test_wake.py -v
python3 workbench-app/tests/test_verify.py -v
python3 workbench-app/tests/test_notifications.py -v
python3 workbench-app/tests/test_session.py -v
cd workbench-ui && npm run probe:todo    # headless 三列/拖拽/建卡/验收/回执/位次探针
cd workbench-ui && npm run probe:verify  # headless 全屏验收探针
cd workbench-ui && npm run probe:notify  # headless 通知中心探针
cd workbench-ui && npm run probe:session # headless 会话抽屉/入口/只读/重试探针
```

后端测试会为每个套件创建临时 `HERMES_KANBAN_HOME` 与 `WORKBENCH_DB`，并用
`~/.hermes/tools/python-3.14*/bin/python3`（或 `WORKBENCH_PYTHON` 指定）启动
隔离服务。覆盖：验收闸门（PATCH/create/bulk ready/bulk reclaim/reclaim/
reassign+reclaim_first/run terminate）、run→task 映射失败关闭、404 透传、
parent 停卡、review/todo 落点、落地 GET、`/board` 失败与重读、幂等重派、
rework 重拍快照与失败恢复上一快照、快照 `write_token` 归属、闸门锁竞态、
done/archived、running、非法 defaults 与卡覆盖、metadata 201-id 返回
`too_many_ids`、上游挂起超时、PATCH 404、metadata 库故障、并发派发与共享
`kanban.db` 无 workbench 对象；待办写端点覆盖建卡默认值/校验、bucket/contract
合并、幂等重试保留用户契约、`contract_defaults` 不被覆盖、验收落库与派发联动。

`npm run probe:todo` 会自起隔离服务与 headless Chrome（无新增 npm 依赖），
断言三列顺序/计数、仓库卡静态内容、空验收抖动与零请求、验收就地编辑、
有验收派发、pending→最终回执与 dispatch 响应一致、全局串行派发、回执存活期
409/空验收拒绝清回执、关闭按钮与 6s 自动消失、ready 卡 1-based 位次、外部
队列变化后轮询重算、刷新后位次重派生、running 回执/徽标（暂停轮询避免竞态）、
新建卡落今日/长期、外部轮询、created_at 排序、409 parent 提示、明暗 token
与回执/徽标对比度、窄视口与双 tab 状态保持；并覆盖看板拖到就绪走 dispatch
（快照/位次/中文闸门提示）、dispatch 5s 超时清理 pending 与串行锁、插件宿主
SDK stub（只读就绪列、其他列仍可 PATCH、缺少 `fetchJSON` 的宿主报错）。
`npm run probe:verify` 覆盖全屏验收：单条/数组固化展示、手动「标记已验证」
overlay（幂等、上游保持 pending）、缺固化时 accept 阻断提示、上游
`verified` 记录直接收，以及原有 A/R/S、附件、自述、轮询、外部状态变化与
插件宿主无入口。accept 门禁是共用路径：`test_notifications.py`、
`npm run probe:notify`、`npm run probe:session` 的 review 夹具同样带合法
solidified，`probe:notify` 另覆盖缺固化时通知中心收被阻断。
`npm run probe:notify` 覆盖通知中心：P0 置顶、P1 面板、P2 日报、验收槽/
排队、倒计时数值、remind/不收/收、桌面通知失败提示、挂载后新卡 5s 轮询，
以及插件宿主无通知中心/无轮询。
`npm run probe:session` 覆盖会话抽屉：看板完成卡与验收页入口、task.session_id
「发起会话」回退、按 id 的窗口/过滤/去重、显示条数与 truncated、工具参数脱敏、
滚动到最新、body 锁滚动 + 背景 inert、Esc/焦点、state.db 503 与
session_missing 重试、抽屉打开时 A/R/S 不影响背后验收卡，以及插件宿主无入口。
`npm run verify` = `build + probe:todo + probe:verify + probe:notify + probe:session`。

探针前置条件：Node ≥22（内置 `WebSocket`/`fetch`）、本机 Chrome/Chromium
（自动找 `google-chrome` / Playwright 缓存，找不到时用 `CHROME_BIN` 指定）、
Python 运行时（同后端测试，可用 `WORKBENCH_PYTHON` 覆盖）。

## 已知限制

- 固定当前 board，不切换多 board；metadata 只按 `task_id` 键（多 board 支持 deferred）
- 待办不做长期→今日自动倒流、栏内拖动排序与排序持久化
- 不实现 provenance / `contract.ready` 语义；只使用 `contract.input/accept/budget`
- 建卡与 metadata 写入跨上游 REST + 本地库，非单事务；metadata 失败时返回 503，
  客户端需用同一 `idempotency_key` 重试
- 插件宿主只能看看板；待办依赖 `/api/workbench/*`，仅独立版 8787 提供
- 非工作台建的 `triage` 卡没有 `bucket`，不会出现在今日/长期（需先写 metadata）
- `contract.budget` 只能合并/覆盖键，暂不支持清空单个 budget 键
- 待办/看板的轮询与拖拽逻辑目前各自实现，尚未抽共享 hook
- 上游 `recompute_ready` 与原生 9119 dashboard 不经过 ready 闸门；不额外加 watcher
- 插件宿主「就绪列只读」是客户端拦截；绕过 UI 直接调用 9119 dashboard 的 kanban
  API 仍不受本服务闸门约束（该宿主没有 `/api/workbench/*`）
- 自调用仍是独立 `urllib` 请求（dispatch 在线程池执行），不共享连接池
- 已跟踪的 `web/*.bak-*` 与旧 `web/app.js` / `sync-ui.sh` 清理 deferred；本次不删已跟踪文件
- 没有任务详情抽屉（看详情仍可用 dashboard 的 Kanban 页）
- 5 秒轮询，不是 WebSocket
- 通知引擎是内存态：重启后重新采集，不持久化通知历史；手机通道只在 payload
  预留 `channels.mobile`，不做真实推送
- 「今日/长期」归属以 `metadata.bucket` 为准；列表固定 `created_at` 倒序，不做栏内拖动或排序持久化
- 无登录，仅限本机 / Tailscale

## 卸载

```bash
systemctl --user disable --now jarvis-workbench
rm -f ~/.config/systemd/user/jarvis-workbench.service
rm -f ~/.local/share/applications/jarvis-workbench.desktop
rm -rf ~/Projects/Jarvis/workbench-app
```
