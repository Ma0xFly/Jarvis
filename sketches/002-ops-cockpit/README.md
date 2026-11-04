## Variant: Ops Cockpit

### Design stance
把工作台做成一台"飞行仪表盘"：一屏看全 6 个状态列的吞吐、运行进度和实时事件流，信息密度优先。参照 Linear dark + Grafana/Command Center。

### Key choices
- Layout：顶栏遥测 + 6 列紧凑看板 + 右侧活动流 + 底部命令栏
- Typography：等宽字体，10-12px，全大写列头，数字用 tabular-nums
- Color：`#0b0d10` 底；琥珀为主强调，绿/红/紫区分 running/blocked/review
- Interaction：运行卡有进度条动画；底部 `/` 输入入队；活动流自动补最新事件

### Trade-offs
- Strong at：并发任务多时一眼看清"谁在跑、卡在哪、发生了什么"；适合常驻副屏
- Weak at：录入体验一般（底部命令栏偏工具化）；深色长时间阅读疲劳；移动端不友好

### Best for
"机器主导、人只批奏折"的用法；任务多、需要盯运行状态和失败重试。