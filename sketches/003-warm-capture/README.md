## Variant: Warm Capture

### Design stance
把工作台做成"每天早上的第一句话"：一个大输入框，写下就想，AI 负责拆解；任务以卡片呈现，亲切、低压力。参照 Things 3 / TickTick 的暖色待办。

### Key choices
- Layout：单列居中，问候 → 捕获卡 → AI 早报 → 今天列表 → 看板 tab
- Typography：系统字体 13-16px，输入框 16px，标题 20px
- Color：`#faf7f2` 奶油底 + `#d9652b` 铁锈橙；项目色条区分归属
- Interaction：回车入队 + "AI 拆解并加入"双路径；Tab 切换今天/看板/全部；勾选完成

### Trade-offs
- Strong at：录入摩擦最低（一句话+回车）；情绪友好，愿意每天打开；手机上也舒服
- Weak at：信息密度最低；超过 20 条任务后列表变长，需要靠 tab 分流

### Best for
个人生活+工作混合管理；重视"愿意天天用"胜过"功能全"。