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
  summary: 在真实插件宿主里运行待办三区（或给插件宿主提供 `/api/workbench/*` 代理/后端）。
  evidence: 插件宿主没有 workbench 后端，当前只显示看板并提示使用独立版 8787；`dist/plugin` 只做了产物字节一致性检查，未在 Hermes dashboard 里跑待办运行时。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 处理没有 `bucket` 的 triage 卡：原生 dashboard/CLI 建的卡不会出现在今日/长期。
  evidence: 待办只渲染 `status=triage` 且 `metadata.bucket` 为 `today|long` 的卡；仓库外建卡没有 metadata 写入口，目前不可见，需要 triage 收件箱或回填规则。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 支持清空 `contract.budget` 的单个键（或整体清空）。
  evidence: metadata 写端点用 JSON1 `json_patch` 做合并，校验层不接受 null/空 budget；预算覆盖只能设置或覆盖，不能删除。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: 抽取 Board / TodoPage 共享的轮询、toast、拖拽与 loading hook。
  evidence: `Board.tsx` 与 `TodoPage.tsx` 各自实现 5s 轮询、toast 定时器与 HTML5 拖拽；行为已出现重复维护点（如拖拽高亮/清理），后续做详情抽屉或多视图时应先抽 hook。

- source_spec: `_bmad-output/implementation-artifacts/spec-1-3-todo-three-zone.md`
  summary: Story 1.4 派发回执：派发后显示「已入队 · 第 N 位 · 预计 HH:MM」。
  evidence: 1.3 成功派发只提示「已派发」并刷新；dispatch API 已返回 `position`/`eta`，但回执 UI、刷新后位次重算与失败重试文案属于 1.4。