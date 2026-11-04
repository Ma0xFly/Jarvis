# Jarvis 工作台（独立版）

把原来只能在 Hermes dashboard 里打开的 `workbench` 插件，拆成一个**独立本地应用**：
自己的进程、自己的端口、自己的窗口，但任务数据仍然读写同一份 `kanban.db`。

```
浏览器 / Chrome app 窗口
        │  http://127.0.0.1:8787
        ▼
workbench-app/server.py  (FastAPI)
        │  挂载 Hermes kanban 插件的同一套 REST router
        ▼
~/.hermes/kanban.db  ←── CLI / gateway / dashboard 共用
```

## 为什么不是复制一套数据

`server.py` 不自己写 SQL，而是用 `importlib` 加载 Hermes 自带的
`plugins/kanban/dashboard/plugin_api.py`，把它原样挂到
`/api/plugins/kanban/*`。所以：

- 看板、任务、评论、完成历史全部来自 `kanban.db`
- CLI、gateway 调度器、Hermes dashboard、这个独立 app 看到的是同一份状态
- 不新增数据库，不需要同步

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

## 界面

- **待办**：今日（左）/ 仓库（右），互拖、排序、搜索、新建
- **看板**：8 列 `待整理 / 待执行 / 已排期 / 就绪 / 运行中 / 待拍板 / 待验收 / 已完成`
- **已完成**：按今天/昨天/更早分组，可恢复
- 明暗主题：右上角 ☾/☀，记住在 `localStorage["wb.theme"]`

「今日」和仓库排序仍然只存浏览器（`wb.today.v1` / `wb.repoOrder.v1`），
和 dashboard 插件行为一致。

## 文件

| 文件 | 作用 |
|---|---|
| `server.py` | FastAPI：挂 kanban router + 静态页 + `/healthz` |
| `web/app.js` | 工作台 UI（从 dashboard 插件同步） |
| `web/style.css` | 工作台样式（从 dashboard 插件同步） |
| `web/theme.css` | 独立版的明暗主题 token 和外壳 |
| `web/sdk-shim.js` | 把 dashboard 的 `SDK` 接口映射到普通 `fetch` |
| `web/index.html` / `boot.js` | 页面骨架 + 主题/连接状态 |
| `install.sh` | 用户级 systemd 服务 + 桌面入口 |
| `sync-ui.sh` | 从 dashboard 插件重新同步 UI 文件 |

## 同步 UI

在 Hermes dashboard 插件里改完样式/交互后：

```bash
./sync-ui.sh
systemctl --user restart jarvis-workbench
```

`sync-ui.sh` 只复制 `index.js` / `style.css`，不会碰 `server.py` 和主题。

## 已知限制

- 固定当前 board，不切换多 board
- 没有任务详情抽屉（看详情仍可用 dashboard 的 Kanban 页）
- 5 秒轮询，不是 WebSocket
- 「今日」/排序不跨浏览器
- 无登录，仅限本机 / Tailscale

## 卸载

```bash
systemctl --user disable --now jarvis-workbench
rm -f ~/.config/systemd/user/jarvis-workbench.service
rm -f ~/.local/share/applications/jarvis-workbench.desktop
rm -rf ~/Projects/Jarvis/workbench-app
```