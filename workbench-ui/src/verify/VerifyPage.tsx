import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, KANBAN } from "../platform";
import {
  getInbox,
  markSolidifiedVerified,
  MAX_REJECT_REASON_CHARS,
  sweepReviews,
  verifyTask,
  WorkbenchError,
  type InboxTask,
  type SolidifiedEntry,
  type SolidifiedVerifiedEntry,
  type VerifyAction,
} from "../api/workbench";

/**
 * 全屏验收：一次只处理一张 review 卡。
 * 数据只读：inbox（id/标题/metadata/时长）+ 上游 GET /tasks/{id}
 * （result/latest_summary/attachments/events/runs）。本页不写上游状态，
 * 所有动作都走 POST /api/workbench/verify/{task_id}。
 */

const COUNTDOWN_TICK_MS = 30_000;
const INBOX_POLL_MS = 5000;
const AUTO_EXIT_MS = 3000;

const SOLIDIFIED_TYPE_LABEL: Record<SolidifiedEntry["type"], string> = {
  routing: "路由",
  skill_patch: "技能补丁",
  script: "脚本",
};

interface VerifyAttachment {
  id: number;
  filename: string;
  content_type?: string | null;
  size?: number | null;
}

interface VerifyRun {
  id: number;
  profile?: string | null;
  status?: string | null;
  outcome?: string | null;
  started_at?: number | null;
  ended_at?: number | null;
  summary?: string | null;
  error?: string | null;
}

interface VerifyEvent {
  id: number;
  kind: string;
  payload?: { summary?: string | null; [key: string]: unknown } | null;
  created_at?: number | null;
}

interface VerifyTaskDetail {
  task: {
    id?: string;
    title?: string;
    status?: string;
    result?: string | null;
    latest_summary?: string | null;
    assignee?: string | null;
  };
  attachments?: VerifyAttachment[];
  events?: VerifyEvent[];
  runs?: VerifyRun[];
}

interface VerifyStats {
  accepted: number;
  rejected: number;
  skipped: number;
}

/** 队列项带上本地接收时间：倒计时用后端 age + 本地经过时间。 */
type QueueItem = InboxTask & { receivedAt: number };

interface VerifyPageProps {
  /** 从「待收」行进入时优先展示该卡；从计数进入时为 null。 */
  initialTaskId?: string | null;
  /** 顶栏「验收 0」进入：队列为空时停在空态，不 3 秒自动退出。 */
  manualOpen?: boolean;
  onClose: () => void;
  /** 每完成一个动作后通知 App 刷新右上角计数。 */
  onChanged?: () => void;
  /** 打开该卡本轮执行会话（只读抽屉，由 App 挂载）。 */
  onOpenSession: (taskId: string, title?: string) => void;
  /** 会话抽屉打开时，验收页必须让出 Esc 与 A/R/S 键。 */
  sessionOpen?: boolean;
}

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatSize(size?: number | null): string {
  if (!size || size <= 0) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function formatClock(ts?: number | null): string {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分 ${secs} 秒`;
  return `${secs} 秒`;
}

function countdownLabel(ageSeconds: number | null | undefined, ttl: number): string {
  if (ageSeconds == null) return "无 review 记录，不会自动收";
  if (!Number.isFinite(ttl) || ttl <= 0) return "自动收时限未知";
  const remain = Math.max(0, ttl - ageSeconds);
  if (remain <= 0) return "已到自动收时限";
  const hours = Math.floor(remain / 3600);
  const minutes = Math.floor((remain % 3600) / 60);
  if (hours > 0) return `还有 ${hours} 小时 ${minutes} 分自动收`;
  return `还有 ${Math.max(1, minutes)} 分自动收`;
}

/** Runtime-safe view of `metadata.solidified_verified` (may be corrupt). */
function normalizeVerifiedEntries(value: unknown): SolidifiedVerifiedEntry[] {
  if (!Array.isArray(value)) return [];
  const byPath = new Map<string, SolidifiedVerifiedEntry>();
  for (const raw of value) {
    if (!raw || typeof raw !== "object") continue;
    const entry = raw as { path?: unknown; verified_at?: unknown };
    if (
      typeof entry.path !== "string" ||
      !entry.path.trim() ||
      typeof entry.verified_at !== "number" ||
      !Number.isFinite(entry.verified_at)
    ) {
      continue;
    }
    if (!byPath.has(entry.path)) {
      byPath.set(entry.path, {
        path: entry.path,
        verified_at: entry.verified_at,
      });
    }
  }
  return Array.from(byPath.values());
}

function sortQueue(items: QueueItem[]): QueueItem[] {
  // 与后端一致：最早 review 在前；无 review_requested 的卡排最后，
  // 同组内按 created_at / id 稳定排序。
  return [...items].sort((a, b) => {
    const aAt = a.review_requested_at ?? null;
    const bAt = b.review_requested_at ?? null;
    if (aAt === null && bAt !== null) return 1;
    if (aAt !== null && bAt === null) return -1;
    const delta = (aAt ?? a.created_at ?? 0) - (bAt ?? b.created_at ?? 0);
    if (delta !== 0) return delta;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

function latestRun(runs: VerifyRun[] | undefined): VerifyRun | null {
  if (!runs || runs.length === 0) return null;
  // Same order as the server: started_at, then run id.
  return [...runs].sort(
    (a, b) => (b.started_at ?? 0) - (a.started_at ?? 0) || b.id - a.id,
  )[0];
}

function latestReviewEvent(events: VerifyEvent[] | undefined): VerifyEvent | null {
  if (!events) return null;
  let latest: VerifyEvent | null = null;
  for (const event of events) {
    if (event.kind !== "review_requested") continue;
    if (!latest || (event.created_at ?? 0) > (latest.created_at ?? 0)) latest = event;
  }
  return latest;
}

export default function VerifyPage({
  initialTaskId,
  manualOpen = false,
  onClose,
  onChanged,
  onOpenSession,
  sessionOpen = false,
}: VerifyPageProps): React.ReactElement {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [ttl, setTtl] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [failedIds, setFailedIds] = useState<string[]>([]);
  const [detail, setDetail] = useState<VerifyTaskDetail | null>(null);
  const [detailError, setDetailError] = useState("");
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState("");
  const [markingPath, setMarkingPath] = useState<string | null>(null);
  const [stats, setStats] = useState<VerifyStats>({ accepted: 0, rejected: 0, skipped: 0 });
  const [openedEmpty, setOpenedEmpty] = useState(false);
  const openedDetermined = useRef(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const reasonRef = useRef<HTMLTextAreaElement | null>(null);
  const loadSeq = useRef(0);
  // 本会话已动作过的卡：跳过仍在上游 review，轮询不能把它重新加回队列。
  const handledRef = useRef<Set<string>>(new Set());
  const queueRef = useRef<QueueItem[]>([]);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  // 本会话手动标记的 overlay：5s 轮询可能带回过期快照，合并时保留本地的。
  const markedOverlaysRef = useRef<Record<string, SolidifiedVerifiedEntry[]>>({});

  const current = queue.length > 0 ? queue[0] : null;
  const currentId = current?.id ?? null;

  const withMarkedOverlay = useCallback(
    (task: InboxTask, receivedAt: number): QueueItem => {
      const marked = normalizeVerifiedEntries(markedOverlaysRef.current[task.id]);
      if (marked.length === 0) return { ...task, receivedAt };
      const base = normalizeVerifiedEntries(task.metadata?.solidified_verified);
      return {
        ...task,
        receivedAt,
        metadata: {
          ...task.metadata,
          solidified_verified: normalizeVerifiedEntries([...base, ...marked]),
        },
      };
    },
    [],
  );

  const loadQueue = useCallback(async () => {
    const seq = ++loadSeq.current;
    setLoadError("");
    setNotice("");
    try {
      // Explicit sweep is the only write path for overdue cards; polling and
      // reads stay pure.
      await sweepReviews().catch(() => undefined);
      const inbox = await getInbox();
      if (seq !== loadSeq.current) return;
      const startIndex = initialTaskId
        ? inbox.tasks.findIndex((task) => task.id === initialTaskId)
        : 0;
      if (initialTaskId && startIndex < 0) {
        setNotice("这张卡已不在待收队列，为你打开当前最早的一张");
      }
      // 从某一行进入时把该卡转到队首，仍按 1/N 顺序处理其余卡。
      const ordered =
        startIndex > 0
          ? [...inbox.tasks.slice(startIndex), ...inbox.tasks.slice(0, startIndex)]
          : inbox.tasks;
      const receivedAt = Date.now();
      setQueue(ordered.map((task) => withMarkedOverlay(task, receivedAt)));
      if (!openedDetermined.current) {
        openedDetermined.current = true;
        setOpenedEmpty(!initialTaskId && ordered.length === 0);
      }
      setTtl(inbox.review_ttl_seconds);
      setFailedIds(inbox.failed_ids);
    } catch (error) {
      if (seq !== loadSeq.current) return;
      setLoadError("验收队列不可读：" + errText(error));
    } finally {
      if (seq === loadSeq.current) setLoaded(true);
    }
  }, [initialTaskId, withMarkedOverlay]);

  useEffect(() => {
    void loadQueue();
  }, [loadQueue]);

  useEffect(() => {
    queueRef.current = queue;
  }, [queue]);

  // 队列与右上角计数同源：5s 轮询补齐新卡、刷新仍在队列里的字段、
  // 移除已被别处处理的卡，并按后端顺序（最早 review 在前）重排。
  // 本会话动作过的卡（尤其 skip 后仍是 review）由 handledRef 挡住；
  // 详情读取失败的卡保留上一份快照（failed_ids），不整队清空。
  const syncQueue = useCallback(async () => {
    try {
      const inbox = await getInbox();
      setTtl(inbox.review_ttl_seconds);
      setFailedIds(inbox.failed_ids);
      const receivedAt = Date.now();
      const inboxIds = new Set(inbox.tasks.map((task) => task.id));
      const failed = new Set(inbox.failed_ids);
      setQueue((prev) => {
        const refreshed = inbox.tasks
          .filter((task) => !handledRef.current.has(task.id))
          .map((task) => withMarkedOverlay(task, receivedAt));
        const preserved = prev.filter(
          (task) =>
            !inboxIds.has(task.id) &&
            failed.has(task.id) &&
            !handledRef.current.has(task.id),
        );
        return sortQueue([...refreshed, ...preserved]);
      });
    } catch {
      // 队列轮询失败保留现有快照；下一次轮询重试。
    }
  }, [withMarkedOverlay]);

  useEffect(() => {
    if (!loaded) return;
    const timer = window.setInterval(() => {
      void syncQueue();
    }, INBOX_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loaded, syncQueue]);

  // 队列清空：停留 3s 展示本批统计后自动退出；Esc/返回仍可提前关闭。
  // 关闭前最后查一次 inbox：统计页期间新到的卡要重新铺回队列，而不是被带走。
  useEffect(() => {
    if (!loaded || queue.length > 0) return;
    // 从「验收 0」进入且队列本来就空：用户是来确认界面的，停在空态。
    if (manualOpen && openedEmpty) return;
    const timer = window.setTimeout(async () => {
      try {
        const inbox = await getInbox();
        const fresh = inbox.tasks.filter((task) => !handledRef.current.has(task.id));
        if (fresh.length > 0) {
          const receivedAt = Date.now();
          setTtl(inbox.review_ttl_seconds);
          setFailedIds(inbox.failed_ids);
          setQueue(fresh.map((task) => ({ ...task, receivedAt })));
          return;
        }
      } catch {
        // 收尾检查失败就按原计划退出。
      }
      onClose();
    }, AUTO_EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [loaded, queue.length, manualOpen, openedEmpty, onClose]);

  // Focus the dialog on open, trap Tab inside it, and restore focus on close.
  useEffect(() => {
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    overlayRef.current?.focus();
    return () => {
      const previous = returnFocusRef.current;
      if (previous && document.contains(previous) && typeof previous.focus === "function") {
        previous.focus();
      }
    };
  }, []);

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), COUNTDOWN_TICK_MS);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!currentId) {
      setDetail(null);
      setDetailError("");
      return;
    }
    let alive = true;
    setDetail(null);
    setDetailError("");
    void api(KANBAN + "/tasks/" + encodeURIComponent(currentId))
      .then((data: unknown) => {
        if (!alive) return;
        if (data && typeof data === "object" && (data as { task?: unknown }).task) {
          setDetail(data as VerifyTaskDetail);
        } else {
          setDetailError("产出物响应格式不正确");
        }
      })
      .catch((error: unknown) => {
        if (!alive) return;
        setDetailError("产出物不可读：" + errText(error));
      });
    return () => {
      alive = false;
    };
  }, [currentId]);

  useEffect(() => {
    if (reasonOpen) reasonRef.current?.focus();
  }, [reasonOpen]);

  // 轮询把当前卡移走 / 前进到下一张时，理由输入和上一张的报错不能留在新卡上。
  useEffect(() => {
    setReasonOpen(false);
    setReason("");
    setActionError("");
    setMarkingPath(null);
  }, [currentId]);

  const runAction = useCallback(
    async (action: VerifyAction, reasonText?: string) => {
      if (!current || submitting) return;
      setSubmitting(true);
      setActionError("");
      try {
        await verifyTask(current.id, action, reasonText);
        setStats((prev) => ({
          accepted: prev.accepted + (action === "accept" ? 1 : 0),
          rejected: prev.rejected + (action === "reject" ? 1 : 0),
          skipped: prev.skipped + (action === "skip" ? 1 : 0),
        }));
        setReasonOpen(false);
        setReason("");
        handledRef.current.add(current.id);
        setQueue((prev) => prev.filter((task) => task.id !== current.id));
        onChanged?.();
      } catch (error) {
        const code = error instanceof WorkbenchError ? error.code : "";
        if (code === "not_in_review") {
          // 卡已被别处处理 / 状态变化：从本批移除，但不要标进 handledRef。
          // 如果上游其实仍是 review（并发/偶发），下一次 5s 轮询会重新加回。
          setActionError("这张卡已不在验收队列，已移除");
          setQueue((prev) => prev.filter((task) => task.id !== current.id));
          onChanged?.();
        } else if (code === "reason_required") {
          setReasonOpen(true);
          setActionError("不收必须写理由");
        } else if (code === "reason_too_long") {
          setReasonOpen(true);
          setActionError(`理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`);
        } else if (code === "missing_solidified") {
          setActionError(
            "没有可用的固化记录（未登记或格式不合法），不能收；可打回补齐，或等 48h 自动收",
          );
        } else {
          setActionError("验收失败：" + errText(error));
        }
      } finally {
        setSubmitting(false);
      }
    },
    [current, submitting, onChanged],
  );

  const markVerified = useCallback(
    async (path: string) => {
      if (!current || markingPath) return;
      setMarkingPath(path);
      setActionError("");
      try {
        const overlay = await markSolidifiedVerified(current.id, path);
        markedOverlaysRef.current[current.id] = overlay;
        // 本地合并 overlay：轮询也会带回同一份，按钮无需等下一轮。
        setQueue((prev) =>
          prev.map((task) =>
            task.id === current.id
              ? {
                  ...task,
                  metadata: { ...task.metadata, solidified_verified: overlay },
                }
              : task,
          ),
        );
      } catch (error) {
        const code = error instanceof WorkbenchError ? error.code : "";
        setActionError(
          code === "missing_solidified"
            ? "这条固化记录不在最新一轮 run；可打回补齐，或等 48h 自动收"
            : "标记已验证失败：" + errText(error),
        );
      } finally {
        setMarkingPath(null);
      }
    },
    [current, markingPath],
  );

  const openReject = useCallback(() => {
    setReason("");
    setReasonOpen(true);
    setActionError("");
  }, []);

  const submitReject = useCallback(() => {
    if (!reason.trim()) {
      setActionError("不收必须写理由");
      return;
    }
    if (reason.trim().length > MAX_REJECT_REASON_CHARS) {
      setActionError(`理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`);
      return;
    }
    void runAction("reject", reason);
  }, [reason, runAction]);

  useEffect(() => {
    const focusables = (): HTMLElement[] => {
      const overlay = overlayRef.current;
      if (!overlay) return [];
      return Array.from(
        overlay.querySelectorAll<HTMLElement>(
          'button:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((node) => node.offsetParent !== null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.repeat) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      // 会话抽屉在上层打开时，Esc / A / R / S 都属于抽屉；这里完全让出。
      if (sessionOpen) return;
      if (event.key === "Tab") {
        const nodes = focusables();
        if (nodes.length === 0) {
          event.preventDefault();
          return;
        }
        const first = nodes[0];
        const last = nodes[nodes.length - 1];
        const active = document.activeElement;
        if (!event.shiftKey && (active === last || active === overlayRef.current)) {
          event.preventDefault();
          first.focus();
        } else if (event.shiftKey && (active === first || active === overlayRef.current)) {
          event.preventDefault();
          last.focus();
        }
        return;
      }
      if (event.key === "Escape") {
        // 契约：Esc 一律退出验收（不论是否在写理由）。
        event.preventDefault();
        onClose();
        return;
      }
      if (reasonOpen || submitting || !current) return;
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT" || target?.isContentEditable) {
        return;
      }
      const key = event.key.toLowerCase();
      if (key === "a") {
        event.preventDefault();
        void runAction("accept");
      } else if (key === "r") {
        event.preventDefault();
        openReject();
      } else if (key === "s") {
        event.preventDefault();
        void runAction("skip");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, reasonOpen, submitting, runAction, openReject, onClose, sessionOpen]);

  const processed = stats.accepted + stats.rejected + stats.skipped;
  // 总数为派生值：轮询补进来的新卡会自动把 N 增大。
  const total = processed + queue.length;
  const position = Math.min(processed + 1, Math.max(total, 1));
  const progress = total > 0 ? Math.min(1, processed / total) : 0;
  const ageSeconds = useMemo(() => {
    if (current?.review_age_seconds == null) return null;
    return current.review_age_seconds + Math.max(0, (nowMs - current.receivedAt) / 1000);
  }, [current, nowMs]);

  const task = detail?.task;
  const title = task?.title || current?.title || "";
  const summary = task?.latest_summary ?? current?.latest_summary ?? null;
  const result = task?.result ?? current?.result ?? null;
  const attachments = detail?.attachments ?? [];
  const run = latestRun(detail?.runs);
  const reviewEvent = latestReviewEvent(detail?.events);
  const accept = current?.metadata?.contract?.accept?.trim() || "";
  const input = current?.metadata?.contract?.input?.trim() || "";
  const reviewAt = current?.review_requested_at ?? null;
  const selfReport = current?.self_report ?? null;
  const solidified = current?.solidified ?? [];
  const verifiedPaths = useMemo(
    () =>
      new Set(
        normalizeVerifiedEntries(current?.metadata?.solidified_verified).map(
          (entry) => entry.path,
        ),
      ),
    [current?.metadata?.solidified_verified],
  );

  const closeButton = (
    <button className="wb-btn wb-verify-close" type="button" onClick={onClose}>
      退出 <kbd>Esc</kbd>
    </button>
  );

  return (
    <div
      className="wb-verify-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="验收"
      ref={overlayRef}
      tabIndex={-1}
    >
      <header className="wb-verify-top">
        <span className="wb-verify-brand">验收</span>
        {current ? (
          <>
            <span className="wb-verify-count" aria-label="验收进度">
              {position}/{Math.max(total, 1)}
            </span>
            <span className="wb-verify-progress" aria-hidden="true">
              <span style={{ width: `${Math.round(progress * 100)}%` }} />
            </span>
            <span className="wb-verify-countdown">
              {countdownLabel(ageSeconds, ttl)}
            </span>
          </>
        ) : null}
        <span className="wb-verify-top-spacer" />
        {closeButton}
      </header>

      {notice ? (
        <div className="wb-verify-notice" role="status">
          {notice}
        </div>
      ) : null}
      {failedIds.length > 0 ? (
        <div className="wb-verify-notice" role="status">
          {failedIds.length} 张卡的详情暂时不可读，已保留在队列中稍后重试
        </div>
      ) : null}
      {current?.auto_collect_error ? (
        <div className="wb-verify-notice" role="status">
          48h 自动收失败：{current.auto_collect_error.message}
          {typeof current.auto_collect_error.retry_in_seconds === "number"
            ? `（约 ${current.auto_collect_error.retry_in_seconds}s 后重试）`
            : ""}
        </div>
      ) : null}

      {loadError ? (
        <div className="wb-verify-state">
          <div className="wb-verify-state-text">{loadError}</div>
          <button
            className="wb-btn"
            type="button"
            onClick={() => {
              setLoaded(false);
              void loadQueue();
            }}
          >
            重试
          </button>
        </div>
      ) : !loaded ? (
        <div className="wb-verify-state">加载验收队列…</div>
      ) : !current ? (
        manualOpen && openedEmpty ? (
          <div className="wb-verify-finish wb-verify-empty">
            <div className="wb-verify-finish-title">现在没有待验收的卡</div>
            <p className="wb-verify-finish-hint">
              验收队列是空的。收完的卡会进入「已完成」；新的待收会自动出现在这里。
            </p>
            <button className="wb-btn wb-verify-primary" type="button" onClick={onClose}>
              返回 <kbd>Esc</kbd>
            </button>
          </div>
        ) : (
          <div className="wb-verify-finish">
            <div className="wb-verify-finish-title">本批验收完成</div>
            <div className="wb-verify-stats">
              <div className="wb-verify-stat accepted">
                <b>{stats.accepted}</b>
                <span>已收</span>
              </div>
              <div className="wb-verify-stat rejected">
                <b>{stats.rejected}</b>
                <span>不收</span>
              </div>
              <div className="wb-verify-stat skipped">
                <b>{stats.skipped}</b>
                <span>跳过</span>
              </div>
            </div>
            <p className="wb-verify-finish-hint">
              {stats.skipped > 0 ? "跳过的卡仍在待收，下次进入会再次出现。" : "队列已清空。"}
            </p>
            <p className="wb-verify-finish-hint">3 秒后自动返回；Esc 可立即退出。</p>
            <button className="wb-btn wb-verify-primary" type="button" onClick={onClose}>
              返回待办 <kbd>Esc</kbd>
            </button>
          </div>
        )
      ) : (
        <div className="wb-verify-body">
          <section className="wb-verify-pane wb-verify-produce">
            <div className="wb-verify-heading">
              <h1 className="wb-verify-title">{title}</h1>
              <div className="wb-verify-sub">
                <span>{current.id}</span>
                {current.assignee ? <span>{current.assignee}</span> : null}
                <span>review</span>
              </div>
            </div>

            {detailError ? <div className="wb-verify-note warn">{detailError}</div> : null}

            <div className="wb-verify-section">
              <h2>运行摘要</h2>
              {summary ? (
                <pre className="wb-verify-text">{summary}</pre>
              ) : (
                <div className="wb-verify-note">worker 没有留下运行摘要</div>
              )}
            </div>

            <div className="wb-verify-section">
              <h2>产出物</h2>
              {result ? (
                <pre className="wb-verify-text">{result}</pre>
              ) : (
                <div className="wb-verify-note">
                  {detail ? "worker 没有留下产出文本，以下为附件。" : "加载产出物…"}
                </div>
              )}
              {attachments.length > 0 ? (
                <div className="wb-verify-attachments">
                  {attachments.map((attachment) =>
                    attachment.content_type?.startsWith("image/") ? (
                      <figure className="wb-verify-figure" key={attachment.id}>
                        <img
                          src={KANBAN + "/attachments/" + attachment.id}
                          alt={attachment.filename}
                          loading="lazy"
                        />
                        <figcaption>
                          {attachment.filename}
                          {formatSize(attachment.size) ? " · " + formatSize(attachment.size) : ""}
                        </figcaption>
                      </figure>
                    ) : (
                      <a
                        className="wb-verify-file"
                        key={attachment.id}
                        href={KANBAN + "/attachments/" + attachment.id}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <span className="wb-verify-file-name">{attachment.filename}</span>
                        <span className="wb-verify-file-meta">
                          {formatSize(attachment.size)}
                        </span>
                      </a>
                    ),
                  )}
                </div>
              ) : detail ? (
                <div className="wb-verify-note">没有附件</div>
              ) : null}
            </div>
          </section>

          <aside className="wb-verify-pane wb-verify-accept">
            <div className="wb-verify-section">
              <h2>验收标准</h2>
              {accept ? (
                <p className="wb-verify-standard">{accept}</p>
              ) : (
                <p className="wb-verify-note warn">这张卡没有写验收标准</p>
              )}
            </div>

            <div className="wb-verify-section wb-verify-self-report">
              <h2>worker 自述</h2>
              {selfReport ? (
                <dl className="wb-verify-run wb-verify-self">
                  <dt>交付</dt>
                  <dd>{selfReport.delivered || "—"}</dd>
                  <dt>依据</dt>
                  <dd>{selfReport.evidence || "—"}</dd>
                  <dt>没底</dt>
                  <dd
                    className={
                      selfReport.uncertain ? "wb-verify-self-uncertain" : "wb-verify-note warn"
                    }
                  >
                    {selfReport.uncertain || "未填写"}
                  </dd>
                </dl>
              ) : (
                <p className="wb-verify-note warn">自述缺失</p>
              )}
            </div>

            <div className="wb-verify-section wb-verify-solidified">
              <h2>可复用件</h2>
              {solidified.length > 0 ? (
                <ul className="wb-verify-solidified-list">
                  {solidified.map((entry, index) => {
                    const verified =
                      entry.status === "verified" || verifiedPaths.has(entry.path);
                    const marking = markingPath === entry.path;
                    return (
                      <li
                        className="wb-verify-solidified-item"
                        key={`${entry.type}:${entry.path}:${index}`}
                      >
                        <span
                          className="wb-verify-solidified-type"
                          data-type={entry.type}
                          title={entry.type}
                        >
                          {SOLIDIFIED_TYPE_LABEL[entry.type]}
                        </span>
                        <span className="wb-verify-solidified-path" title={entry.path}>
                          {entry.path}
                        </span>
                        <span
                          className={
                            "wb-verify-solidified-status " +
                            (verified ? "verified" : "pending")
                          }
                        >
                          {verified ? "已验证" : "待验证"}
                        </span>
                        {verified ? null : (
                          <button
                            className="wb-btn wb-verify-solidified-verify"
                            type="button"
                            disabled={markingPath !== null}
                            onClick={() => void markVerified(entry.path)}
                          >
                            {marking ? "标记中…" : "标记已验证"}
                          </button>
                        )}
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <p className="wb-verify-note warn">
                  没有可用的固化记录（未登记或格式不合法），不能收；可打回补齐，或等 48h 自动收。
                </p>
              )}
            </div>

            {input ? (
              <div className="wb-verify-section">
                <h2>输入 / 需求</h2>
                <p className="wb-verify-input">{input}</p>
              </div>
            ) : null}

            <div className="wb-verify-section">
              <h2>本轮运行信息</h2>
              {run ? (
                <dl className="wb-verify-run">
                  <dt>profile</dt>
                  <dd>{run.profile || "—"}</dd>
                  <dt>状态</dt>
                  <dd>{run.outcome || run.status || "—"}</dd>
                  <dt>开始</dt>
                  <dd>{formatClock(run.started_at)}</dd>
                  <dt>耗时</dt>
                  <dd>
                    {run.started_at
                      ? formatDuration(
                          (run.ended_at || Math.floor(nowMs / 1000)) - run.started_at,
                        )
                      : "—"}
                  </dd>
                  {run.summary ? (
                    <>
                      <dt>运行摘要</dt>
                      <dd className="wb-verify-run-text">{run.summary}</dd>
                    </>
                  ) : null}
                  {run.error ? (
                    <>
                      <dt>错误</dt>
                      <dd className="wb-verify-run-error">{run.error}</dd>
                    </>
                  ) : null}
                </dl>
              ) : (
                <div className="wb-verify-note">
                  {detail ? "没有运行记录" : "加载运行信息…"}
                </div>
              )}
              <div className="wb-verify-review-at">
                进入验收 {reviewEvent ? formatClock(reviewEvent.created_at) : formatClock(reviewAt) || "时间未知"}
              </div>
              <div className="wb-verify-session-slot">
                <button
                  className="wb-btn wb-verify-session-open"
                  type="button"
                  onClick={() => onOpenSession(current.id, title)}
                >
                  打开执行会话
                </button>
              </div>
            </div>
          </aside>
        </div>
      )}

      {actionError ? (
        <div className="wb-verify-error" role="alert">
          {actionError}
        </div>
      ) : null}

      {current ? (
        <footer className="wb-verify-foot">
          {reasonOpen ? (
            <div className="wb-verify-reason">
              <label htmlFor="wb-verify-reason-input">不收理由（必填，写回 comment）</label>
              <textarea
                id="wb-verify-reason-input"
                ref={reasonRef}
                value={reason}
                maxLength={MAX_REJECT_REASON_CHARS}
                disabled={submitting}
                placeholder="写清楚哪里没达到验收标准，卡会回到 triage（Ctrl/Cmd+Enter 提交）"
                onChange={(event) => setReason(event.target.value)}
                onKeyDown={(event) => {
                  if (event.nativeEvent.isComposing) return;
                  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                    event.preventDefault();
                    submitReject();
                  }
                }}
              />
              <div className="wb-verify-reason-count">
                {reason.trim().length}/{MAX_REJECT_REASON_CHARS}
              </div>
              <div className="wb-verify-reason-actions">
                <button
                  className="wb-btn wb-verify-primary"
                  type="button"
                  disabled={submitting || !reason.trim()}
                  onClick={submitReject}
                >
                  {submitting ? "提交中…" : "提交不收，回到 triage"}
                </button>
                <button
                  className="wb-btn"
                  type="button"
                  disabled={submitting}
                  onClick={() => {
                    setReasonOpen(false);
                    setReason("");
                    setActionError("");
                  }}
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <div className="wb-verify-keys">
              <button
                className="wb-verify-key accept"
                type="button"
                disabled={submitting}
                onClick={() => void runAction("accept")}
              >
                <kbd>A</kbd> 收
              </button>
              <button
                className="wb-verify-key reject"
                type="button"
                disabled={submitting}
                onClick={openReject}
              >
                <kbd>R</kbd> 不收
              </button>
              <button
                className="wb-verify-key skip"
                type="button"
                disabled={submitting}
                onClick={() => void runAction("skip")}
              >
                <kbd>S</kbd> 跳过
              </button>
              <span className="wb-verify-key-hint">
                收 = done；不收必须写理由并回 triage；跳过不改状态
              </span>
            </div>
          )}
        </footer>
      ) : null}
    </div>
  );
}