import React, { useCallback, useEffect, useState } from "react";
import Board from "./board/Board";
import TodoPage from "./todo/TodoPage";
import VerifyPage from "./verify/VerifyPage";
import NotificationCenter from "./notify/NotificationCenter";
import SessionViewer from "./session/SessionViewer";
import { getInboxCount, sweepReviews } from "./api/workbench";
import { isPluginHost } from "./platform";
import "./styles/board.css";
import "./styles/todo.css";
import "./styles/verify.css";
import "./styles/notify.css";
import "./styles/session.css";

type TabKey = "todo" | "flow";

// 插件宿主没有 /api/workbench/* 后端；待办/验收依赖它，只在独立版 8787 提供。
const TABS: { key: TabKey; label: string }[] = isPluginHost
  ? [{ key: "flow", label: "看板" }]
  : [
      { key: "todo", label: "待办" },
      { key: "flow", label: "看板" },
    ];

const DEFAULT_TAB: TabKey = isPluginHost ? "flow" : "todo";
const INBOX_POLL_MS = 5000;

export default function App(): React.ReactElement {
  // 独立版待办第一且默认打开；插件宿主只有看板。
  const [tab, setTab] = useState<TabKey>(DEFAULT_TAB);
  const [reviewCount, setReviewCount] = useState(0);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [verifyStartId, setVerifyStartId] = useState<string | null>(null);
  // 会话抽屉独立于验收覆盖层：验收页和看板完成卡共用同一份状态。
  const [sessionTarget, setSessionTarget] = useState<{
    id: string;
    title?: string;
  } | null>(null);

  const refreshInbox = useCallback(async () => {
    if (isPluginHost) return;
    try {
      const inbox = await getInboxCount();
      setReviewCount(inbox.count);
    } catch {
      // 保留上一次计数：网络抖动不该把入口清零。
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

  return (
    <div className="wb-root">
      <div className="wb-head">
        <div className="wb-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={"wb-tab" + (tab === t.key ? " on" : "")}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {!isPluginHost ? (
          <button
            className={"wb-review-entry" + (reviewCount > 0 ? " active" : "")}
            type="button"
            onClick={() => openVerify()}
            title="进入全屏验收"
          >
            验收
            <span className="wb-review-entry-count">{reviewCount}</span>
          </button>
        ) : null}
        {isPluginHost ? (
          <span className="wb-notice">插件宿主只读；请在 127.0.0.1:8787 独立版派发</span>
        ) : null}
      </div>

      {/* 两个面板都保持挂载，切换 tab 不丢草稿/验收编辑状态。 */}
      {!isPluginHost ? (
        <div className={"wb-pane" + (tab === "todo" ? "" : " wb-pane-hidden")}>
          <TodoPage onVerify={openVerify} />
        </div>
      ) : null}
      <div className={"wb-pane" + (tab === "flow" ? "" : " wb-pane-hidden")}>
        <Board onOpenSession={isPluginHost ? undefined : openSession} />
      </div>

      {/* 通知中心只在独立版挂载（插件宿主没有 /api/workbench/*，也不轮询）。 */}
      {!isPluginHost ? (
        <NotificationCenter
          onOpenBoard={openBoard}
          onOpenVerify={openVerify}
          onChanged={refreshInbox}
        />
      ) : null}

      {/* 验收是全屏覆盖层，不是 tab；插件宿主的 TABS 里没有入口。 */}
      {!isPluginHost && verifyOpen ? (
        <VerifyPage
          initialTaskId={verifyStartId}
          onClose={closeVerify}
          onChanged={refreshInbox}
          onOpenSession={openSession}
          sessionOpen={sessionTarget !== null}
        />
      ) : null}

      {/* 只读会话抽屉：插件宿主没有 /api/workbench/*，入口与挂载都不存在。 */}
      {!isPluginHost && sessionTarget ? (
        <SessionViewer
          taskId={sessionTarget.id}
          taskTitle={sessionTarget.title}
          onClose={closeSession}
        />
      ) : null}
    </div>
  );
}