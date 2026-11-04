import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, KANBAN } from "../platform";
import { COLUMNS, COL_BY_KEY, colLabel, ENTERABLE, enterHint, type ColKey } from "./columns";

export interface Task {
  id: string;
  title: string;
  status: string;
  assignee?: string | null;
  priority?: number | null;
  block_kind?: string | null;
  latest_summary?: string | null;
  created_at?: number | null;
  completed_at?: number | null;
}

interface BoardCol {
  name: string;
  tasks: Task[];
}

interface BoardData {
  columns: BoardCol[];
}

const POLL_MS = 5000;

/** 相对时间，与原生卡片上的 "4h ago" 同义 */
function timeLabel(ts?: number | null): string {
  if (!ts) return "";
  const diff = Math.max(0, Date.now() / 1000 - ts);
  if (diff < 60) return "刚刚";
  if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 86400 * 7) return Math.floor(diff / 86400) + " 天前";
  const d = new Date(ts * 1000);
  return d.getMonth() + 1 + "/" + d.getDate();
}

function dayKey(ts?: number | null): string {
  if (!ts) return "earlier";
  const d = new Date(ts * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "today";
  return "earlier";
}

export default function Board(): React.ReactElement {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [err, setErr] = useState<string>("");
  const [dropCol, setDropCol] = useState<string | null>(null);
  const [toast, setToast] = useState<string>("");
  const toastTimer = useRef<number | null>(null);

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2600);
  }, []);

  const load = useCallback(async () => {
    try {
      const data = (await api(KANBAN + "/board")) as BoardData;
      setBoard(data);
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
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
    },
    [],
  );

  const tasks = useMemo<Task[]>(() => {
    if (!board || !board.columns) return [];
    return board.columns.reduce<Task[]>((acc, col) => acc.concat(col.tasks || []), []);
  }, [board]);

  const byCol = useMemo(() => {
    const map: Record<string, Task[]> = {};
    if (board && board.columns) {
      board.columns.forEach((col) => {
        map[col.name] = col.tasks || [];
      });
    }
    return map;
  }, [board]);

  const runningCount = byCol["running"] ? byCol["running"].length : 0;
  const doneTasks = byCol["done"] || [];

  /** 拖拽落列 = PATCH status，走原生状态机 */
  const flowDrop = useCallback(
    async (taskId: string, status: ColKey) => {
      const task = tasks.find((t) => t.id === taskId);
      if (!task) return;
      const cur = task.status as ColKey;

      if (status === "running") {
        showToast("运行中由调度器接管，不能手动设置");
        return;
      }
      if (cur === status) return;

      const from = ENTERABLE[status];
      if (cur && from && from.indexOf(cur) < 0) {
        showToast("不能从「" + colLabel(cur) + "」移到「" + colLabel(status) + "」：" + enterHint(status));
        return;
      }

      const body: Record<string, unknown> = { status };
      if (status === "done") body.summary = "从工作台完成";

      try {
        await api(KANBAN + "/tasks/" + taskId, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        showToast("已移到「" + colLabel(status) + "」");
        void load();
      } catch (e) {
        showToast((e instanceof Error ? e.message : String(e)) || "改状态失败");
      }
    },
    [tasks, showToast, load],
  );

  const onDragStart = (e: React.DragEvent, task: Task) => {
    e.dataTransfer.setData("text/plain", task.id);
    e.dataTransfer.effectAllowed = "move";
  };

  const renderCard = (t: Task) => {
    const col = COL_BY_KEY[t.status];
    return (
      <div
        key={t.id}
        className={"wb-card" + (t.status === "done" ? " done" : "")}
        draggable
        onDragStart={(e) => onDragStart(e, t)}
        title={t.title}
      >
        <div className="ct">{t.title}</div>
        <div className="cm">
          <span className="cid">{t.id}</span>
          {t.priority && t.priority > 0 ? <span className="wb-prio">P{t.priority}</span> : null}
          <span className="wb-tag">{t.assignee || "未指派"}</span>
          {t.block_kind ? <span className="wb-block">{t.block_kind}</span> : null}
          <span className="wb-time">{timeLabel(t.completed_at || t.created_at)}</span>
        </div>
        {t.latest_summary ? <div className="wb-sum">{t.latest_summary}</div> : null}
        {col && t.status === "running" ? <span className="wb-live" /> : null}
      </div>
    );
  };

  const startCount = (byCol["triage"] || []).length + (byCol["todo"] || []).length + (byCol["ready"] || []).length;
  const blockedCount = (byCol["blocked"] || []).length;
  const doneToday = doneTasks.filter((t) => dayKey(t.completed_at) === "today").length;

  return (
    <div className="wb-board-wrap">
      <div className="wb-stats">
        <span>
          运行中 <b>{runningCount}</b>
        </span>
        <span>
          今日完成 <b>{doneToday}</b>
        </span>
        <span>
          待拍板 <b>{blockedCount}</b>
        </span>
        <span>
          待启动 <b>{startCount}</b>
        </span>
        <span className="wb-hint">看板是流程视图：拖拽卡片换列 = 改状态</span>
        <button className="wb-btn" onClick={() => void load()} title="刷新">
          刷新
        </button>
      </div>

      {err ? <div className="wb-empty">kanban 不可达：{err}</div> : null}

      <div className="wb-board">
        {COLUMNS.map((col) => {
          const list = byCol[col.key] || [];
          return (
            <div
              key={col.key}
              className={"wb-col" + (dropCol === col.key ? " drop" : "")}
              onDragOver={(e) => {
                e.preventDefault();
                setDropCol(col.key);
              }}
              onDragLeave={() => setDropCol((c) => (c === col.key ? null : c))}
              onDrop={(e) => {
                e.preventDefault();
                setDropCol(null);
                const id = e.dataTransfer.getData("text/plain");
                if (id) void flowDrop(id, col.key);
              }}
            >
              <div className="wb-chead">
                <span className="wb-dot" style={{ background: col.color }} />
                <span className="wb-cname">{col.label}</span>
                <span className="wb-ccount">{list.length}</span>
              </div>
              {list.map(renderCard)}
              {list.length === 0 ? <div className="wb-dropempty">拖到这里</div> : null}
            </div>
          );
        })}
      </div>

      {toast ? <div className="wb-toast">{toast}</div> : null}
    </div>
  );
}