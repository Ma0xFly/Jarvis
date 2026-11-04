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

- source_spec: `_bmad-output/implementation-artifacts/spec-1-1-native-kanban.md`
  summary: 更新 `workbench-app/README.md` 的文件表与“同步 UI”流程，改为 `workbench-ui` 源码 + `npm run build`。
  evidence: （VG2-O1）README 仍写 `web/app.js` 是工作台 UI、要求改 dashboard 插件后跑 `./sync-ui.sh`；该流程已废弃，照做会绕过 `distribute.mjs`。