---
title: '视觉对齐 007：工作台外壳与视觉语言'
type: 'refactor'
created: '2026-11-05'
status: 'done'
route: 'dispatch'
review_loop_iteration: 0
context: []
baseline_commit: 'be8820e5b64c72dcbb8987cae15633fc7ffd1886'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** 当前前端是行为优先的朴素界面，与 `sketches/007-dispatch-pipeline` 的视觉语言差距大：没有左导航、应用框架、统一排版/颜色/卡片/间距，用户认不出这是同一个工作台。

**Approach:** 以 007 为视觉基准重做 standalone 外壳：左侧 224px 导航（品牌、新建、待办/看板/已完成、底部状态）、50px 顶栏（tabs、待你处理、主题切换）、填充视口的 app 框架、007 的 light/dark token 与卡片/面板/排版；保留现有 IA 与行为（8 列看板、待办三区、全屏验收、通知中心、会话抽屉、PWA），并新增只读「已完成」视图。插件宿主只共享 token/卡片，不引入侧栏。

**决策（2026-11-05，人已确认）：**
- 新增「已完成」tab：只读，数据取看板 done 列，按 `completed_at` 本地日期分组；空态沿用 007 文案。
- 不加 hover「派发」按钮：保持 epics 的「hover 只换背景色」，拖拽派发不变。

## Boundaries & Constraints

**Always:**
- 视觉基准 = 007 的 tokens/布局/排版/卡片/间距；**不采用它的 5 列看板**（PLAN §5.3 明确是错误示例，保留 8 列）。
- 保留行为与 IA：待办三区、8 列看板、全屏验收、通知中心、会话抽屉、PWA；已完成视图只读。
- 明暗 token 单一来源；system 字体栈；不硬编码颜色。
- standalone 填充视口（24px canvas 边距 + 圆角 app 框）；不固定 007 的 1320×780 展示框。
- 插件宿主仍 board-only，不出现侧栏/待办/已完成；共享 token 与卡片。

**Never:**
- 不改数据结构/接口/业务行为（已完成视图只读；hover 不新增按钮）。
- 不引入 UI 组件库/字体文件；不采用 007 的静态项目列表（无数据源）。
- 不把 007 的 5 列或「今日仓库/待收」旧列带回看板。

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| 打开 standalone | 任意数据 | 224px 左导航 + 50px 顶栏 + 填充视口的内容区；007 token/排版 | N/A |
| 待办 | 三区数据 | 007 面板/轻卡片/凹槽流水线；交互不变 | N/A |
| 看板 | 任意数据 | 8 列（不出现 007 的 5 列），007 卡片/列样式 | N/A |
| 已完成 | 看板 done 列 | 按 `completed_at` 本地日分组的只读行；可打开会话 | 空态文案；会话缺失由查看器提示 |
| 验收/通知/会话 | 打开对应 UI | 007 的 overlay/panel/card 视觉；行为不变 | N/A |
| 主题切换 | 点 ☾/☀ | light/dark token 全量切换并持久化；首屏无闪烁 | N/A |
| 窄视口 | <900px | 侧栏隐藏，顶栏 tabs 仍可用 | 不横向溢出 |
| 插件宿主 | 9119 | 只有看板，无侧栏/待办/已完成；token 一致 | N/A |

</frozen-after-approval>

## Code Map

- `sketches/007-dispatch-pipeline/index.html` — 视觉基准：tokens、`.app`/`.sidebar`/`.topbar`/`.panel`/`.card`/`.donerow`/`.overlay`；`shots/*.png` 对照。
- `workbench-app/web/theme.css` — 独立版专用；用 007 light/dark 值替换旧绿/米色 token，并把 `--color-*` 映射到新 token（现有视图靠它继续工作）；body 用 canvas；删除未使用的 `#shell/#appbar` 样式。
- `workbench-ui/src/styles/base.css` — 新增 `--wb-side/--wb-app/--wb-faint/--wb-hair/--wb-col/--wb-accent-weak/--wb-accent-border`（插件宿主从 dashboard token 兜底）；新增 `.wb-shell`/`.wb-sidebar`/`.wb-topbar`/`.wb-views` 外壳；standalone 画布框，插件宿主不加框。
- `workbench-ui/src/App.tsx` — 独立版外壳：侧栏（品牌/新建/待办/看板/已完成/状态）、顶栏（tabs/待你处理/主题）、视图容器；新建按钮切到待办并聚焦 composer；计数由 TodoPage/Board 回调提供；插件宿主保持现有 board-only + notice。
- `workbench-ui/src/todo/TodoPage.tsx` — 新增可选 `onCounts({today,long})` 与 `focusComposer` 信号；composer 按 007 capture 样式重做；创建/派发/验收行为不变。
- `workbench-ui/src/board/Board.tsx` — 新增可选 `onStats({running,done})`；8 列、拖拽、派发闸门不变。
- 新增 `workbench-ui/src/done/DonePage.tsx` + `styles/done.css` — 仅在已完成 tab 挂载；5s 读 `/api/plugins/kanban/board`，取 done 列，按 `completed_at` 本地日倒序分组；行 = ✓ + 标题 + 摘要/时间 + 会话按钮；空态「还没有已完成的卡」。
- `workbench-ui/src/styles/{todo,board,verify,notify,session}.css` — 007 面板/卡片/排版/间距；保留 `wb-*` 行为选择器供探针使用。
- `workbench-ui/standalone.html` — head 内联脚本首屏前读 `localStorage.wb-theme` 或系统主题；主题切换时更新 `theme-color`；PWA 注册不变。
- `workbench-app/server.py` — 仅 manifest 的 `theme_color/background_color` 对齐 007；无端点/逻辑改动。
- 计数来源：待办 = TodoPage 的 today+long；看板 = Board 的 running；已完成 = Board 的 done；待你处理 = 现有 inbox/count 轮询。不新增端点。
- 探针选择器：保留 `wb-*` 行为 hook；新增外壳/主题/已完成检查；若改类名同步更新四个探针。

## Tasks & Acceptance

**Execution:**
- [x] `workbench-app/web/theme.css` -- 007 light/dark token + `--color-*` 映射 + canvas body -- 主题基座
- [x] `workbench-ui/src/styles/base.css` -- 外壳 token/布局（侧栏/顶栏/视图/画布框/窄屏）-- 外壳基座
- [x] `workbench-ui/src/App.tsx` -- 侧栏 + 顶栏 + 三 tab + 主题切换 + 计数 + 新建聚焦 -- 外壳
- [x] `workbench-ui/src/done/DonePage.tsx` + `styles/done.css` -- 只读已完成视图/分组/会话入口/空态 -- 已完成 tab
- [x] `workbench-ui/src/todo/TodoPage.tsx` + `todo.css` -- 007 面板/卡片/流水线；onCounts/focusComposer -- 待办视觉
- [x] `workbench-ui/src/board/Board.tsx` + `board.css` -- 007 卡片/列样式；onStats；保留 8 列 -- 看板视觉
- [x] `workbench-ui/src/styles/verify.css` -- 007 验收 overlay/进度/键位视觉 -- 验收视觉
- [x] `workbench-ui/src/styles/notify.css` + `session.css` -- 007 通知/抽屉视觉 -- 通知/会话视觉
- [x] `workbench-ui/standalone.html` + `workbench-app/server.py` -- 首屏主题/theme-color/manifest 颜色 -- 主题一致性
- [x] `workbench-ui/scripts/` -- 外壳/主题/已完成/8 列/选择器回归 -- 探针
- [x] `workbench-ui` -- `npm run build` 分发 -- 双宿主
- [x] `workbench-app/server.py` -- 重启 + `/healthz` -- 不破坏现有

**Acceptance Criteria:**
- Given standalone, when 打开, then 224px 左导航 + 50px 顶栏 + 画布框 + 007 token/排版
- Given 待办, when 打开, then 007 面板/轻卡片/凹槽流水线；三区交互不变
- Given 看板, when 打开, then 8 列名称/顺序不变，007 卡片/列样式；无 5 列旧列
- Given 已完成, when 有点击, then 按完成日分组的只读行；会话按钮可开抽屉；空态正确
- Given 验收/通知/会话, when 打开, then 007 overlay/panel/抽屉视觉；行为与键位不变
- Given 主题切换, when 点击, then 全量 light/dark 切换，刷新后保持，首屏无闪烁
- Given 窄视口 <900px, when 打开, then 侧栏隐藏、顶栏 tabs 可用、无横向溢出
- Given 插件宿主, when 打开, then 只有看板，无侧栏/待办/已完成；token 一致
- Given 改动完成, when build + 现有探针, then 全绿；`/healthz` → `{"ok":true}`

## Implementation Notes

- 主题首屏：`standalone.html` 用内联脚本在样式表前读 `localStorage.wb-theme` / 系统主题；App 只跟随并同步 `theme-color`。
- 外壳：`.wb-root.wb-standalone` 24px 画布 + `.wb-shell` 圆角框；插件宿主 `.wb-root.wb-plugin` 仍是 board-only，不挂侧栏/DonePage/主题按钮。
- 已完成只读视图只在 tab 激活时挂载，5s 轮询 `/api/plugins/kanban/board`，按 `completed_at` 本地日倒序分组；会话入口复用现有抽屉。
- 探针：沿用 `todo/verify/notify/session` 四个，`todo-probe` 新增外壳/主题持久化/已完成分组/8 列名称与插件宿主无侧栏检查，并补窄视口侧栏隐藏/页面不横向溢出、待办面板/轻卡片/凹槽、看板列卡片视觉；`session-probe` 的 inert 选择器改到 `.wb-root > .wb-shell`；`verify-probe` 补 48px 顶栏/396px 右栏，`notification-probe` 补右侧 38px 偏移。
- 验收：四套探针全绿（todo 141 / verify 33 / notify 25 / session 26）；`/healthz` → `{"ok":true}`。对比度矩阵覆盖外壳/已完成文本与验收警示；首屏主题在 bundle 前验证；已完成 error/empty/loading 互斥；窄屏通知不再压顶栏。

## Spec Change Log

## Review Triage Log

| 来源 | 发现 | 裁决 | 路由 | 证据 |
|---|---|---|---|---|
| BH1 | 生成产物未入 review diff，standalone 可能仍服务旧 bundle | false | reject | `git status` 显示 `workbench-app/web/index.{html,js}` 与 `style.css` 已更新；探针对分发后的 standalone 运行并通过；review diff 有意排除压缩产物。 |
| BH2 | 主题色在 `App.tsx`/`standalone.html`/`server.py` 三处硬编码，manifest 暗色安装仍用浅色 | medium | patch | `THEME_COLOR` 常量与内联脚本各一份；`applyTheme` 可改读 computed `--canvas`。首屏前无法读 CSS，内联 bootstrap 常量需保留并注明；manifest 单色限制转 defer。 |
| BH3 | `--wb-faint` 在 10–11.5px 文本上 1.85–3.14:1 | high | patch | WCAG 复算：浅色 `#b0b0a8` 在 canvas 1.85、app 2.11、dark card 3.14；多处元数据/时间/计数使用。 |
| BH4 | 浅色 `--wb-muted` 3.27–3.39:1 | medium | patch | `#8c8c84` 在 app 3.27、white 3.39；tab 标签、done 摘要、通知说明均使用。 |
| BH5 | 激活态 4.01:1，计数叠加 `opacity:.75` 后约 2.7:1 | medium | patch | `#5e6ad2` on `#ececf9` = 4.01；`.wb-nav-btn.on`/`.wb-tab.on` 及计数。 |
| BH6 | 浅色 run/wait/block 文本 2.25–3.76:1 | medium | patch | `#22c55e` 2.28、`#d9a520` 2.25、`#ef4444` 3.76 on white；旧浅色语义色约 4.0–5.4。 |
| BH7 | 通知中心 `<=640px` top 58px 压住 63px 顶栏底边；桌面 right 38 为魔数、gutter 不统一 | medium | patch | `12px` canvas + `1px` shell + `50px` topbar = 63；`notify.css` 58px。right 38 = 24 + 14 顶栏 padding，可保留；gutter 变量化转低优先。 |
| BH8 | DonePage 首请求失败时同时显示错误与空态；`loaded` 在 finally 置 true | high | patch | `DonePage.tsx:69-76` + `121-126`；`err` 非空时仍走 `doneTasks.length === 0` 空态；malformed `columns` 静默为空。 |
| BH9 | DonePage 重复 BoardData/列字符串/轮询/day 逻辑 | low | patch | 类型与 `"done"` 字符串可去掉；5s 轮询是 spec 明确设计（Board 常驻时多一次请求），共享 hook 记 defer。 |
| BH10 | 侧栏 footer 永远显示「工作台运行中」绿点，无连接状态 | medium | patch | `App.tsx:196-198` 固定文案；`refreshInbox` 的 catch 可驱动 `gatewayOk`。 |
| BH11 | `<=900px` 侧栏隐藏后「新建待办」入口消失 | low | defer | composer 仍在待办 tab；顶栏加号属于 007 桌面外壳之外的增强。 |
| BH12 | 主题按钮无 `aria-pressed`/动态标签；系统主题变化不跟随 | low | patch | `aria-label` 固定为「切换明暗」；补状态语义即可；跨 tab storage 监听非必需，注明仅启动时跟系统。 |
| BH13 | 侧栏与顶栏重复三个目的地，无 `aria-current`/tab 语义 | low | patch | 两组按钮均无 `aria-current`/`aria-selected`；加 `aria-current="page"` 与分组名即可。 |
| BH14 | body 过渡被 `.wb-root` 覆盖；reduced-motion 未覆盖可见根 | low | patch | `theme.css` body transition；`.wb-root.wb-standalone` 不透明覆盖；探针读 body 而非可见根。 |
| BH15 | `navCounts` 采集但未断言；新建按钮/侧栏导航点击未测 | medium | patch | `todo-probe.mjs:633` 仅采集；全仓无 `.wb-nav-btn`/`.wb-newbtn` 点击；`focusComposer` 无断言。 |
| BH16 | 「会话缺失由查看器提示」断言恒 true | low | patch | `todo-probe.mjs:1951` `check(..., true)`；未断言 id/title/error 文本。 |
| BH17 | 「无闪烁」只比字符串偏移；未在 bundle 前观察首帧 | medium | patch | `todo-probe.mjs:350-356` 文本顺序；重载断言等在 `.wb-shell` 挂载后。 |
| BH18 | 探针硬编码颜色/尺寸；主题断言读 body；无 manifest 断言 | low | patch | 尺寸是 spec 断言可保留；改读 `.wb-root/.wb-shell`；manifest 测试 VG7 defer。 |
| BH19 | DonePage 日标签非 heading、✓ 无角色、错误无 alert、会话按钮 22px；看板会话按钮 ~17px | low | patch | `DonePage.tsx:108-145`、`done.css:15,34,75-93,111`、`board.css:147-153`。 |
| BH20 | 暗色 `--col #0f1216` 与 `--card #101318` 仅差 1–2 RGB，凹槽层次弱 | low | accept | 正是 007 暗色 token 对；hair 边框与面板头承担层次；改值会偏离视觉基准。 |
| BH21 | 卡片/元数据 9.5–10.5px 偏小 | low | accept | 007 的排版尺度；对比度修复解决可读性主因，无 WCAG 最小字号。 |
| BH22 | spec 与实现矛盾（硬编码/新建入口/矩阵缺错误行） | — | reject | 修复需改本 spec，workflow 明确拒绝；「新建待办」只切 tab 并聚焦既有 composer，不创建卡；错误行由 BH8/VG4/EC1 覆盖。 |
| BH23 | 空态副文案称「验收通过后」；行 hover 非交互；轮询错误无 stale 标识 | low | patch | 副文案改「完成的卡会按完成日期出现在这里」；删行 hover；stale 标识记 defer。 |
| VG1 | 首帧主题只有 HTML 文本顺序检查 | medium | patch | 已预验证；按 VG 方案用 `Network.setBlockedURLs` 阻断 bundle，断言首帧 `data-theme`/body 背景。 |
| VG2 | 侧栏/顶栏计数无断言 | medium | patch | 已预验证；`navCounts` 未断言；把 4/0 运行/0 与列表数对照。 |
| VG3 | 侧栏导航/新建点击未验证 | medium | patch | 已预验证；四个探针无 `.wb-nav-btn`/`.wb-newbtn` 点击。 |
| VG4 | 已完成 kanban 不可达错误态未验证 | high | patch | 已预验证；无 `.wb-done-error` 引用；受控 fetch 失败后断言错误而非空态。 |
| VG5 | 浅色 wait/block 未进对比度矩阵且低于 AA | high | patch | 已预验证；`#a16207`/`#b91c1c` 方案可达 4.5+；扩展 `contrastFor` 选择器。 |
| VG6 | 通知中心纵向偏移未断言；`<=640` 58px 压顶栏 | medium | patch | 已预验证；补 top 84 与 420px 下 top >= 63 检查。 |
| VG7 | manifest 颜色无测试 | low | defer | 已预验证；静态 manifest 只在安装/启动读取，无运行时路径；记 defer。 |
| VG8 | 新建在 TodoPage 首次加载前点击，焦点信号被消费，composer 未挂载 | medium | patch | `TodoPage.tsx:462-468` 的 `!loaded` 分支；focus effect 只依赖 `focusComposer`，加 `loaded/creating` 依赖。 |
| EC1 | DonePage 首请求失败仍显示空态 | high | patch | 与 BH8/VG4 同根；`err` 非空时不得渲染空态。 |
| EC2 | 慢轮询响应乱序覆盖新数据 | low | defer | 新 DonePage 沿用 Board/Todo 的 5s 轮询模式；共享 sequence guard 应一次修三处，记 defer。 |
| EC3 | 新建聚焦竞态 | medium | patch | 与 VG8 同根；补 `loaded/creating`。 |
| EC4 | 探针种子用 `now`/`now-86400`，跨本地午夜会变「昨天/前天」 | low | patch | `todo-probe.mjs:1904-1912`；改用本地中午锚点。 |
| EC5 | 空态未逐字用 007 的「A 收」文案 | low | accept | 007 文案只覆盖验收来源；done 列也含拖拽/自动收，逐字复用会不准确；保留准确文案，副文案按 BH23 修正。 |

**分组：** 对比度 BH3/BH4/BH5/BH6/VG5；通知定位 BH7/VG6；已完成错误态 BH8/VG4/EC1；新建聚焦 VG8/EC3；主题 bootstrap BH2；测试缺口 BH15/BH16/BH17/BH18/VG1/VG2/VG3/EC4；a11y BH12/BH13/BH19；外壳 polish BH10/BH14/BH23；类型/列常量 BH9。以上全部 patch。defer：BH11、EC2、VG7、BH23 stale、BH9 shared hook、BH2 manifest 单色。accept：BH20、BH21、EC5。reject：BH1、BH22。无 intent_gap/bad_spec。

## Design Notes

- 007 的 1320×780 是静态展示框；真实应用用 `calc(100vh - 48px)` 的画布框填充视口。
- 007 的 5 列看板是 PLAN §5.3 明确否决的示例；本故事只取视觉，不取该 IA。
- 007 的「项目」分组与「活动/统计」是静态 mock，当前无数据源/功能；本故事不做，侧栏只保留 3 个真实入口与底部状态。
- 007 底部 capture 与待办页 composer 职责重复；复用 TodoPage composer 并按其样式重做，不新增全局创建入口；不新增 N 快捷键，新建按钮直接切 tab 并聚焦输入。
- 已完成视图按天分组是用户选择，比 007 的平铺列表多一点结构；数据仍只读。
- hover 派发按钮不加，避免与 epics 的 UX 规则冲突；拖拽仍是唯一派发手势。

## Verification

**Commands:**
- `cd workbench-ui && npm run typecheck && npm run build`
- `cd workbench-ui && npm run verify` -- 四套探针全绿
- 手工对照 `sketches/007-dispatch-pipeline/shots/{light,dark,review}.png`：外壳/间距/卡片/token
- `systemctl --user restart jarvis-workbench`；`curl /healthz` → `{"ok":true}`

**Manual checks:**
- 8787：侧栏/顶栏/三区/8 列/已完成/验收/通知/会话 的 007 观感；明暗切换与持久化；窄视口；插件宿主只有看板