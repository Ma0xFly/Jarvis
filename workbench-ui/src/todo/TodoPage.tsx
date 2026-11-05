import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, KANBAN } from "../platform";
import type { Task } from "../board/Board";
import TaskDetail from "./TaskDetail";
import "../styles/detail.css";
import {
  createTask,
  dispatchTask,
  getTaskMetadata,
  hasAcceptance,
  mergeTaskMetadata,
  updateTaskMetadata,
  WorkbenchError,
  type Bucket,
  type TaskMetadata,
} from "../api/workbench";
import { PositionBadge, ReceiptBar, type ReceiptState } from "./Receipt";

interface BoardColumn {
  name: string;
  tasks: Task[];
}

interface BoardData {
  columns: BoardColumn[];
}

type TaskWithMeta = Task & { metadata?: TaskMetadata };

const POLL_MS = 5000;
/** 回执是瞬时 UI：出现后保留一会儿，刷新/轮询都不重建它。 */
const RECEIPT_MS = 6000;

/** 流水线只放 ready/running/review；todo/blocked/scheduled 留在看板 */
const FLOW_SEGMENTS = [
  { key: "ready", label: "队列" },
  { key: "running", label: "运行" },
  { key: "review", label: "待收" },
] as const;

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function acceptText(task: TaskWithMeta): string {
  const accept = task.metadata?.contract?.accept;
  return typeof accept === "string" ? accept.trim() : "";
}

/** 仓库卡：triage（旧卡）/ todo（旧仓库）/ scheduled（新仓库） + bucket；固定 created_at 倒序 */
function repoCards(tasks: TaskWithMeta[], bucket: Bucket): TaskWithMeta[] {
  return tasks
    .filter(
      (task) =>
        (task.status === "triage" ||
          task.status === "todo" ||
          task.status === "scheduled") &&
        task.metadata?.bucket === bucket,
    )
    .sort(
      (a, b) =>
        (b.created_at ?? 0) - (a.created_at ?? 0) ||
        (a.id < b.id ? 1 : a.id > b.id ? -1 : 0),
    );
}

/** 每次草稿尝试一个 key；失败重试复用，成功清空。 */
function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "wb-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
}

interface TodoPageProps {
  /** 独立版入口：打开全屏验收；带 taskId 时先看这张卡。 */
  onVerify?: (taskId?: string) => void;
  /** 打开只读执行会话抽屉（详情里的出口）。 */
  onOpenSession?: (taskId: string, title?: string) => void;
  /** 外壳侧栏计数：今日 + 长期仓库卡数量。 */
  onCounts?: (counts: { today: number; long: number }) => void;
  /** 每次自增表示「切到待办并聚焦 composer」；0 表示首屏不抢焦点。 */
  focusComposer?: number;
  /** 当前是否为可见 tab；切走时关闭详情，避免 body 锁留在隐藏面板里。 */
  active?: boolean;
}

export default function TodoPage({
  onVerify,
  onOpenSession,
  onCounts,
  focusComposer,
  active = true,
}: TodoPageProps): React.ReactElement {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [metadataById, setMetadataById] = useState<Record<string, TaskMetadata>>({});
  const [err, setErr] = useState("");
  const [dropActive, setDropActive] = useState(false);
  const [bucketDrop, setBucketDrop] = useState<Bucket | null>(null);
  const [toast, setToast] = useState("");
  const [receipt, setReceipt] = useState<ReceiptState | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [shakeId, setShakeId] = useState<string | null>(null);
  const [warnId, setWarnId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [savingId, setSavingId] = useState<string | null>(null);
  const [dispatchingId, setDispatchingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [draftBucket, setDraftBucket] = useState<Bucket>("today");
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);

  const composerRef = useRef<HTMLInputElement | null>(null);
  const toastTimer = useRef<number | null>(null);
  const shakeTimer = useRef<number | null>(null);
  const warnTimer = useRef<number | null>(null);
  const receiptTimer = useRef<number | null>(null);
  const receiptSeq = useRef(0);
  const dismissedSeq = useRef(-1);
  const cancelEditRef = useRef(false);
  // A poll that started before a local write must not overwrite that write.
  const loadSeq = useRef(0);
  const mutationSeq = useRef(0);
  const savingCardsRef = useRef<Set<string>>(new Set());
  // 全局串行派发：一次只允许一张卡在途，回执只属于最近一次尝试。
  const dispatchInFlightRef = useRef(false);
  const draftAttemptRef = useRef<{ key: string; title: string; bucket: Bucket } | null>(null);

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2600);
  }, []);

  const flashWarn = useCallback((taskId: string) => {
    setShakeId(taskId);
    setWarnId(taskId);
    if (shakeTimer.current) window.clearTimeout(shakeTimer.current);
    if (warnTimer.current) window.clearTimeout(warnTimer.current);
    shakeTimer.current = window.setTimeout(() => setShakeId(null), 320);
    warnTimer.current = window.setTimeout(() => setWarnId(null), 2600);
  }, []);

  const clearReceipt = useCallback(() => {
    if (receiptTimer.current) {
      window.clearTimeout(receiptTimer.current);
      receiptTimer.current = null;
    }
    setReceipt(null);
  }, []);

  /** drop 时立即显示 pending；替换上一条回执（latest-attempt）。 */
  const showPendingReceipt = useCallback((): number => {
    receiptSeq.current += 1;
    const seq = receiptSeq.current;
    if (receiptTimer.current) {
      window.clearTimeout(receiptTimer.current);
      receiptTimer.current = null;
    }
    setReceipt({ kind: "pending", seq });
    return seq;
  }, []);

  /** 只消费 dispatch 返回值；不重算 ETA，也不写缓存。 */
  const showReceipt = useCallback(
    (seq: number, position: number, eta: string | null) => {
      // 用户已关闭这次尝试的回执：迟到的响应不再弹出。
      if (dismissedSeq.current === seq) return;
      setReceipt({ kind: "final", seq, position, eta });
      if (receiptTimer.current) window.clearTimeout(receiptTimer.current);
      receiptTimer.current = window.setTimeout(() => setReceipt(null), RECEIPT_MS);
    },
    [],
  );

  const dismissReceipt = useCallback(() => {
    dismissedSeq.current = receiptSeq.current;
    clearReceipt();
  }, [clearReceipt]);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    const mutation = mutationSeq.current;
    const superseded = () => seq !== loadSeq.current || mutation !== mutationSeq.current;
    try {
      const data = (await api(KANBAN + "/board")) as BoardData;
      if (superseded()) return;
      const columns = Array.isArray(data.columns) ? data.columns : [];
      setBoard({ columns });
      const ids = columns.reduce<string[]>(
        (acc, column) => acc.concat((column.tasks || []).map((task) => task.id)),
        [],
      );
      try {
        const meta = await getTaskMetadata(ids);
        if (superseded()) return;
        setMetadataById(meta);
        setErr("");
      } catch (error) {
        if (superseded()) return;
        // Keep the previous map so a transient metadata failure does not make
        // every repository card disappear.
        setErr("契约不可读：" + errText(error));
      }
      // Only clear loading after metadata settles (success or failure): the
      // first paint must not show empty today/long columns while acceptance
      // is still in flight.
      setLoaded(true);
    } catch (error) {
      if (superseded()) return;
      setErr("kanban 不可达：" + errText(error));
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(
    () => () => {
      if (toastTimer.current) window.clearTimeout(toastTimer.current);
      if (shakeTimer.current) window.clearTimeout(shakeTimer.current);
      if (warnTimer.current) window.clearTimeout(warnTimer.current);
      if (receiptTimer.current) window.clearTimeout(receiptTimer.current);
    },
    [],
  );

  const allTasks = useMemo<Task[]>(() => {
    if (!board) return [];
    return board.columns.reduce<Task[]>((acc, column) => acc.concat(column.tasks || []), []);
  }, [board]);

  const tasks = useMemo(
    () => mergeTaskMetadata(allTasks, metadataById) as TaskWithMeta[],
    [allTasks, metadataById],
  );

  const today = useMemo(() => repoCards(tasks, "today"), [tasks]);
  const long = useMemo(() => repoCards(tasks, "long"), [tasks]);

  useEffect(() => {
    onCounts?.({ today: today.length, long: long.length });
  }, [today.length, long.length, onCounts]);

  // 新建按钮只切 tab + 自增信号；composer 挂载（loaded）且不在建卡中才聚焦。
  useEffect(() => {
    if (!focusComposer || !loaded || creating) return;
    const frame = window.requestAnimationFrame(() => composerRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [focusComposer, loaded, creating]);

  const flowLists = FLOW_SEGMENTS.map((segment) => ({
    ...segment,
    cards: tasks.filter((task) => task.status === segment.key),
  }));
  const flowCount = flowLists.reduce((sum, segment) => sum + segment.cards.length, 0);

  const detailTask = useMemo(
    () => (detailId ? tasks.find((task) => task.id === detailId) : undefined),
    [detailId, tasks],
  );
  const readyList = flowLists.find((segment) => segment.key === "ready")?.cards || [];
  const detailPosition =
    detailTask?.status === "ready"
      ? readyList.findIndex((task) => task.id === detailTask.id) + 1
      : undefined;

  useEffect(() => {
    if (detailId && loaded && !tasks.some((task) => task.id === detailId)) {
      setDetailId(null);
    }
  }, [detailId, loaded, tasks]);

  useEffect(() => {
    if (!active) setDetailId(null);
  }, [active]);

  const dispatchToFlow = useCallback(
    async (task: TaskWithMeta) => {
      // 防御性串行化：handleDrop 已拦一次，这里再兜底。
      if (dispatchInFlightRef.current) {
        showToast("正在派发上一张");
        return;
      }
      dispatchInFlightRef.current = true;
      // 新尝试立刻用 pending 替换上一条回执；失败时清掉，不留下旧成功回执。
      const seq = showPendingReceipt();
      setDispatchingId(task.id);
      try {
        const result = await dispatchTask(task.id);
        showReceipt(seq, result.position, result.eta);
      } catch (error) {
        clearReceipt();
        if (error instanceof WorkbenchError && error.code === "blocked_by_parent") {
          showToast("已回到仓库，等 parent 完成");
        } else if (error instanceof WorkbenchError && error.code === "missing_acceptance") {
          flashWarn(task.id);
          return;
        } else if (error instanceof WorkbenchError && error.code === "dispatch_timeout") {
          // The dispatch client aborted; pending receipt was cleared above and
          // the in-flight guard is released in `finally`.
          showToast("派发超时，请刷新后重试");
        } else {
          showToast("派发失败：" + errText(error));
        }
      } finally {
        dispatchInFlightRef.current = false;
        setDispatchingId(null);
        mutationSeq.current += 1;
        void load();
      }
    },
    [showToast, flashWarn, clearReceipt, showPendingReceipt, showReceipt, load],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLElement>) => {
      event.preventDefault();
      setDropActive(false);
      const taskId = event.dataTransfer.getData("text/plain");
      if (!taskId) return;
      const task = tasks.find((candidate) => candidate.id === taskId);
      if (!task) return;
      // 全局串行：上一张还在派发时，忽略新 drop，保留当前 pending 回执。
      if (dispatchInFlightRef.current) {
        showToast("正在派发上一张");
        return;
      }
      // 客户端闸门：验收为空时零请求，抖动 + 红字后卡回原位。
      if (!hasAcceptance(task.metadata)) {
        clearReceipt();
        flashWarn(task.id);
        return;
      }
      void dispatchToFlow(task);
    },
    [tasks, showToast, flashWarn, clearReceipt, dispatchToFlow],
  );

  const handleDragLeave = (event: React.DragEvent<HTMLElement>) => {
    const next = event.relatedTarget as Node | null;
    if (!next || !event.currentTarget.contains(next)) setDropActive(false);
  };

  /** 仓库之间拖拽只改归属（bucket），不派发；只有流水线的 drop 才调用 dispatch。 */
  const moveTaskBucket = useCallback(
    async (task: TaskWithMeta, bucket: Bucket) => {
      if (task.metadata?.bucket === bucket) return;
      if (task.status !== "triage" && task.status !== "todo") return;
      if (savingCardsRef.current.has(task.id)) return;
      savingCardsRef.current.add(task.id);
      setSavingId(task.id);
      mutationSeq.current += 1;
      try {
        const metadata = await updateTaskMetadata(task.id, { bucket });
        setMetadataById((prev) => ({ ...prev, [task.id]: metadata }));
        showToast(bucket === "today" ? "已移到今日" : "已移到长期");
      } catch (error) {
        showToast("移动失败：" + errText(error));
      } finally {
        savingCardsRef.current.delete(task.id);
        setSavingId(null);
      }
    },
    [showToast],
  );

  const handleBucketDragOver = (event: React.DragEvent<HTMLElement>, bucket: Bucket) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setBucketDrop(bucket);
  };

  const handleBucketDragLeave = (event: React.DragEvent<HTMLElement>, bucket: Bucket) => {
    const next = event.relatedTarget as Node | null;
    if (!next || !event.currentTarget.contains(next)) {
      setBucketDrop((current) => (current === bucket ? null : current));
    }
  };

  const handleBucketDrop = (event: React.DragEvent<HTMLElement>, bucket: Bucket) => {
    event.preventDefault();
    setBucketDrop(null);
    const taskId = event.dataTransfer.getData("text/plain");
    if (!taskId) return;
    const task = tasks.find((candidate) => candidate.id === taskId);
    if (task) void moveTaskBucket(task, bucket);
  };

  const startEdit = (task: TaskWithMeta) => {
    if (savingCardsRef.current.has(task.id)) return;
    cancelEditRef.current = false;
    setEditingId(task.id);
    setEditText(acceptText(task));
  };

  const commitAccept = useCallback(
    async (task: TaskWithMeta) => {
      if (editingId !== task.id) return;
      if (savingCardsRef.current.has(task.id)) return;
      const typed = editText;
      const next = typed.trim();
      const current = acceptText(task);
      setEditingId(null);
      if (next === current) return;
      savingCardsRef.current.add(task.id);
      setSavingId(task.id);
      try {
        const metadata = await updateTaskMetadata(task.id, {
          contract: { accept: next },
        });
        mutationSeq.current += 1;
        setMetadataById((prev) => ({ ...prev, [task.id]: metadata }));
      } catch (error) {
        // Keep the typed text instead of dropping the user's edit.
        setEditingId(task.id);
        setEditText(typed);
        showToast("验收保存失败，请重试（" + errText(error) + "）");
      } finally {
        savingCardsRef.current.delete(task.id);
        setSavingId(null);
      }
    },
    [editingId, editText, showToast],
  );

  const onAcceptKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "Enter") {
      event.preventDefault();
      event.currentTarget.blur();
    } else if (event.key === "Escape") {
      cancelEditRef.current = true;
      event.currentTarget.blur();
    }
  };

  const onAcceptBlur = (task: TaskWithMeta) => {
    if (cancelEditRef.current) {
      cancelEditRef.current = false;
      setEditingId(null);
      return;
    }
    void commitAccept(task);
  };

  const submitDraft = useCallback(async () => {
    const title = draft.trim();
    if (!title || creating) return;
    const previous = draftAttemptRef.current;
    const key =
      previous && previous.title === title && previous.bucket === draftBucket
        ? previous.key
        : newIdempotencyKey();
    draftAttemptRef.current = { key, title, bucket: draftBucket };
    setCreating(true);
    try {
      await createTask(title, draftBucket, key);
      draftAttemptRef.current = null;
      mutationSeq.current += 1;
      setDraft("");
      showToast(draftBucket === "today" ? "已加到今日" : "已加到长期");
      void load();
    } catch (error) {
      // Keep draftAttemptRef so a retry reuses the same upstream idempotency key.
      showToast("建卡失败，请重试（" + errText(error) + "）");
    } finally {
      setCreating(false);
    }
  }, [draft, draftBucket, creating, showToast, load]);

  const renderRepoCard = (task: TaskWithMeta): React.ReactElement => {
    const accept = acceptText(task);
    const editing = editingId === task.id;
    const dragBlocked = editing || savingId === task.id || dispatchingId === task.id;
    return (
      <div
        key={task.id}
        className={
          "wb-todo-card wb-todo-repo-card" +
          (shakeId === task.id ? " wb-todo-shake" : "") +
          (savingId === task.id ? " saving" : "")
        }
        draggable={!dragBlocked}
        onDragStart={(event) => {
          if (dragBlocked) {
            event.preventDefault();
            return;
          }
          event.dataTransfer.setData("text/plain", task.id);
          event.dataTransfer.effectAllowed = "move";
        }}
        onDragEnd={() => {
          setDropActive(false);
          setBucketDrop(null);
        }}
        title={task.title}
      >
        <div className="wb-todo-title">{task.title}</div>
        {editing ? (
          <input
            className="wb-todo-accept-input"
            autoFocus
            value={editText}
            placeholder="怎样算做完"
            onChange={(event) => setEditText(event.target.value)}
            onKeyDown={onAcceptKeyDown}
            onBlur={() => onAcceptBlur(task)}
          />
        ) : (
          <div
            className={"wb-todo-accept" + (accept ? "" : " empty")}
            role="button"
            tabIndex={0}
            onClick={() => startEdit(task)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                startEdit(task);
              }
            }}
          >
            {accept ? (
              <>
                <span className="wb-todo-accept-lab">验收 · </span>
                {accept}
              </>
            ) : (
              "补一句怎样算做完"
            )}
          </div>
        )}
        {warnId === task.id ? (
          <div className="wb-todo-warn">先补一句怎样算做完</div>
        ) : null}
      </div>
    );
  };

  if (!loaded) {
    return (
      <div className="wb-todo-wrap">
        <div className="wb-empty">{err || "加载中…"}</div>
      </div>
    );
  }

  return (
    <div className="wb-todo-wrap">
      {err ? <div className="wb-empty">{err}</div> : null}
      <ReceiptBar receipt={receipt} onDismiss={dismissReceipt} />

      <div className="wb-todo-grid">
        <section
          className={"wb-todo-col wb-todo-flow" + (dropActive ? " drop" : "")}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "move";
            setDropActive(true);
          }}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="wb-todo-chead">
            <span className="wb-todo-cname">流水线</span>
            <span className="wb-todo-count">{flowCount}</span>
            <span className="wb-todo-hint">拖进来才算派发</span>
          </div>

          <div className="wb-todo-body">
            {flowLists.map((segment) => {
              const openVerify = segment.key === "review" ? onVerify : undefined;
              return (
                <div className="wb-todo-seg" key={segment.key}>
                  <div className="wb-todo-seghead">
                    {openVerify ? (
                      <button
                        className="wb-todo-seghead-open"
                        type="button"
                        onClick={() => openVerify()}
                        title="进入全屏验收"
                      >
                        {segment.label}
                      </button>
                    ) : (
                      <span>{segment.label}</span>
                    )}
                    <span className="wb-todo-segline" />
                    <b className="wb-todo-segcount">{segment.cards.length}</b>
                  </div>
                  {segment.cards.map((task, index) => {
                    const accept = acceptText(task);
                    // 位次是派生值：ready 段内按 /board 顺序 1-based，每次渲染重算。
                    const position = segment.key === "ready" ? index + 1 : 0;
                    const openDetail = () => setDetailId(task.id);
                    return (
                      <div
                        className="wb-todo-card wb-todo-flow-card clickable"
                        key={task.id}
                        role="button"
                        tabIndex={0}
                        title="查看卡片详情"
                        onClick={openDetail}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            openDetail();
                          }
                        }}
                      >
                        <div className="wb-todo-flow-head">
                          <span className="wb-todo-title">{task.title}</span>
                          <PositionBadge status={task.status} position={position} />
                        </div>
                        <div className={"wb-todo-accept" + (accept ? "" : " empty")}>
                          {accept ? (
                            <>
                              <span className="wb-todo-accept-lab">验收 · </span>
                              {accept}
                            </>
                          ) : (
                            "未写验收"
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {segment.cards.length === 0 ? (
                    <div className="wb-todo-empty">
                      {segment.key === "ready"
                        ? "队列空"
                        : segment.key === "running"
                          ? "没有在跑的"
                          : "没有待收的"}
                    </div>
                  ) : null}
                </div>
              );
            })}

            <div className="wb-todo-dropempty">拖卡片到这里 = 派发</div>
          </div>
        </section>

        <section
          className={"wb-todo-col wb-todo-plane" + (bucketDrop === "today" ? " drop" : "")}
          onDragOver={(event) => handleBucketDragOver(event, "today")}
          onDragLeave={(event) => handleBucketDragLeave(event, "today")}
          onDrop={(event) => handleBucketDrop(event, "today")}
        >
          <div className="wb-todo-chead">
            <span className="wb-todo-cname">今日</span>
            <span className="wb-todo-count">{today.length}</span>
            <span className="wb-todo-hint">只存信息 · 可互拖</span>
          </div>
          <div className="wb-todo-body">
            <div className="wb-todo-list">{today.map(renderRepoCard)}</div>
            {today.length === 0 ? (
              <div className="wb-todo-empty">还没有今日卡</div>
            ) : null}
          </div>
        </section>

        <section
          className={"wb-todo-col wb-todo-plane" + (bucketDrop === "long" ? " drop" : "")}
          onDragOver={(event) => handleBucketDragOver(event, "long")}
          onDragLeave={(event) => handleBucketDragLeave(event, "long")}
          onDrop={(event) => handleBucketDrop(event, "long")}
        >
          <div className="wb-todo-chead">
            <span className="wb-todo-cname">长期</span>
            <span className="wb-todo-count">{long.length}</span>
            <span className="wb-todo-hint">冷存储 · 可互拖</span>
          </div>
          <div className="wb-todo-body">
            <div className="wb-todo-list">{long.map(renderRepoCard)}</div>
            {long.length === 0 ? <div className="wb-todo-empty">还没有长期卡</div> : null}
          </div>
        </section>
      </div>

      <div className="wb-todo-composer">
        <span className="wb-todo-composer-icon" aria-hidden="true">
          ✎
        </span>
        <div className="wb-todo-buckets" role="group" aria-label="新建卡归属">
          <button
            type="button"
            className={"wb-todo-bucket" + (draftBucket === "today" ? " on" : "")}
            disabled={creating}
            onClick={() => setDraftBucket("today")}
          >
            今日
          </button>
          <button
            type="button"
            className={"wb-todo-bucket" + (draftBucket === "long" ? " on" : "")}
            disabled={creating}
            onClick={() => setDraftBucket("long")}
          >
            长期
          </button>
        </div>
        <input
          ref={composerRef}
          value={draft}
          disabled={creating}
          placeholder="一句话记下来，回车新建"
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
            event.preventDefault();
            void submitDraft();
          }}
        />
        <span className="wb-todo-composer-hint">
          默认落{draftBucket === "today" ? "今日" : "长期"} <kbd>↵</kbd>
        </span>
      </div>

      {toast ? <div className="wb-toast">{toast}</div> : null}

      {detailTask ? (
        <TaskDetail
          task={detailTask}
          position={detailPosition || undefined}
          onClose={() => setDetailId(null)}
          onVerify={onVerify}
          onOpenSession={onOpenSession}
        />
      ) : null}
    </div>
  );
}
