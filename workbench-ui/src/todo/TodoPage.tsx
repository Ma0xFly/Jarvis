import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, KANBAN } from "../platform";
import type { Task } from "../board/Board";
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

interface BoardColumn {
  name: string;
  tasks: Task[];
}

interface BoardData {
  columns: BoardColumn[];
}

type TaskWithMeta = Task & { metadata?: TaskMetadata };

const POLL_MS = 5000;

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

/** 仓库卡：triage + 对应 bucket；固定 created_at 倒序，不做栏内拖动 */
function repoCards(tasks: TaskWithMeta[], bucket: Bucket): TaskWithMeta[] {
  return tasks
    .filter((task) => task.status === "triage" && task.metadata?.bucket === bucket)
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

export default function TodoPage(): React.ReactElement {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [metadataById, setMetadataById] = useState<Record<string, TaskMetadata>>({});
  const [err, setErr] = useState("");
  const [dropActive, setDropActive] = useState(false);
  const [toast, setToast] = useState("");
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

  const toastTimer = useRef<number | null>(null);
  const shakeTimer = useRef<number | null>(null);
  const warnTimer = useRef<number | null>(null);
  const cancelEditRef = useRef(false);
  // A poll that started before a local write must not overwrite that write.
  const loadSeq = useRef(0);
  const mutationSeq = useRef(0);
  const savingCardsRef = useRef<Set<string>>(new Set());
  const dispatchPendingRef = useRef<Set<string>>(new Set());
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

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    const mutation = mutationSeq.current;
    const superseded = () => seq !== loadSeq.current || mutation !== mutationSeq.current;
    try {
      const data = (await api(KANBAN + "/board")) as BoardData;
      if (superseded()) return;
      const columns = Array.isArray(data.columns) ? data.columns : [];
      setBoard({ columns });
      setLoaded(true);
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
    } catch (error) {
      if (superseded()) return;
      setErr("kanban 不可达：" + errText(error));
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

  const flowLists = FLOW_SEGMENTS.map((segment) => ({
    ...segment,
    cards: tasks.filter((task) => task.status === segment.key),
  }));
  const flowCount = flowLists.reduce((sum, segment) => sum + segment.cards.length, 0);

  const dispatchToFlow = useCallback(
    async (task: TaskWithMeta) => {
      // Ignore repeated drops while this card's dispatch is in flight.
      if (dispatchPendingRef.current.has(task.id)) return;
      dispatchPendingRef.current.add(task.id);
      setDispatchingId(task.id);
      try {
        await dispatchTask(task.id);
        showToast("已派发");
      } catch (error) {
        if (error instanceof WorkbenchError && error.code === "blocked_by_parent") {
          showToast("已转为 todo，等 parent 完成");
        } else if (error instanceof WorkbenchError && error.code === "missing_acceptance") {
          flashWarn(task.id);
          return;
        } else {
          showToast("派发失败：" + errText(error));
        }
      } finally {
        dispatchPendingRef.current.delete(task.id);
        setDispatchingId(null);
        mutationSeq.current += 1;
        void load();
      }
    },
    [showToast, flashWarn, load],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLElement>) => {
      event.preventDefault();
      setDropActive(false);
      const taskId = event.dataTransfer.getData("text/plain");
      if (!taskId) return;
      const task = tasks.find((candidate) => candidate.id === taskId);
      if (!task) return;
      // 客户端闸门：验收为空时零请求，抖动 + 红字后卡回原位。
      if (!hasAcceptance(task.metadata)) {
        flashWarn(task.id);
        return;
      }
      void dispatchToFlow(task);
    },
    [tasks, flashWarn, dispatchToFlow],
  );

  const handleDragLeave = (event: React.DragEvent<HTMLElement>) => {
    const next = event.relatedTarget as Node | null;
    if (!next || !event.currentTarget.contains(next)) setDropActive(false);
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
        onDragEnd={() => setDropActive(false)}
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
            {accept || "补一句怎样算做完"}
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
          </div>

          {flowLists.map((segment) => (
            <div className="wb-todo-seg" key={segment.key}>
              <div className="wb-todo-seghead">
                <span>{segment.label}</span>
                <span>{segment.cards.length}</span>
              </div>
              {segment.cards.map((task) => {
                const accept = acceptText(task);
                return (
                  <div className="wb-todo-card wb-todo-flow-card" key={task.id}>
                    <div className="wb-todo-title">{task.title}</div>
                    <div className={"wb-todo-accept" + (accept ? "" : " empty")}>
                      {accept || "未写验收"}
                    </div>
                  </div>
                );
              })}
            </div>
          ))}

          {flowCount === 0 ? <div className="wb-todo-dropempty">拖到这里派发</div> : null}
        </section>

        <section className="wb-todo-col wb-todo-plane">
          <div className="wb-todo-chead">
            <span className="wb-todo-cname">今日</span>
            <span className="wb-todo-count">{today.length}</span>
          </div>
          <div className="wb-todo-list">{today.map(renderRepoCard)}</div>
          {today.length === 0 ? <div className="wb-todo-empty">还没有今日卡</div> : null}
        </section>

        <section className="wb-todo-col wb-todo-plane">
          <div className="wb-todo-chead">
            <span className="wb-todo-cname">长期</span>
            <span className="wb-todo-count">{long.length}</span>
          </div>
          <div className="wb-todo-list">{long.map(renderRepoCard)}</div>
          {long.length === 0 ? <div className="wb-todo-empty">还没有长期卡</div> : null}
        </section>
      </div>

      <div className="wb-todo-composer">
        <span className="wb-todo-composer-label">新建到</span>
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
      </div>

      {toast ? <div className="wb-toast">{toast}</div> : null}
    </div>
  );
}