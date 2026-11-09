- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 清理 `workbench-app/web/app.js`、`web/boot.js`、`web/sdk-shim.js` 与 `workbench-app/sync-ui.sh` 这组旧架构遗留文件。
  evidence: （BH-7/BH2-6）`web/app.js` 仍含一份旧 8 列映射（6 处列名命中）但已不被 `index.html` 加载；`sync-ui.sh` 复制 `app.js` 的流程已被 `workbench-ui/scripts/distribute.mjs` 取代。Story 1.1 明确不动宿主副本与旧脚本，故留作独立清理。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 为看板卡片渲染补一个可重复测试（组件测试，或把本次的 headless Chromium/CDP 探针固化成脚本）。
  evidence: （BH-5/VG-1/VG2-1/VG2-2/BH2-12）`workbench-ui` 无 test 脚本/测试框架，`Board.tsx` 的空字段隐藏与绿点删除目前只靠一次性 CDP 探针验证；任何后续改动都可静默回退而 typecheck/build/healthz 仍全绿。探针本身不需要新增 npm 依赖，可提交到 `workbench-ui/scripts/`。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 清理 `columns.ts` 中无消费者的 `COL_KEYS` 导出，并收窄 `COL_BY_KEY` 的暴露面。
  evidence: （BH-13）`grep -rn COL_KEYS workbench-ui/src` 只命中定义本身（`columns.ts`）；该导出自 commit `10f2685` 起就未被使用，不属于 Story 1.1 的改动范围。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 定义构建备份与生成物的策略（`.bak-*` 忽略/保留期、`workbench-app/web/index.js` 是否继续入库）。
  evidence: （BH-2/BH2-5）`distribute.mjs` 每次构建都在 `workbench-app/web/` 生成 `.bak-<时间戳>`；两轮回环后已有 8 个被 git 跟踪、6 个未跟踪（14-51-57 与 15-08-57 两组）。`.gitignore` 无 `.bak-*` 规则，生成物入库会让每次构建都产生噪声 diff。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 给构建链路加“新鲜度”门禁：从当前源码重新构建后，校验 `workbench-app/web/` 与插件宿主的产物确实来自本次构建。
  evidence: （VG2-3）`workbench-ui/dist/` 被 gitignore，`diff -q` 只比较“上一次 dist”与“上一次分发”；若改了源码却跳过 `npm run build`，两个宿主仍可继续提供旧行为而所有检查通过。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 把 `columns.ts` 中 `review` 列的硬编码色值 `#8b5cf6` 改为主题 token，或明确记录为例外。
  evidence: （BH2-14）冻结段要求“明暗双主题走既有 token，不硬编码色值”，但 `columns.ts` 的 `review` 项仍是字面色值；该行自 `10f2685` 起存在，非本故事引入。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 对插件宿主做一次真实运行时验收（打开 9119 工作台 tab，或在插件宿主环境里跑最小渲染探针）。
  evidence: （BH2-7）当前只用 `dist/plugin` 与宿主产物字节一致代替视觉验收；插件包 external React、运行在 Hermes SDK 宿主里，8787 的 standalone 探针不能覆盖这条运行时路径。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-contract-gate.md`
  summary: 支持多 board：dispatch/metadata 固定当前 board、metadata 只按 task_id 键。
  evidence: （BH2）上游支持多 board，workbench 目前只用当前 board；切 board 时同 id 的 metadata 可能串。需要 board 维度后再引入。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-contract-gate.md`
  summary: 处理 dispatch 的 done→ready TOCTOU：GET 与 PATCH 之间卡被完成时可能被重新打开。
  evidence: （EC9）上游 PATCH 无 compare-and-swap；窗口极小，但理论上可复活已完成卡。需要上游支持条件更新或客户端二次确认。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-contract-gate.md`
  summary: 实现 provenance / `contract.ready` 语义；1.2/1.3 只使用 `contract.input` / `accept` / `budget`。
  evidence: （二次审查 13）现有契约字段不参与派发决策；`contract.ready` 与 provenance 留给后续验收故事。1.3 只在建卡时写入空 `ready` 占位，仍未定义其闸门语义。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-contract-gate.md`
  summary: 给上游 `recompute_ready`（parent 完成自动提升）与原生 9119 dashboard 补闸门，或加 watcher。
  evidence: （二次审查 1/13）workbench 只能拦经过本服务的入口；上游 recompute 与 9119 不经过 8787，1.2 明确不添加 watcher，属范围外已知限制。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-2-contract-gate.md`
  summary: 清理已跟踪的 `workbench-app/web/*.bak-*` 与旧 `web/app.js` / `boot.js` / `sdk-shim.js` / `sync-ui.sh`。
  evidence: （二次审查 11）本次只把新 `.bak-*` 加入 `.gitignore`，不删除任何已跟踪文件；旧文件清理仍是独立任务（见 1.1 的 BH-7/BH-2 条目）。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 在真实插件宿主里运行待办三列（或给插件宿主提供 `/api/workbench/*` 代理/后端）。
  evidence: 插件宿主没有 workbench 后端，当前只显示看板并提示使用独立版 8787；`dist/plugin` 只做了产物字节一致性检查，未在 Hermes dashboard 里跑待办运行时。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 处理没有 `bucket` 的 triage 卡：原生 dashboard/CLI 建的卡不会出现在今日/长期。
  evidence: 待办只渲染 `status=triage` 且 `metadata.bucket` 为 `today|long` 的卡；仓库外建卡没有 metadata 写入口，目前不可见，需要 triage 收件箱或回填规则。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 支持清空 `contract.budget` 的单个键（或整体清空）。
  evidence: metadata 写端点用 JSON1 `json_patch` 做合并，校验层不接受 null/空 budget；预算覆盖只能设置或覆盖，不能删除。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 抽取 Board / TodoPage 共享的轮询、toast、拖拽与 loading hook。
  evidence: `Board.tsx` 与 `TodoPage.tsx` 各自实现 5s 轮询、toast 定时器与 HTML5 拖拽；1.4 又在 TodoPage 增加了回执 6s 定时器与常驻 live-region，行为已出现重复维护点（如拖拽高亮/清理、定时器清理），后续做详情抽屉或多视图时应先抽 hook。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-fullscreen-verify.md`
  summary: 抽取 `workbench-ui/scripts/` 两个探针重复的 CDP harness 到共享 `scripts/lib/`。
  evidence: （BH23）`todo-probe.mjs` 与 `verify-probe.mjs` 各自实现浏览器启动/连接/断言/收尾逻辑；2.1 新增第二份副本后，任何探针基础设施改动都要同步两处。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-fullscreen-verify.md`
  summary: 让 48h 自动收的记录 comment 从 best-effort 变为可核对的终态（失败可重试/可观测）。
  evidence: （BH5 残留）`_auto_collect_locked` 在 PATCH 落 `done` 后调用 `_auto_collect_comment` 但忽略返回值；comment 连续失败只打 stderr，卡已收但「48h 自动收」审计记录可能缺失，只能靠日志发现。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-fullscreen-verify.md`
  summary: 处理 `/inbox` 并行详情失败的尾部：让 `failed_ids` 可见/可重试，并与 `/inbox/count` 口径对齐。
  evidence: （BH3 残留）`inbox()` 在整体 deadline 下跳过失败详情并返回 `failed_ids`，但 `count` 只数成功项，`/inbox/count` 按 `/board` 计数；UI 目前不展示 `failed_ids`，计数与可见队列可能不一致。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-1-fullscreen-verify.md`
  summary: 决定顶栏计数 1s `/board` 缓存的去留，或在验收动作后主动失效缓存。
  evidence: （BH2 残留）`INBOX_CACHE_SECONDS=1.0`；verify 成功后 `/inbox/count` 最多 1s 仍显示旧值，验收页与顶栏两个 5s 轮询共用该缓存。当前单用户规模可接受，记录为已知延迟。

- source_spec: `_bmad-output/implementation-artifacts/spec-2-2-p0-notifications.md`
  summary: 补一条通知 sweep 与 accept/reject 并发的 guard 测试（board 读取窗口内完成动作）。
  evidence: （VG6）`server.py` 的 `notification_lock` 已把 board 读取 + collect 与动作串行化，但测试只顺序调用 sweep/action；重叠窗口为一次 board 读取，下一轮 sweep 会自愈。可仿照 `tests/test_dispatch.py:1598` 的 GateLockTests 构造阻塞 board 读取。

- source_spec: `_bmad-output/implementation-artifacts/spec-visual-align-007.md`
  summary: 窄视口（<=900px）侧栏隐藏后，给「新建待办」提供一个顶栏入口或把侧栏做成抽屉。
  evidence: （BH11）007 的「＋ 新建待办」只在侧栏；`<=900px` 隐藏侧栏后只剩待办 tab 内的 composer，创建入口不再显式可见。属于响应式增强，不改 007 桌面外壳。

- source_spec: `_bmad-output/implementation-artifacts/spec-visual-align-007.md`
  summary: 给 Board / TodoPage / DonePage 的 5s 轮询抽共享的 sequence guard（或共享 hook），避免慢响应乱序覆盖新数据。
  evidence: （EC2）三处都用 `setInterval` + 无请求序号；DonePage 本次新增，Board/Todo 是既有模式。只修 DonePage 会留下不一致，应在共享轮询 hook 里一次修三处。

- source_spec: `_bmad-output/implementation-artifacts/spec-visual-align-007.md`
  summary: 为 PWA manifest 的 `theme_color/background_color` 补回归断言，并决定暗色安装时的单色启动画面策略。
  evidence: （VG7/BH2）manifest 是静态单色（当前 `#ececea`），浏览器只在安装/启动读取；四个探针与 `workbench-app/tests` 都没有 fetch `/manifest.webmanifest`。动态 manifest 或按主题生成是后续 PWA 工作。

- source_spec: `_bmad-output/implementation-artifacts/spec-visual-align-007.md`
  summary: 已完成视图在轮询失败后保留旧数据时，显示最后更新时间/陈旧标识。
  evidence: （BH23）`DonePage` 出错时保留上一次 `board`，只显示 `.wb-done-error`；没有 `last_updated`/stale 提示，用户无法判断列表是新的还是旧的。

- source_spec: `_bmad-output/implementation-artifacts/spec-visual-align-007.md`
  summary: 把已完成视图的 `/board` 读取、列 key 与完成日分组并入共享 board 数据层。
  evidence: （BH9）`Board` 在 standalone 常驻并 5s 轮询，`DonePage` 打开时再起一份 5s `/board`；两处各自定义 `BoardData/BoardColumn`，`DonePage` 用字面量 `"done"` 匹配列。spec 明确 DonePage 只在激活时挂载，故本次按设计实现，共享数据层留后续。

- source_spec: `_bmad-output/implementation-artifacts/spec-3-1-self-report.md`
  summary: 在 Hermes `kanban_request_review` tool schema 的 metadata 描述中加入 self_report 三行契约。
  evidence: 当前 tool schema 只列 changed_files/tests_run/commit/decisions；self_report 只能从 workbench-conventions skill 发现。修改 `~/.hermes/hermes-agent/tools/kanban_tools_schemas.py` 属上游改动，超出 3.1 的“上游零改动”边界；仓库内已补 README 契约说明，待上游愿意改 schema 时再补。

- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-solidified.md`
  summary: 固化 verified overlay 目前只按 path 幂等；rework 后同一 path 会沿用旧 verified，且无法撤销或清理旧 path。
  evidence: `workbench-app/server.py:2052-2072` 的 overlay 无 review_requested_at/run 维度；spec Design Notes 明确 path 幂等，属已选设计的已知边界。后续可按 review 轮次隔离或增加撤销/清理。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-solidified.md`
  summary: solidified 的 type/status 枚举在 server、UI、README、SKILL、spec 五处重复，缺少跨语言 schema 或 cross-check 测试。
  evidence: `workbench-app/server.py:141-142`、`workbench-ui/src/api/workbench.ts:490-491`、`workbench-app/README.md`、`~/.hermes/skills/workbench-conventions/SKILL.md`、`spec-3-2-solidified.md`；一侧新增类型不会自动让其他侧失败。

- source_spec: `_bmad-output/implementation-artifacts/spec-3-3-wake.md`
  summary: 增加 `POST /api/workbench/wake/sweep` 手动触发与成功唤醒日志，便于运维和测试不等 30s 调度 tick。
  evidence: 当前只有后台 `WakeScheduler`；`wake_due` 成功不写日志。3.3 review R18，属可观测性增强。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-3-wake.md`
  summary: legacy triage/todo 卡缺验收时 wake 只返回 400，不停车到 scheduled，仍可能被上游 triage specifier/recompute_ready 接走。
  evidence: `wake_task` 对 STORE_STATUSES 放行；缺验收不 PATCH；新仓库卡走 scheduled，遗留卡需迁移或额外 guard。3.3 review R19。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-3-wake.md`
  summary: 外部写入的 malformed `wake_at` 被 due query 静默跳过，无日志/清理。
  evidence: `due_wake_tasks` 只选 integer；`_has_wake_at` 只用于清理；3.3 review R22。

- source_spec: `_bmad-output/implementation-artifacts/spec-3-4-executor-contract.md`
  summary: `claude_code` 的 summary 未限长，可能把多 KB 的最终 result 塞进单行 JSON。
  evidence: `translate_result` 原样使用 `result`；3.4 review R17，当前无长度契约，留到 3.5 或后续统一截断策略。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-4-executor-contract.md`
  summary: executor-run 的 stdout/stderr 与日志无大小上限，且 `.runs/` 默认落在目标仓库，可能产生未跟踪日志。
  evidence: `communicate` 全量缓冲并整段写日志；`.gitignore` 只覆盖 Jarvis 仓库，目标仓库需自行忽略；3.4 review R19。
