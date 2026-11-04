## Variant: Calm Linear

### Design stance
把工作台做成一张"安静的工作桌"：默认只显示今天要推进的事，其余一切收进渐进披露。参照 Linear / Things 的 calm design。

### Key choices
- Layout：左侧栏（导航+项目）+ 居中 760px 阅读列；列表为主视图，看板是切换项
- Typography：系统字体 13-14px，标题 15px；靠字重与灰阶分层，不靠图标
- Color：`#fbfbfa` 底 + `#5e6ad2` 单一强调色，状态点承担语义色
- Interaction：顶部速记框回车入队；行点击开右侧抽屉；列表/看板分段切换

### Trade-offs
- Strong at：日常录入极快、扫读成本低、长时间看不累
- Weak at：多任务并发时（6 条以上）全局态势感知弱，必须切到看板

### Best for
以"人主导、每天推几条"为主的用法；把 Agent 当同事而不是流水线。