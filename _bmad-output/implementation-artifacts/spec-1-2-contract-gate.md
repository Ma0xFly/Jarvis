---
title: '1.2 契约字段与派发闸门'
type: 'feature'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 1
context: []
baseline_commit: 'e94620e83a17e469131942cf578a7f6fcfd95a26'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 派发缺少不可绕过的闸门：验收为空的卡不能进 ready；派发瞬间必须把预算/禁区的生效值快照到卡上，事后能追溯它按什么规则跑。当前没有 dispatch 端点、没有前端 API 客户端，也没有全局默认文件。

**Approach:** 在 `workbench-app/server.py` 增加 `POST /api/workbench/dispatch/{task_id}`：读卡 → 校验 `metadata.contract.accept` 非空 → 合并预算默认值 → 写 `metadata.contract_defaults` → 状态落 `ready` → 返回队列位次与 ETA。前端新增 `workbench-ui/src/api/workbench.ts` 负责调用、错误映射与派发前校验；待办三区与拖拽接线属于 Story 1.3。

## Boundaries & Constraints

**决策（2026-11-05，人已确认）：**
- 1.2 前端只做 `api/workbench.ts`；待办三区/拖拽 UI 留给 1.3（`sketches/006-final` 只作视觉参考，且与 epics 的三栏/8 列不一致）。
- metadata 存 **workbench 自有库 `workbench-app/workbench.db`**（2026-11-05 人确认：不碰共享 `kanban.db`，上游升级重建表/加同名列都不影响）。`server.py` 只通过上游公开 REST 读写任务，不 import `kanban_db`/`kanban_db_connect`、不依赖私有函数；派发时先写快照再 PATCH，PATCH 失败补偿删除。
- 三层合并只落全局默认 + 卡覆盖（`metadata.contract.budget`）；routing 层不在 1.2 实现。默认 `max_minutes=120`、`max_cost=5`、`deny_paths=[".git","~/.ssh","~/.hermes","node_modules",".env"]`。

**Always:**
- 端点只加在 `workbench-app/server.py` 的 `/api/workbench/*`；不改 `~/.hermes/hermes-agent/` 下任何文件。
- 不直接读写 `kanban.db`、不 import 上游内部模块；所有 kanban 读写经 `/api/plugins/kanban/*` REST。
- 验收为空 → 400 `missing_acceptance`，卡状态不变、不入队。
- 有未完成 parent → 409 `blocked_by_parent`，卡落 `todo`，不重复入队。
- 重复派发幂等：已 ready 的卡再派发返回 200，位次按当前 ready 队列重算，不覆盖已有快照。
- `contract_defaults` 必须含 `max_minutes` / `max_cost` / `deny_paths` 三键，且是派发瞬间的生效值快照。
- 前端不写死预算数值；全局默认只在 `workbench-app/defaults.json`。
- `running` 仍不可手动设置；状态流转复用 Hermes 既有语义。

**Never:**
- 不实现待办三区/拖拽 UI（Story 1.3）。
- 不引入依赖、不改 kanban 状态机、不新增 WebSocket。
- 不把预算数值写进前端或 spec 之外的常量。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 派发成功 | accept 非空、无未完成 parent | 200 `{ok:true,position,eta,status:"ready"}`；`contract_defaults` 三键写入；状态 ready | N/A |
| 验收为空 | accept 缺失/空白 | 400 `{ok:false,error:"missing_acceptance"}`；状态不变 | 前端可先拦 |
| 有未完成 parent | parents 未 done | 409 `{ok:false,error:"blocked_by_parent",status:"todo"}`；卡落 todo | 不重复入队 |
| 重复派发 | 已 ready 且有快照 | 200；位次按当前 ready 重算；不覆盖快照 | N/A |
| 卡不存在 | 非法 id | 404 | N/A |
| routing 层未接 | 是否存在 routing.json 都一样 | 用全局默认 + 卡覆盖，不读取 routing | N/A |
| 读取卡片 metadata | `GET /api/workbench/metadata?ids=a,b` | 返回 `{id: {contract, contract_defaults, bucket…}}`；无记录省略该 id | N/A |

</frozen-after-approval>

## Additional Edge Cases（审查回环 1 补充，非冻结段）

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 直接 PATCH 绕过 | `PATCH /api/plugins/kanban/tasks/{id}` body `status=ready`，卡无 accept | 400 `missing_acceptance`，不转发上游 | 中间件拦截 |
| 直接 PATCH 放行 | 同上，但卡有 accept | 正常转发，上游语义照旧 | N/A |
| 落点不是 ready | blocked/scheduled/review 派发后被上游落到 `review`/`todo` | 409 `not_ready`（或 `blocked_by_parent` 落 todo），`status` 返回实际落点；本次快照补偿删除 | 不谎报 200 |
| `/board` 读失败 | PATCH 已成功但 board 超时/非 200 | 502 `upstream_unavailable`，不返回 position 0 | 不谎报成功 |
| 409 停卡 | ready 被拒后 PATCH todo 失败/落点异常 | 报告实际落点或 502；不硬写 `status:"todo"` | 不谎报 |
| 完成/归档卡 | done/archived | 409 `not_dispatchable` | 不复活 |
| 上游 5xx/不可达 | 任一 REST 调用失败 | 502 `upstream_unavailable` | 补偿快照 |
| 默认文件非法 | 类型/范围不对 | 500 `invalid_defaults` | 不写坏快照 |
| metadata 库不可用 | workbench.db 打不开/锁住 | 503 `metadata_unavailable`；服务仍能起，`/healthz` 可用 | 不拖垮整个服务 |
| 并发派发 | 两张请求同时快照/补偿 | 只删除本次请求写入的快照；不丢其他键 | JSON1 按 key 更新 |
| running 重派 | 卡已 running | 200 `{status:"running",position:0}`，不 PATCH | N/A |
| 删除竞态 | GET 后卡被删，PATCH 返回 404 | 404 `not_found` | 补偿快照 |
| bulk 绕过 | `POST /tasks/bulk` status=ready，任一 id 无 accept | 400 `missing_acceptance`，整批不转发 | 中间件拦截 |
| reclaim 绕过 | `POST /tasks/{id}/reclaim`，卡无 accept | 400 `missing_acceptance` | 中间件拦截 |
| 直建 ready | `POST /tasks` 未带 `triage` | 400 `missing_acceptance`；workbench 建卡必须 triage | 中间件拦截 |
| 未知 id 直 PATCH | 无 metadata 且上游 404 | 404 `not_found` | 先 GET 卡再判 |
| 快照归属 | 相同快照并发，创建者失败 | 仅 token 匹配时删除；另一请求快照保留 | `write_token` |
| metadata 大批量 | ids > 200 | 客户端按 100 分块，结果合并完整 | 不静默截断 |
| 默认值超大 | minutes_per_task 1e300 | 500 `invalid_defaults`，不写快照 | 上界校验 |

## Code Map

- `workbench-app/server.py` — FastAPI 薄壳。新增 dispatch、`GET /api/workbench/metadata`、**ready 中间件闸门**（单卡 PATCH、bulk、reclaim、POST 建 ready）；PATCH 后用 GET `/tasks/{id}` 校验落点，再读 `/board` 算位次；用 `urllib` 自调用本机上游 REST（host/port 由启动参数解析，`0.0.0.0`/`::` 回落 `127.0.0.1`）；单次 1s、总预算 <3s；不 import 上游内部模块。
- `workbench-app/workbench.db` — workbench 自有 `task_metadata(task_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, updated_at INTEGER NOT NULL, write_token TEXT)`；`WORKBENCH_DB` 可覆盖；启动幂等建表。metadata 按 key 用 SQLite JSON1 `json_set`/`json_remove`；补偿只删 token 匹配的快照。
- `workbench-app/defaults.json` — `budget` 三键 + `queue.minutes_per_task: 7`；启动与派发时校验收敛数值/数组类型。
- `workbench-app/tests/test_dispatch.py` — 新增可重复测试（stdlib `unittest` + `subprocess`；临时 `HERMES_KANBAN_HOME`/`WORKBENCH_DB`；覆盖下方矩阵）。
- `workbench-ui/src/api/workbench.ts` — `dispatchTask`、`hasAcceptance(task, metadata?)`、`getTaskMetadata(ids)`、`mergeTaskMetadata(tasks, map)`；错误码包含 `missing_acceptance`/`blocked_by_parent`/`not_ready`/`not_dispatchable`/`not_found`/`invalid_defaults`/`metadata_unavailable`/`upstream_unavailable`/`dispatch_failed`。
- `workbench-ui/src/platform.ts` — 复用 `api`；错误 message 含后端 `error` 文本；客户端优先精确匹配 code，状态码兜底。
- `GET /api/plugins/kanban/board`、`GET/PATCH /api/plugins/kanban/tasks/{id}` — 仅依赖这三条公开 REST；派发后 `/board` 自调用加唯一查询参数避免与 UI 轮询读合并。
- `_bmad-output/planning-artifacts/epics.md`、`_bmad-output/specs/spec-jarvis-workbench/ARCHITECTURE-SPINE.md`、`_bmad-output/implementation-artifacts/epic-1-context.md` — 更新 1.2 的 metadata 读取路径/存储决策/routing 范围，避免与实现矛盾。
- `workbench-app/README.md` — 更新文件表与同步流程（`workbench-ui` + `npm run build`；`defaults.json`、`workbench.db`、dispatch/metadata 端点）。
- `.gitignore` — `workbench-app/workbench.db*` 与 `workbench-app/web/*.bak-*`。
- 多 board：workbench 固定当前 board，metadata 只按 `task_id` 键；多 board 支持 deferred。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/server.py` -- 实现 dispatch + metadata GET + PATCH-ready 中间件闸门；lane/board/409 落点校验、补偿归属、1s×3 总预算、host/port 解析 -- 不可绕过的闸门
- [x] `workbench-app/server.py` -- workbench.db `task_metadata` 表 + JSON1 按 key 更新 + 启动容错（DB 坏了服务仍能起）-- metadata 存储
- [x] `workbench-app/defaults.json` -- 全局预算、7 分钟/张，并校验类型/范围 -- 单一默认源
- [x] `workbench-ui/src/api/workbench.ts` -- 调用、错误码、`hasAcceptance(task, metadata?)`、`mergeTaskMetadata` -- 前端可调用
- [x] `workbench-app/tests/test_dispatch.py` -- 可重复覆盖矩阵（含直接 PATCH 绕行、lane review、board 失败、幂等、done、非法默认、共享库无 workbench 对象）-- 回归
- [x] `.gitignore` -- 忽略 `workbench-app/workbench.db*` 与 `workbench-app/web/*.bak-*` -- 运行期/备份
- [x] `_bmad-output/planning-artifacts/epics.md`、`.../ARCHITECTURE-SPINE.md`、`.../epic-1-context.md`、`workbench-app/README.md` -- 同步存储与端点变化 -- 文档一致
- [x] `workbench-app/server.py` -- 重启服务并用 `/healthz` 回归 -- 不破坏现有

**Acceptance Criteria:**
- Given 卡无 accept, when 直接 `PATCH status=ready`、bulk ready、reclaim 或 POST dispatch, then 400 `missing_acceptance`，状态不变
- Given `POST /tasks` 未带 triage, when 经工作台服务建卡, then 400 `missing_acceptance`，不直建 ready
- Given 两个相同快照并发, when 创建者失败, then 仅 token 匹配的快照被删，另一请求快照保留
- Given 201 个 id, when `getTaskMetadata`, then 分块请求并返回全部 201 条
- Given accept 非空, when dispatch, then 200，状态 ready，`GET /api/workbench/metadata?ids={id}` 的 `contract_defaults` 含三键
- Given 未完成 parent, when dispatch, then 409 `blocked_by_parent`，卡落 todo，ready 队列不新增
- Given 卡被上游落到 review/todo, when dispatch, then 409 `not_ready`/`blocked_by_parent` 且 `status` 为实际落点，快照补偿删除
- Given PATCH 成功但 `/board` 不可读, when dispatch, then 502 `upstream_unavailable`，不返回 position 0
- Given 已 ready, when 再次 dispatch, then 200，不重复入队、不覆盖快照，位次按当前 ready 队列重算
- Given done/archived, when dispatch, then 409 `not_dispatchable`
- Given defaults.json 类型非法, when dispatch, then 500 `invalid_defaults`，不写快照
- Given 改动完成, when 检查 `~/.hermes/kanban.db`, then 无 workbench 表/列，`integrity_check=ok`
- Given 测试, when 运行 `workbench-app/tests/test_dispatch.py`, then 全部通过
- Given 服务重启, when `curl /healthz`, then `{"ok":true}`，现有看板不受影响

## Implementation Notes

- **最终架构：** metadata 存 `workbench-app/workbench.db` 的 `task_metadata`（`WORKBENCH_DB` 可覆盖）；`server.py` 对 `kanban.db` 零读写、零 DDL，不 import 上游内部模块；上游仅经回环公开 REST（`GET /board`、`GET/PATCH /tasks/{id}`）。
- **闸门：** `PATCH status=ready` 由 workbench 中间件统一拦截；无 accept 直接 400，不转发。dispatch 的自身 PATCH 因快照/契约已存在而放行。原生 9119 dashboard 不经过本服务，属范围外。
- **快照/补偿：** 先写快照再 PATCH；只补偿本请求写入的快照（记录写入内容/使用 JSON1 按 key 删除），不整块覆盖其他键。状态与快照跨 REST + 本地库，非单事务，靠补偿收敛。
- **验证：** `npm run typecheck`；`workbench-app/tests/test_dispatch.py` 全绿；真实 `/healthz` + `/board`；共享库无 workbench 对象。
- **范围假设：** 固定当前 board；metadata 只按 `task_id`。多 board 支持 deferred。
- **1.3 依赖：** 建卡/编辑契约需要 1.3 的 metadata 写端点；1.2 提供读端点与 `mergeTaskMetadata` 供其接线。
- **2026-11-05 复验：** `python3 workbench-app/tests/test_dispatch.py` 21/21 通过；`npm run typecheck` + `npm run build` 通过（`platform.ts` 变更已重新分发，4 个产物与宿主 `diff -q` 一致）；直接 `PATCH status=ready` 无验收 → 400 `missing_acceptance`；非 ready PATCH 透传 → 上游 404；`/healthz` ok、`/board` 200；共享 `kanban.db` 无 `workbench%` 对象、`tasks` 无 `metadata` 列、`integrity_check=ok`。
- **2026-11-05 二次审查修复：** 闸门扩展到无 triage 建卡、bulk ready、reclaim；PATCH ready 缺验收时先 GET 卡，404 透传。landing truth 改为 PATCH 后 GET `/tasks/{id}`，只有 ready 才读 `/board`（未命中重读一次，仍不命中 502）。`task_metadata` 增加 `write_token` 归属列（响应不暴露），补偿只在 token 匹配时删除。数值上限 `max_minutes≤10080` / `max_cost≤1000` / `minutes_per_task≤1440`，卡覆盖同样校验；ETA 溢出返回 500 `dispatch_failed`。dispatch 顺序改为 running → done/archived → acceptance。前端 `hasAcceptance(metadata)` 不再读 `task.metadata`，`getTaskMetadata` 按 100/请求分块。补测上游挂起、PATCH 404、todo 失败、token 归属、非法覆盖、201-id 分块；共 32 项通过。`/board` 读取保留 `include_archived=true` 以与 UI 轮询分键，不再使用无效的 `_wb` 参数。不实现 provenance / `contract.ready`，不改自调用线程池，不加 recompute watcher；已跟踪 `.bak-*` 与旧 UI 文件清理 deferred。

## Spec Change Log

### 2026-11-05 — 审查回环 1（bad_spec：闸门可绕过 + 边角语义/验证缺失）

- **触发：** 三路审查发现 dispatch 之外的 `PATCH status=ready` 可绕过验收闸门；blocked→review 落点被谎报为 200 ready；`/board` 读失败返回 position 0；409 停卡未校验；快照补偿存在并发归属问题；错误码/超时/默认值校验不足；无可重复测试；epics/architecture/context/README 与最终存储决策不一致；Implementation Notes 保留三代已废弃方案。
- **修订：** 增加 PATCH-ready 中间件闸门与非冻结边角矩阵；补 lane/board/409/404/5xx/非法默认/metadata 库故障处理；快照按 key 更新与归属补偿；1s×3 总预算；错误码与客户端映射；新增可重复测试任务；同步规划文档与 README；把历史方案移入本日志，Implementation Notes 只保留最终架构。
- **避免的坏状态：** 空验收卡从看板拖入 ready；卡实际落 review 却显示派发成功；上游读失败被当成成功；并发派发互删快照；上游升级 dict/文档漂移。
- **KEEP：** workbench 自有 metadata 库、REST-only、先快照后 PATCH、双宿主无共享库对象、位次/ETA 公式、`getTaskMetadata` 读取路径。

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1 | 闸门只在 dispatch，PATCH/看板可绕过 | high | bad_spec | `Board.tsx` 的 `flowDrop` 直接 PATCH ready；上游 recompute 也会提升 todo；spec 的「不可绕过」不成立 |
| BH2 | 多 board 未固定，metadata 只按 task_id | low | defer | workbench 当前只用当前 board；多 board 支持记 deferred |
| BH3 | 只用 lane==todo 判断失败，blocked→review 返回 200 ready | medium | bad_spec | `unblock_task` 可按 review resume 落 review；代码只判 todo |
| BH4 | `/board` 读失败返回 200 position 0 | medium | bad_spec | `_board_lane` 失败返回 `(0,None)`，dispatch 未检查 |
| BH5 | 409 停卡 fire-and-forget，响应硬写 todo | medium | bad_spec | `_patch_status(todo)` 返回值被忽略；review→todo 可能落 ready |
| BH6 | 快照补偿并发竞态，可能删他人快照 | medium | bad_spec | 整块读改写；无归属标记 |
| BH7 | metadata 整块覆盖会丢 1.3 并发写入 | medium | bad_spec | `write_workbench_metadata` 覆盖整个 JSON |
| BH8 | 无 contract/bucket 写端点 | low | defer | 属 1.3 建卡流程；本故事只读/快照 |
| BH9 | epics/context 仍写 GET /tasks 暴露 metadata、三层含 routing | low | bad_spec | 与最终存储/范围不一致 |
| BH10 | ARCHITECTURE-SPINE 仍写 PATCH metadata、sync-ui.sh | low | bad_spec | 文档漂移 |
| BH11 | Implementation Notes 保留三代废弃方案 | low | bad_spec | 已移入本 Change Log，只留最终架构 |
| BH12 | 矩阵/AC 漏 done、5xx、非法默认、running；位次措辞矛盾 | medium | bad_spec | 已补 Additional Edge Cases 并统一「重算」 |
| BH13 | 无可重复测试 | high | bad_spec | 仅一次性 curl；新增 `tests/test_dispatch.py` |
| BH14 | 自调用固定 127.0.0.1，绑定其他 host 会 502 | low | bad_spec | 已改 host 解析 |
| BH15 | 5s×3 最坏 15s，超 3s 回执 | medium | bad_spec | 改 1s×3 总预算 |
| BH16 | 客户端错误码缺 invalid_defaults 等；409 兜底误映射 | low | bad_spec | 已补 code 与精确映射 |
| BH17 | `hasAcceptance` 期望 task.metadata，board 不携带 | low | bad_spec | 已改显式 metadata/merge helper |
| BH18 | defaults 只校验键存在，不校验类型/范围 | medium | bad_spec | 已加类型/范围校验 |
| BH19 | README/legacy 文件仍旧架构 | low | defer | 已记 deferred；README 更新在本次任务内 |
| BH20 | `.bak-*` 未 ignore | low | bad_spec | 已加 ignore；已跟踪备份清理仍 deferred |
| BH21 | docstring 声称不碰 kanban.db 但挂载 router | low | bad_spec | 已改措辞区分 dispatch 路径与挂载 router |
| BH22 | 新文件缺尾随换行 | low | bad_spec | 重派生时补齐 |
| EC1 | 同 BH3 | carried | carried | 同一落点/同一主张 |
| EC2 | 同 BH4 | carried | carried | 同上 |
| EC3 | 无 metadata 的不存在 id 先返 400 而非 404 | medium | bad_spec | 先 GET 卡再校验 acceptance |
| EC4 | 同 BH5 | carried | carried | 同上 |
| EC5 | workbench.db 初始化失败拖垮整个服务 | medium | bad_spec | 启动容错，端点 503，healthz 仍可用 |
| EC6 | 同 BH14 | carried | carried | 同上 |
| EC7 | 同 BH18 | carried | carried | 同上 |
| EC8 | 同 BH16 | carried | carried | 同上 |
| EC9 | done 卡在 GET 与 PATCH 之间被复活 | medium | defer | 上游无 compare-and-swap；窗口极小，记 deferred |
| EC10 | GET 后卡被删，PATCH 404 被映射成 500 | low | bad_spec | 补 404 映射 |
| EC11 | 同 BH1 | carried | carried | 同上 |
| VG1 | 验收闸门无可重复覆盖 | high | bad_spec | 已加测试任务 |
| VG2 | 快照/回滚无可重复覆盖 | high | bad_spec | 已加测试任务 |
| VG3 | 位次/ETA/落点无可重复覆盖 | high | bad_spec | 已加测试任务 |
| VGO1 | 同 BH4 | carried | carried | 同上 |
| VGO2 | 同 BH3 | carried | carried | 同上 |
| VGO3 | `/board` coalesced_read 可能读到 PATCH 前状态 | medium | bad_spec | 自调用加唯一查询参数/失败重读，避免与 UI 轮询合并 |
| VGO4 | 同 BH9 | carried | carried | 同上 |

**分组与路由：**
- **bad_spec 组**（BH1/3/4/5/6/7/9/10/11/12/13/14/15/16/17/18/20/21/22、EC3/5/10、VG1-3、VGO3）→ 回环：修订非冻结段并重派生。
- **defer 组**（BH2/BH8/BH19/EC9）→ `deferred-work.md`。
- 无 false；其余为同源 carried。

### 回环 1 复审（2026-11-05，二轮）

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH2-1 | bulk/reclaim/POST 仍可绕过闸门 | high | patch | 已定向扩展中间件；recompute/9119 记为范围外 |
| BH2-2 | 上游 recompute_ready 绕过闸门 | medium | defer | 上游内部路径，本服务无法拦截；记入范围边界 |
| BH2-3 | `_wb` 缓存绕过无效（coalesced 按绑定参数）| medium | patch | 改为 PATCH 后 GET `/tasks/{id}` 作为落点真相 |
| BH2-4 | 快照归属按值比较，相同值互删 | high | patch | 加 `write_token` 列，按 token 归属 |
| BH2-5 | `_land_in_todo` 仍可能硬写 todo | medium | patch | 无 task.status 时返回 502 |
| BH2-6 | 无 metadata 写端点，现有卡无法进 ready | medium | defer | 1.3 提供建卡/契约写端点 |
| BH2-7 | ETA 从 position=1 起算 7 分钟 | false | — | epics 公式即 `now + 每张平均耗时 × 位次`；position 1 = 一个槽位 |
| BH2-8 | metadata ids 静默截断 200 | medium | patch | 客户端按 100 分块合并 |
| BH2-9 | 默认值无上界，huge minutes 触发 500 | medium | patch | 加上界与 OverflowError 兜底 |
| BH2-10 | `hasAcceptance` 仍回退 task.metadata | medium | patch | 改为必须显式传 metadata |
| BH2-11 | ARCHITECTURE 元数据表仍指向上游 task.metadata | medium | patch | 标为未存储/deferred |
| BH2-12 | localStorage 与 metadata.bucket 矛盾 | low | patch | 文档改为 bucket 在 workbench.db，localStorage 只存排序 |
| BH2-13 | triage 声明未落地（换行/.bak/legacy）| low | patch/defer | 补换行；tracked `.bak` 与 legacy 清理 deferred |
| BH2-14 | deferred-work.md 未进 diff | low | patch | 最终 diff/提交纳入 |
| BH2-15 | 测试直接写 kanban.db | low | defer | 测试专用 setup，加注释；不视为生产耦合 |
| BH2-16 | 已声称的修复缺回归 | high | patch | 补 rollback/todo 失败/非法覆盖/超时/404-after-GET 测试 |
| BH2-17 | 依赖上游英文 detail 分类 | medium | patch | 加测试并保留 409 兜底 |
| BH2-18 | 未知 id 400 vs 404；running 需 metadata | medium | patch | 先 GET 卡；running 先返回 200 |
| BH2-19 | 两处图未对齐 | low | patch | 文档修正 |
| BH2-20 | 快照无来源/时间/defaults 版本 | low | defer | 三键快照是 epics 约定；provenance 后续 |
| BH2-21 | `contract.ready` 未被使用 | low | defer | 1.3 建卡时定义语义 |
| BH2-22 | manifest/install/epic-context 文案过期 | low | patch | 文档修正 |
| BH2-23 | 自调用占线程池，40 并发可自锁 | low | defer | 本地单用户；记 known limitation |
| BH2-24 | 测试 free_port 竞态/代理重复 | low | defer | 测试基础设施优化 |
| EC2-1 | `_land_in_todo` 读错 links，拒因错误 | medium | patch | 从 fetched 响应读 links/link_tasks |
| EC2-2/3/4/6/7/8/10/11/12/13/14 | 同 BH2-1/4/3/9/18/9/23/1/1/9/4 | carried | carried | 同源 |
| VG2-1 | rollback 归属未覆盖 | high | patch | 加 created=False / token 不匹配测试 |
| VG2-2 | todo 失败分支未覆盖 | high | patch | 加 todo PATCH 失败测试 |
| VG2-3 | 非法卡覆盖未覆盖 | medium | patch | 加非法 override 测试 |
| VG2-4 | 1s/2.9s 预算未观测 | medium | patch | 加 hanging proxy 限时测试 |
| VG2-O1 | bulk/POST/recompute 旁路 | high | patch/defer | 同 BH2-1/2 |
| VG2-O2 | 相同快照归属不可区分 | high | patch | 同 BH2-4 |

**复审结论：** 无新增 bad_spec/intent_gap 需整轮重派生；采用定向补丁 + 测试 + 文档同步。gate 范围在 Additional Edge Cases 与 README/架构中明确：workbench 服务的 dispatch、单卡/bulk ready、POST 建 ready、reclaim 被拦；上游 recompute 与 9119 原生页不覆盖。

## Design Notes

- 闸门由 workbench 服务统一执行：dispatch、单卡/bulk `PATCH ready`、`reclaim`、`POST /tasks` 直建 ready 都过同一检查；上游 `recompute_ready`（parent 完成自动提升）与原生 9119 dashboard 不经过本服务，范围外并写入文档。
- 快照归属用 `write_token`：补偿只删本次写入且 token 仍匹配的快照，避免两个相同快照互删。
- 固定当前 board；metadata 只按 `task_id`。多 board 支持 deferred。
- 状态与快照跨 REST + 本地库，非单事务；先写快照、失败按归属补偿，避免谎报。
- 队列位次取 `/board` ready 列的 1-based 顺序；position 1 表示前面还有一个执行槽位（ETA = now + 7 分钟）；重复派发重算位次但不重复入队、不覆盖快照。
- 1.3 负责建卡/编辑契约的 metadata 写端点；1.2 提供读端点与 merge helper。
- 已知限制：dispatch 同步自调用会占用一个服务线程（本地单用户可接受）；tracked `.bak-*` 清理与 legacy `app.js`/`sync-ui.sh` 删除 deferred；`contract.ready` 语义留到 1.3。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck` -- 无类型错误
- `python3 workbench-app/tests/test_dispatch.py` -- 全部通过（临时 `HERMES_KANBAN_HOME`/`WORKBENCH_DB`）
- 隔离实例手动抽查：直接 PATCH 无 accept → 400；dispatch 200/400/409/幂等/位次/eta
- `python3 -c "..."` 检查 `~/.hermes/kanban.db` -- 无 `workbench%` 对象、`tasks` 无 `metadata` 列、`integrity_check=ok`
- `systemctl --user restart jarvis-workbench` -- 退出码 0
- `curl -s --noproxy 127.0.0.1 http://127.0.0.1:8787/healthz` -- `{"ok":true}`

**Manual checks:**
- 8787 看板仍正常，Console 无报错