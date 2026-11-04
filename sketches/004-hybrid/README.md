## Variant: Hybrid Workbench（004）

### Design stance
Calm Linear 的排版与密度 + Ops Cockpit 的看板面板。一个界面同时服务两种动作：日常录入/推进（今日待办），批量管理/调度（待办仓库）。

### Key choices
- Layout：左侧栏（新建按钮+导航+项目）+ 顶部双 Tab + 居中 760px 列表 / 全宽看板
- Theme：CSS 变量双主题，右上角一键切换，localStorage 记忆，跟随系统初始值
- Tabs：今日待办（分组列表）｜待办仓库（6 列看板 + 统计条）
- Drag & Drop：列表行跨分组拖动改状态；把行拖到「待办仓库」Tab = 归档；看板卡片跨列拖动；把卡片拖到「今日待办」Tab = 加入今天
- New task：侧栏 + 顶栏两个入口按钮，弹窗表单（标题/类型/项目/优先级/备注/AI 补全开关）
- Status：triage → todo → ready → running → blocked → review → done

### Trade-offs
- Strong at：录入选「按钮」或「一句话」都顺手；今日聚焦与仓库全局两种视角切换零成本；深浅色都可用
- Weak at：看板 6 列在窄屏（<1100px）会横向滚动；功能比纯 Linear 多，首次上手需要理解两个 Tab 的语义

### Best for
既是"每天推几条"的个人工作台，也要管"几十条积压"的仓库——本项目的目标形态。