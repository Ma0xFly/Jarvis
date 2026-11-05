import React, { useCallback, useEffect, useState } from "react";
import Board from "./board/Board";
import TodoPage from "./todo/TodoPage";
import DonePage from "./done/DonePage";
import VerifyPage from "./verify/VerifyPage";
import NotificationCenter from "./notify/NotificationCenter";
import SessionViewer from "./session/SessionViewer";
import { getInboxCount, sweepReviews } from "./api/workbench";
import { isPluginHost } from "./platform";
import "./styles/board.css";
import "./styles/todo.css";
import "./styles/done.css";
import "./styles/verify.css";
import "./styles/notify.css";
import "./styles/session.css";

type TabKey = "todo" | "flow" | "done";
type Theme = "light" | "dark";

const THEME_KEY = "wb-theme";
const INBOX_POLL_MS = 5000;

// 插件宿主没有 /api/workbench/* 后端；待办/验收依赖它，只在独立版 8787 提供。
const PLUGIN_TABS: { key: TabKey; label: string }[] = [{ key: "flow", label: "看板" }];
const STANDALONE_TABS: { key: TabKey; label: string }[] = [
  { key: "todo", label: "待办" },
  { key: "flow", label: "看板" },
  { key: "done", label: "已完成" },
];

/** 首屏主题由 standalone.html 的内联脚本先行写入 data-theme，这里只跟随。 */
function readInitialTheme(): Theme {
  const current = document.documentElement.getAttribute("data-theme");
  if (current === "light" || current === "dark") return current;
  try {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // localStorage 不可用时跟随系统。
  }
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute("data-theme", theme);
  // theme-color 直接取 theme.css 的 --canvas：token 是唯一色源。
  const canvas = getComputedStyle(root).getPropertyValue("--canvas").trim();
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta && canvas) meta.setAttribute("content", canvas);
}

export default function App(): React.ReactElement {
  const [tab, setTab] = useState<TabKey>(isPluginHost ? "flow" : "todo");
  const [theme, setTheme] = useState<Theme>(() => (isPluginHost ? "light" : readInitialTheme()));
  const [reviewCount, setReviewCount] = useState(0);
  const [gatewayOk, setGatewayOk] = useState(true);
  const [notifyOpen, setNotifyOpen] = useState(false);
  const [notifyCounts, setNotifyCounts] = useState({ p0: 0, p1: 0, p2: 0, total: 0 });
  const [todoCounts, setTodoCounts] = useState({ today: 0, long: 0 });
  const [boardStats, setBoardStats] = useState({ running: 0, done: 0 });
  const [composerFocus, setComposerFocus] = useState(0);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [verifyStartId, setVerifyStartId] = useState<string | null>(null);
  // 会话抽屉独立于验收覆盖层：验收页和看板完成卡共用同一份状态。
  const [sessionTarget, setSessionTarget] = useState<{
    id: string;
    title?: string;
  } | null>(null);

  useEffect(() => {
    if (isPluginHost) return;
    applyTheme(theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((previous) => {
      const next: Theme = previous === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(THEME_KEY, next);
      } catch {
        // 隐私模式下写不进去也不影响本次切换。
      }
      return next;
    });
  }, []);

  const refreshInbox = useCallback(async () => {
    if (isPluginHost) return;
    try {
      const inbox = await getInboxCount();
      setReviewCount(inbox.count);
      setGatewayOk(true);
    } catch {
      // 保留上一次计数：网络抖动不该把入口清零；状态点标红。
      setGatewayOk(false);
    }
  }, []);

  useEffect(() => {
    if (isPluginHost) return;
    // 显式触发一次 48h 自动收（服务端仍有 5 分钟后台 sweeper）；
    // 轮询本身只读，不在 count 请求里写卡。
    void sweepReviews()
      .catch(() => undefined)
      .then(() => refreshInbox());
    const timer = window.setInterval(() => void refreshInbox(), INBOX_POLL_MS);
    return () => window.clearInterval(timer);
  }, [refreshInbox]);

  const openVerify = useCallback((taskId?: string) => {
    if (isPluginHost) return;
    setVerifyStartId(taskId ?? null);
    setVerifyOpen(true);
  }, []);

  const closeVerify = useCallback(() => {
    setVerifyOpen(false);
    setVerifyStartId(null);
    void refreshInbox();
  }, [refreshInbox]);

  const openBoard = useCallback(() => setTab("flow"), []);

  const openSession = useCallback((taskId: string, title?: string) => {
    if (isPluginHost || !taskId.trim()) return;
    setSessionTarget({ id: taskId, title });
  }, []);

  const closeSession = useCallback(() => setSessionTarget(null), []);

  const handleTodoCounts = useCallback((counts: { today: number; long: number }) => {
    setTodoCounts(counts);
  }, []);

  const handleBoardStats = useCallback((stats: { running: number; done: number }) => {
    setBoardStats(stats);
  }, []);

  const handleNotifyCounts = useCallback(
    (counts: { p0: number; p1: number; p2: number; total: number }) => {
      setNotifyCounts(counts);
    },
    [],
  );

  /** 新建不是全局入口：切到待办并聚焦 composer，不新增 N 快捷键。 */
  const newTodo = useCallback(() => {
    setTab("todo");
    setComposerFocus((value) => value + 1);
  }, []);

  const todoTotal = todoCounts.today + todoCounts.long;
  const counts: Record<TabKey, string> = {
    todo: String(todoTotal),
    flow: `${boardStats.running} 运行`,
    done: String(boardStats.done),
  };

  if (isPluginHost) {
    return (
      <div className="wb-root wb-plugin">
        <div className="wb-head">
          <div className="wb-tabs">
            {PLUGIN_TABS.map((entry) => (
              <button
                key={entry.key}
                className={"wb-tab" + (tab === entry.key ? " on" : "")}
                type="button"
                aria-current={tab === entry.key ? "page" : undefined}
                onClick={() => setTab(entry.key)}
              >
                <span className="wb-tab-label">{entry.label}</span>
              </button>
            ))}
          </div>
          <span className="wb-notice">插件宿主只读；请在 127.0.0.1:8787 独立版派发</span>
        </div>
        <div className="wb-pane">
          <Board />
        </div>
      </div>
    );
  }

  return (
    <div className="wb-root wb-standalone">
      <div className="wb-shell">
        <aside className="wb-sidebar">
          <div className="wb-brand">
            <span className="wb-brand-dot" aria-hidden="true" />
            Jarvis 工作台
          </div>
          <button className="wb-newbtn" type="button" onClick={newTodo}>
            ＋ 新建待办
          </button>
          <nav className="wb-nav" aria-label="侧栏视图">
            {STANDALONE_TABS.map((entry) => (
              <button
                key={entry.key}
                className={"wb-nav-btn" + (tab === entry.key ? " on" : "")}
                type="button"
                aria-current={tab === entry.key ? "page" : undefined}
                onClick={() => setTab(entry.key)}
              >
                <span>{entry.label}</span>
                <span className="wb-nav-count">{counts[entry.key]}</span>
              </button>
            ))}
          </nav>
          <div className={"wb-sidebar-foot" + (gatewayOk ? "" : " down")}>
            <span className="wb-pulse" aria-hidden="true" />
            {gatewayOk ? "工作台运行中" : "8787 不可达"}
          </div>
        </aside>

        <div className="wb-main">
          <header className="wb-topbar">
            <div className="wb-tabs" role="group" aria-label="顶栏视图">
              {STANDALONE_TABS.map((entry) => (
                <button
                  key={entry.key}
                  className={"wb-tab" + (tab === entry.key ? " on" : "")}
                  type="button"
                  aria-current={tab === entry.key ? "page" : undefined}
                  onClick={() => setTab(entry.key)}
                >
                  <span className="wb-tab-label">{entry.label}</span>
                  <span className="wb-tab-count">{counts[entry.key]}</span>
                </button>
              ))}
            </div>
            <div className="wb-actions">
              <button
                className={"wb-iconbtn wb-notify-toggle" + (notifyCounts.total > 0 ? " active" : "")}
                type="button"
                onClick={() => setNotifyOpen((value) => !value)}
                title={notifyOpen ? "收起通知中心" : "打开通知中心"}
                aria-label={notifyOpen ? "收起通知中心" : "打开通知中心"}
                aria-expanded={notifyOpen}
              >
                <span aria-hidden="true">🔔</span>
                {notifyCounts.total > 0 ? (
                  <span className="wb-notify-badge">{notifyCounts.total}</span>
                ) : null}
              </button>
              <button
                className={"wb-review-entry" + (reviewCount > 0 ? " active" : "")}
                type="button"
                onClick={() => openVerify()}
                title="打开全屏验收（收完才进已完成）"
              >
                <span className="wb-review-ring" aria-hidden="true" />
                验收
                <span className="wb-review-entry-count">{reviewCount}</span>
              </button>
              <button
                className="wb-iconbtn wb-theme-toggle"
                type="button"
                onClick={toggleTheme}
                title={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
                aria-label={theme === "dark" ? "切换到浅色主题" : "切换到深色主题"}
                aria-pressed={theme === "dark"}
              >
                {theme === "dark" ? "☀" : "☾"}
              </button>
            </div>
          </header>

          <div className="wb-views">
            {/* 两个面板都保持挂载，切换 tab 不丢草稿/验收编辑状态。 */}
            <div className={"wb-pane" + (tab === "todo" ? "" : " wb-pane-hidden")}>
              <TodoPage
                onVerify={openVerify}
                onOpenSession={openSession}
                onCounts={handleTodoCounts}
                focusComposer={composerFocus}
                active={tab === "todo"}
              />
            </div>
            <div className={"wb-pane" + (tab === "flow" ? "" : " wb-pane-hidden")}>
              <Board onOpenSession={openSession} onStats={handleBoardStats} />
            </div>
            {tab === "done" ? (
              <div className="wb-pane">
                <DonePage onOpenSession={openSession} />
              </div>
            ) : null}
          </div>
        </div>
      </div>

      {/* 通知中心只在独立版挂载（插件宿主没有 /api/workbench/*，也不轮询）。 */}
      <NotificationCenter
        onOpenBoard={openBoard}
        onOpenVerify={openVerify}
        onChanged={refreshInbox}
        open={notifyOpen}
        onOpenChange={setNotifyOpen}
        onCounts={handleNotifyCounts}
      />

      {/* 验收是全屏覆盖层，不是 tab；插件宿主的 TABS 里没有入口。 */}
      {verifyOpen ? (
        <VerifyPage
          initialTaskId={verifyStartId}
          manualOpen={verifyStartId === null}
          onClose={closeVerify}
          onChanged={refreshInbox}
          onOpenSession={openSession}
          sessionOpen={sessionTarget !== null}
        />
      ) : null}

      {/* 只读会话抽屉：插件宿主没有 /api/workbench/*，入口与挂载都不存在。 */}
      {sessionTarget ? (
        <SessionViewer
          taskId={sessionTarget.id}
          taskTitle={sessionTarget.title}
          onClose={closeSession}
        />
      ) : null}
    </div>
  );
}