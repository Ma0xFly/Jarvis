import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  getNotifications,
  MAX_REJECT_REASON_CHARS,
  notificationAction,
  sweepNotifications,
  WorkbenchError,
  type NotificationActionName,
  type NotificationBundle,
  type NotificationItem,
  type NotificationSnapshot,
} from "../api/workbench";

/**
 * 右上角通知中心（Story 2.2，只在独立版 8787 挂载）。
 *
 * P0 置顶常驻，P1 面板，P2 每日一行；review 卡显示「还有 Xh Ym 自动收」，
 * 验收槽固定 3 束（同 parent 的兄弟卡成束），超出的束进排队。
 * 动作复用 2.1 verify：accept/reject 直接点卡片，reject 理由必填；
 * remind 只对 P0/P1 开放，10 分钟后下一轮再通知。
 */

const POLL_MS = 5000;
const TICK_MS = 30_000;

interface NotificationCenterProps {
  /** 非 review 通知的「打开看板」入口。 */
  onOpenBoard: () => void;
  /** review 通知的「去验收」入口，直接打开该卡的全屏验收。 */
  onOpenVerify: (taskId: string) => void;
  /** 动作完成后通知 App 刷新验收计数。 */
  onChanged?: () => void;
  /** 面板是否展开；收起时仍轮询，只把计数交给顶栏铃铛。 */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** 顶栏铃铛徽标：P0/P1/P2 计数；无快照时为 0。 */
  onCounts?: (counts: { p0: number; p1: number; p2: number; total: number }) => void;
}

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** review 倒计时：快照里的剩余秒数 - 本地经过时间。 */
function remainingSeconds(
  item: NotificationItem,
  snapshot: NotificationSnapshot,
  nowMs: number,
): number | null {
  if (item.auto_collect_in_seconds == null) return null;
  const elapsed = Math.max(0, nowMs / 1000 - snapshot.generated_at);
  return Math.max(0, item.auto_collect_in_seconds - elapsed);
}

function countdownText(
  item: NotificationItem,
  snapshot: NotificationSnapshot,
  nowMs: number,
): string {
  if (item.kind !== "review") return "";
  if (item.review_requested_at == null && item.auto_collect_in_seconds == null) {
    return "无 review 记录，不会自动收";
  }
  const remain = remainingSeconds(item, snapshot, nowMs);
  if (remain == null) return "自动收时限未知";
  if (remain <= 0) return "已到自动收时限";
  const hours = Math.floor(remain / 3600);
  const minutes = Math.floor((remain % 3600) / 60);
  if (hours > 0) return `还有 ${hours} 小时 ${minutes} 分自动收`;
  return `还有 ${Math.max(1, minutes)} 分自动收`;
}

function bundleLabel(bundle: NotificationBundle): string {
  if (bundle.count > 1) {
    return `${bundle.count} 张兄弟卡（同 parent）`;
  }
  return bundle.cards[0]?.title || bundle.task_ids[0] || bundle.key;
}

export default function NotificationCenter({
  onOpenBoard,
  onOpenVerify,
  onChanged,
  open = true,
  onOpenChange,
  onCounts,
}: NotificationCenterProps): React.ReactElement | null {
  const [snapshot, setSnapshot] = useState<NotificationSnapshot | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejectFor, setRejectFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [actionError, setActionError] = useState("");
  const [nowMs, setNowMs] = useState(() => Date.now());
  const inFlightRef = useRef(false);

  const refresh = useCallback(
    async (collect = false): Promise<NotificationSnapshot | null> => {
      if (inFlightRef.current) return null;
      inFlightRef.current = true;
      let sweepError = "";
      try {
        if (collect) {
          try {
            // 显式采集一轮；后台 poller 可能已关闭，手动刷新仍要能拉到新事件。
            await sweepNotifications();
          } catch (err) {
            sweepError = "采集失败：" + errText(err);
          }
        }
        const data = await getNotifications();
        setSnapshot(data);
        setError(sweepError);
        return data;
      } catch (err) {
        // Keep the last good snapshot, but surface the stale poll.
        setError("通知刷新失败：" + errText(err));
        return null;
      } finally {
        inFlightRef.current = false;
      }
    },
    [],
  );

  useEffect(() => {
    // 首次进入显式采集一轮；之后每 5s 只读拉快照（不堆叠请求）。
    void refresh(true);
    const timer = window.setInterval(() => void refresh(false), POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  const runAction = useCallback(
    async (item: NotificationItem, action: NotificationActionName, reasonText?: string) => {
      if (busyId) return;
      setBusyId(item.id);
      setActionError("");
      setNotice("");
      try {
        await notificationAction(
          item.id,
          action,
          reasonText,
          item.review_requested_at,
        );
        if (action === "remind") {
          setNotice("已安排 10 分钟后再提醒");
        } else if (action === "accept") {
          setNotice("已收下");
        } else {
          setNotice("已打回 triage");
        }
        setRejectFor(null);
        setReason("");
        await refresh();
        onChanged?.();
      } catch (err) {
        const code = err instanceof WorkbenchError ? err.code : "";
        if (code === "reason_required") {
          setRejectFor(item.id);
          setActionError("不收必须写理由");
        } else if (code === "reason_too_long") {
          setRejectFor(item.id);
          setActionError(`理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`);
        } else if (code === "not_in_review") {
          setActionError("这张卡已不在验收队列，已移除");
          await refresh();
          onChanged?.();
        } else if (code === "not_found") {
          setActionError("这条通知已失效，已刷新");
          await refresh(true);
          onChanged?.();
        } else if (code === "stale_review") {
          setActionError("这张卡已进入新一轮验收，已刷新");
          await refresh(true);
          onChanged?.();
        } else {
          setActionError("通知动作失败：" + errText(err));
        }
      } finally {
        setBusyId(null);
      }
    },
    [busyId, onChanged, refresh],
  );

  const openReject = useCallback((item: NotificationItem) => {
    setRejectFor(item.id);
    setReason("");
    setActionError("");
  }, []);

  const submitReject = useCallback(
    (item: NotificationItem) => {
      if (!reason.trim()) {
        setActionError("不收必须写理由");
        return;
      }
      if (reason.trim().length > MAX_REJECT_REASON_CHARS) {
        setActionError(`理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`);
        return;
      }
      void runAction(item, "reject", reason);
    },
    [reason, runAction],
  );

  const notifyCounts = snapshot?.counts ?? { p0: 0, p1: 0, p2: 0, total: 0 };
  useEffect(() => {
    onCounts?.(notifyCounts);
  }, [notifyCounts.p0, notifyCounts.p1, notifyCounts.p2, notifyCounts.total, onCounts]);

  // 收起不是停止工作：轮询继续，计数通过 onCounts 交给顶栏铃铛。
  if (!open) return null;

  if (!snapshot) {
    if (!error) return null;
    return (
      <aside className="wb-notify-center" aria-label="通知中心">
        <header className="wb-notify-top">
          <span className="wb-notify-title">通知</span>
          <button
            className="wb-notify-close"
            type="button"
            onClick={() => onOpenChange?.(false)}
            aria-label="关闭通知中心"
            title="关闭"
          >
            ✕
          </button>
        </header>
        <div className="wb-notify-error" role="alert">
          {error}
        </div>
      </aside>
    );
  }

  const { counts, slots, daily, desktop, sweep } = snapshot;
  const hasPanelError = Boolean(desktop.last_error || sweep.last_error || error);
  if (counts.total === 0 && !hasPanelError) return null;
  const busy = busyId !== null;
  const snoozed = (item: NotificationItem): boolean =>
    item.snooze_until != null && item.snooze_until * 1000 > nowMs;

  const actions = (item: NotificationItem): React.ReactNode => {
    const buttons: React.ReactNode[] = [];
    if (item.kind === "review") {
      buttons.push(
        <button
          key="verify"
          className="wb-notify-action verify"
          type="button"
          disabled={busy}
          onClick={() => onOpenVerify(item.task_id)}
        >
          去验收
        </button>,
      );
    }
    if (item.actions.includes("accept")) {
      buttons.push(
        <button
          key="accept"
          className="wb-notify-action accept"
          type="button"
          disabled={busy}
          onClick={() => void runAction(item, "accept")}
        >
          收
        </button>,
      );
    }
    if (item.actions.includes("reject")) {
      buttons.push(
        <button
          key="reject"
          className="wb-notify-action reject"
          type="button"
          disabled={busy}
          onClick={() => openReject(item)}
        >
          不收
        </button>,
      );
    }
    if (item.actions.includes("remind")) {
      buttons.push(
        <button
          key="remind"
          className="wb-notify-action remind"
          type="button"
          disabled={busy}
          onClick={() => void runAction(item, "remind")}
        >
          10 分钟后再提醒
        </button>,
      );
    }
    if (item.kind === "blocked") {
      buttons.push(
        <button
          key="board"
          className="wb-notify-action board"
          type="button"
          disabled={busy}
          onClick={onOpenBoard}
        >
          打开看板
        </button>,
      );
    }
    return buttons;
  };

  const reasonPanel = (item: NotificationItem): React.ReactNode =>
    rejectFor === item.id ? (
      <div className="wb-notify-reject">
        <label htmlFor={`wb-notify-reason-${item.id}`}>不收理由（必填，写回 comment）</label>
        <textarea
          id={`wb-notify-reason-${item.id}`}
          className="wb-notify-reason"
          data-event-id={item.id}
          value={reason}
          maxLength={MAX_REJECT_REASON_CHARS}
          disabled={busy}
          placeholder="写清楚哪里没达到验收标准，卡会回到 triage"
          onChange={(event) => setReason(event.target.value)}
        />
        <div className="wb-notify-reason-actions">
          <button
            className="wb-notify-action accept"
            type="button"
            disabled={busy || !reason.trim()}
            onClick={() => submitReject(item)}
          >
            提交不收，回到 triage
          </button>
          <button
            className="wb-notify-action"
            type="button"
            disabled={busy}
            onClick={() => {
              setRejectFor(null);
              setReason("");
              setActionError("");
            }}
          >
            取消
          </button>
        </div>
      </div>
    ) : null;

  const meta = (item: NotificationItem): React.ReactNode => (
    <div className="wb-notify-meta">
      <span className="wb-notify-task">{item.task_id}</span>
      <span className="wb-notify-reason-text">{item.reason}</span>
      {item.kind === "review" ? (
        <span className="wb-notify-countdown">{countdownText(item, snapshot, nowMs)}</span>
      ) : null}
      {item.block_kind ? <span className="wb-notify-kind">{item.block_kind}</span> : null}
      {snoozed(item) ? <span className="wb-notify-snoozed">已稍后提醒</span> : null}
      {item.escalated ? <span className="wb-notify-escalated">已升级/再提醒</span> : null}
    </div>
  );

  const itemRow = (item: NotificationItem, extraClass = ""): React.ReactNode => (
    <div className={"wb-notify-item" + (extraClass ? " " + extraClass : "")} key={item.id}>
      <div className="wb-notify-item-head">
        <span className="wb-notify-item-title">{item.title}</span>
        <span className="wb-notify-item-level">{item.level.toUpperCase()}</span>
      </div>
      {meta(item)}
      <div className="wb-notify-actions">{actions(item)}</div>
      {reasonPanel(item)}
    </div>
  );

  return (
    <aside className="wb-notify-center" aria-label="通知中心">
      <header className="wb-notify-top">
        <span className="wb-notify-title">通知</span>
        <span
          className="wb-notify-counts"
          aria-label="通知计数"
          role="status"
          aria-live="polite"
        >
          P0 {counts.p0} · P1 {counts.p1} · P2 {counts.p2}
        </span>
        <button
          className="wb-notify-refresh"
          type="button"
          onClick={() => void refresh(true)}
        >
          刷新
        </button>
        <button
          className="wb-notify-close"
          type="button"
          onClick={() => onOpenChange?.(false)}
          aria-label="关闭通知中心"
          title="关闭"
        >
          ✕
        </button>
      </header>

      {desktop.last_error ? (
        <div className="wb-notify-desktop-error" role="status">
          桌面通知失败：{desktop.last_error}
        </div>
      ) : null}
      {sweep.last_error ? (
        <div className="wb-notify-sweep-error" role="status">
          通知采集失败：{sweep.last_error}
        </div>
      ) : null}

      {snapshot.p0.length > 0 ? (
        <section className="wb-notify-pinned" aria-label="P0 置顶通知">
          <div className="wb-notify-section-head">
            <span>P0 · 立刻处理</span>
            <span className="wb-notify-section-count">{snapshot.p0.length}</span>
          </div>
          {snapshot.p0.map((item) => itemRow(item, "p0"))}
        </section>
      ) : null}

      {snapshot.p1.length > 0 ? (
        <section className="wb-notify-panel" aria-label="P1 通知">
          <div className="wb-notify-section-head">
            <span>P1 · 等你处理（攒批）</span>
            <span className="wb-notify-section-count">{snapshot.p1.length}</span>
          </div>
          {snapshot.p1.map((item) => itemRow(item, "p1"))}
        </section>
      ) : null}

      {slots.active.length > 0 || slots.queued_count > 0 ? (
        <section className="wb-notify-slots" aria-label="验收槽">
          <div className="wb-notify-section-head">
            <span>
              验收槽 {slots.active.length}/{slots.capacity} 束
            </span>
            {slots.queued_count > 0 ? (
              <span className="wb-notify-queue">
                排队 {slots.queued_count} 束 / {slots.queued_cards} 张
              </span>
            ) : null}
          </div>
          {slots.active.map((bundle) => (
            <div className="wb-notify-bundle" key={bundle.key}>
              <span className="wb-notify-bundle-label">{bundleLabel(bundle)}</span>
              <span className="wb-notify-bundle-cards">
                {bundle.cards
                  .map((card) => `${card.title}（${countdownText(card, snapshot, nowMs)}）`)
                  .join(" · ")}
              </span>
            </div>
          ))}
        </section>
      ) : null}

      {snapshot.p2.length > 0 ? (
        <section className="wb-notify-daily" aria-label="P2 每日汇总">
          <div className="wb-notify-section-head">
            <span>P2 · 每日 {String(daily.hour).padStart(2, "0")}:00 汇总</span>
            <span className="wb-notify-section-count">{snapshot.p2.length}</span>
          </div>
          <div className="wb-notify-daily-line" title={daily.line}>
            {daily.line}
          </div>
          <div className="wb-notify-daily-state">
            {daily.sent ? "今日已推送一次" : "今日尚未推送（到点后自动推送一次）"}
          </div>
          {snapshot.p2.map((item) => itemRow(item, "p2"))}
        </section>
      ) : null}

      {notice ? (
        <div className="wb-notify-notice" role="status">
          {notice}
        </div>
      ) : null}
      {actionError ? (
        <div className="wb-notify-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {error ? (
        <div className="wb-notify-error" role="alert">
          {error}
        </div>
      ) : null}
    </aside>
  );
}
