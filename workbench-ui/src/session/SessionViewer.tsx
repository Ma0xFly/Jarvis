import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  getSession,
  WorkbenchError,
  type SessionMessage,
  type SessionTranscript,
} from "../api/workbench";

/**
 * 会话查看器（2.3）：应用内只读抽屉。
 *
 * 数据来自 GET /api/workbench/session/{task_id}：后端只读 ~/.hermes/state.db，
 * 返回最新 run 的 worker session（或 tasks.session_id 回退）最近 200 条消息。
 * 不跳 9119、不发消息、不续跑；executor_session_id 只展示（续跑属 Epic 3）。
 */

interface SessionViewerProps {
  taskId: string;
  /** 看板完成卡/验收页带来的标题，加载前先给个上下文。 */
  taskTitle?: string;
  onClose: () => void;
}

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatClock(ts?: number | null): string {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 秒级时长；结束为空时按“现在”算，配合“进行中”展示。 */
function formatDuration(start?: number | null, end?: number | null): string {
  if (!start) return "—";
  const seconds = Math.max(0, Math.round((end ?? Date.now() / 1000) - start));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days > 0) return `${days} 天 ${hours % 24} 小时`;
  return `${hours} 小时 ${minutes % 60} 分`;
}

function roleLabel(role: string): string {
  if (role === "user") return "用户";
  if (role === "assistant") return "助手";
  if (role === "tool") return "工具";
  return role || "消息";
}

function sessionErrorText(error: unknown): { code: string; message: string } {
  if (error instanceof WorkbenchError) {
    switch (error.code) {
      case "no_session":
        return {
          code: error.code,
          message:
            "这张卡没有执行会话：run metadata 没有 worker_session_id，任务也没有 session_id。",
        };
      case "session_missing":
        return {
          code: error.code,
          message: "会话在 state.db 中不存在，可能已被清理。",
        };
      case "session_unavailable":
        return {
          code: error.code,
          message: "会话库暂时不可读（state.db 不可用），不影响验收。",
        };
      case "not_found":
        return { code: error.code, message: "任务不存在，无法读取会话。" };
      case "upstream_unavailable":
        return {
          code: error.code,
          message: "上游 kanban 暂时不可读，请稍后重试。",
        };
      default:
        return { code: error.code, message: "会话读取失败：" + error.message };
    }
  }
  return { code: "session_unavailable", message: "会话读取失败：" + errText(error) };
}

function renderRole(role: string): string {
  return role.replace(/[^a-z0-9_-]/g, "") || "message";
}

export default function SessionViewer({
  taskId,
  taskTitle,
  onClose,
}: SessionViewerProps): React.ReactElement {
  const [data, setData] = useState<SessionTranscript | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const drawerRef = useRef<HTMLElement | null>(null);
  const scrimRef = useRef<HTMLDivElement | null>(null);
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  // Truly modal (one effect so cleanup order is deterministic): remember the
  // opener, focus the drawer, lock body scroll, make every sibling of the
  // scrim inert / aria-hidden; on close restore the background first, then
  // focus the opener (focusing an inert element would be ignored).
  useEffect(() => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();

    const scrim = scrimRef.current;
    const parent = scrim?.parentElement;
    const siblings = parent
      ? (Array.from(parent.children) as HTMLElement[]).filter((node) => node !== scrim)
      : [];
    const previous = siblings.map((node) => ({
      node,
      inert: node.inert,
      ariaHidden: node.getAttribute("aria-hidden"),
    }));
    for (const entry of previous) {
      entry.node.inert = true;
      entry.node.setAttribute("aria-hidden", "true");
    }
    const bodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      for (const entry of previous) {
        entry.node.inert = entry.inert;
        if (entry.ariaHidden === null) entry.node.removeAttribute("aria-hidden");
        else entry.node.setAttribute("aria-hidden", entry.ariaHidden);
      }
      document.body.style.overflow = bodyOverflow;
      const opener = returnFocusRef.current;
      if (opener && document.contains(opener) && typeof opener.focus === "function") {
        opener.focus();
      }
    };
  }, []);

  // The transcript is chronological (oldest -> newest): open at the newest.
  useEffect(() => {
    if (!data) return;
    const node = messagesRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [data]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setData(null);
    setError(null);
    void getSession(taskId)
      .then((transcript) => {
        if (!alive) return;
        setData(transcript);
      })
      .catch((cause: unknown) => {
        if (!alive) return;
        setError(sessionErrorText(cause));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [taskId, reloadKey]);

  const retry = useCallback(() => setReloadKey((value) => value + 1), []);

  // Capture-phase Esc so the acceptance overlay underneath does not also
  // close; Tab is trapped inside the drawer. The acceptance page additionally
  // ignores keys while this drawer is mounted (see VerifyPage `sessionOpen`).
  useEffect(() => {
    const focusables = (): HTMLElement[] => {
      const drawer = drawerRef.current;
      if (!drawer) return [];
      return Array.from(
        drawer.querySelectorAll<HTMLElement>(
          'button:not([disabled]), summary, a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((node) => node.offsetParent !== null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = focusables();
      if (nodes.length === 0) {
        event.preventDefault();
        return;
      }
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      if (!event.shiftKey && (active === last || !drawerRef.current?.contains(active))) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && (active === first || !drawerRef.current?.contains(active))) {
        event.preventDefault();
        last.focus();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const canRetry =
    error?.code === "session_missing" ||
    error?.code === "session_unavailable" ||
    error?.code === "upstream_unavailable";
  const session = data?.session ?? null;
  const heading = taskTitle || session?.title || taskId;
  const running = Boolean(session && !session.ended_at);
  const origin =
    data?.source === "task"
      ? "发起会话（tasks.session_id 回退，非 worker 轮次）"
      : `worker 会话 · 最新一轮${data?.run_id != null ? ` run #${data.run_id}` : ""}`;

  return (
    <div
      className="wb-session-scrim"
      ref={scrimRef}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        className="wb-session-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={"执行会话：" + heading}
        ref={drawerRef}
      >
        <header className="wb-session-head">
          <div className="wb-session-head-text">
            <div className="wb-session-kicker">
              执行会话
              {session ? (
                <span className={"wb-session-status" + (running ? " running" : "")}>
                  {running ? "进行中" : "已结束"}
                </span>
              ) : null}
            </div>
            <h2 className="wb-session-title">{heading}</h2>
            <div className="wb-session-sub">{taskId}</div>
          </div>
          <button
            className="wb-btn wb-session-close"
            type="button"
            ref={closeRef}
            onClick={onClose}
          >
            关闭 <kbd>Esc</kbd>
          </button>
        </header>

        {loading ? (
          <div className="wb-session-state" role="status">
            读取会话…
          </div>
        ) : error ? (
          <div className="wb-session-state wb-session-error" role="alert">
            <div>{error.message}</div>
            <div className="wb-session-error-code">{error.code}</div>
            {canRetry ? (
              <button className="wb-btn" type="button" onClick={retry}>
                重试
              </button>
            ) : null}
          </div>
        ) : data && session ? (
          <>
            <section className="wb-session-meta-wrap">
              <div className="wb-session-origin">{origin}</div>
              <dl className="wb-session-meta">
                <div>
                  <dt>会话</dt>
                  <dd className="wb-session-id">{session.id}</dd>
                </div>
                <div>
                  <dt>标题</dt>
                  <dd>{session.title || "—"}</dd>
                </div>
                <div>
                  <dt>来源</dt>
                  <dd>
                    {[session.source, session.profile_name].filter(Boolean).join(" · ") || "—"}
                  </dd>
                </div>
                <div>
                  <dt>模型</dt>
                  <dd>{session.model || "—"}</dd>
                </div>
                <div>
                  <dt>开始</dt>
                  <dd>{formatClock(session.started_at) || "—"}</dd>
                </div>
                <div>
                  <dt>结束</dt>
                  <dd>{session.ended_at ? formatClock(session.ended_at) : "进行中"}</dd>
                </div>
                <div>
                  <dt>时长</dt>
                  <dd>
                    {formatDuration(session.started_at, session.ended_at)}
                    {running ? "（进行中）" : ""}
                  </dd>
                </div>
                <div>
                  <dt>记录消息</dt>
                  <dd>{session.message_count}</dd>
                </div>
                <div>
                  <dt>Tokens</dt>
                  <dd>
                    {session.input_tokens} 入 / {session.output_tokens} 出
                  </dd>
                </div>
              </dl>
              {data.executor_session_id ? (
                <div className="wb-session-executor">
                  执行器会话 <code>{data.executor_session_id}</code>
                  <span>续跑属 Epic 3，本页只展示，不启动也不写回。</span>
                </div>
              ) : null}
            </section>

            {data.truncated ? (
              <div className="wb-session-truncated" role="status">
                已截断（truncated）：只加载过滤后最近的 {data.returned_count} 条。
              </div>
            ) : null}

            <div className="wb-session-messages-wrap" ref={messagesRef}>
              <div className="wb-session-count" role="status">
                显示 {data.returned_count} / 共 {data.total_count} 条消息
                {data.total_count !== session.message_count
                  ? `（会话记录 ${session.message_count}）`
                  : ""}
              </div>
              {data.messages.length === 0 ? (
                <div className="wb-session-state">该会话还没有消息。</div>
              ) : (
                <ol className="wb-session-messages">
                  {data.messages.map((message: SessionMessage, index) => (
                    <li
                      className={"wb-session-msg wb-session-msg-" + renderRole(message.role)}
                      key={message.id || "m" + index}
                    >
                      <div className="wb-session-msg-head">
                        <span className="wb-session-role">{roleLabel(message.role)}</span>
                        {message.compressed ? (
                          <span className="wb-session-chip">压缩摘要</span>
                        ) : null}
                        {message.tool_name ? (
                          <span className="wb-session-tool-name">{message.tool_name}</span>
                        ) : null}
                        <time>{formatClock(message.timestamp)}</time>
                      </div>
                      {message.content ? (
                        <pre className="wb-session-content">{message.content}</pre>
                      ) : null}
                      {message.content_truncated ? (
                        <div className="wb-session-msg-size">
                          内容已截断：原 {message.content_chars} 字符
                        </div>
                      ) : null}
                      {message.tool_calls.length > 0 ? (
                        <details className="wb-session-tools">
                          <summary>
                            工具调用 {message.tool_calls.length}
                            {message.tool_calls.some((call) => call.name)
                              ? "：" +
                                message.tool_calls
                                  .map((call) => call.name || "未命名")
                                  .join("、")
                              : ""}
                          </summary>
                          <ul>
                            {message.tool_calls.map((call, callIndex) => (
                              <li key={callIndex}>
                                {call.name ? (
                                  <span className="wb-session-tool-name">{call.name}</span>
                                ) : null}
                                <span className="wb-session-tool-summary">
                                  {call.summary || "参数已折叠"}
                                </span>
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : null}
                    </li>
                  ))}
                </ol>
              )}
            </div>

            <footer className="wb-session-foot">
              只读查看：不写 state.db、不提供发消息或续跑。
            </footer>
          </>
        ) : null}
      </aside>
    </div>
  );
}