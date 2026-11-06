import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, isPluginHost, KANBAN } from "../platform";
import { dispatchTask, WorkbenchError } from "../api/workbench";
import { COLUMNS, colLabel, ENTERABLE, enterHint, type ColKey } from "./columns";

/** 插件宿主没有 /api/workbench/*：就绪列只读，派发必须在独立版 8787 完成。 */
const PLUGIN_READONLY_NOTICE = "插件宿主只读；请在 127.0.0.1:8787 独立版派发";

/** 把派发闸门码映射成与待办页一致的中文提示，不泄露英文 error 码。 */
function gateMessage(error: unknown): string {
  if (error instanceof WorkbenchError) {
    switch (error.code) {
      case "missing_acceptance":
        return "先补一句怎样算做完";
      case "blocked_by_parent":
        return "已回到仓库，等 parent 完成";
      case "not_dispatchable":
        return "已完成或归档的卡不能派发";
      case "not_ready":
        return "卡没有进入队列，请刷新后重试";
      case "dispatch_timeout":
        return "派发超时，请刷新后重试";
      default:
        break;
    }
  }
  return "派发失败：" + (error instanceof Error ? error.message : String(error));
}

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

export interface BoardCol {
  name: string;
  tasks: Task[];
}

export interface BoardData {
  columns: BoardCol[];
}

const POLL_MS = 5000;

interface BoardProps {
  /** 独立版：打开已完成卡本轮执行会话（插件宿主不传）。 */
  onOpenSession?: (taskId: string, title?: string) => void;
  /** 独立版外壳计数：运行中 / 已完成。插件宿主不传。 */
  onStats?: (stats: { running: number; done: number }) => void;
}

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

export default function Board({ onOpenSession, onStats }: BoardProps): React.ReactElement {
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

  // 外壳计数由看板这一份轮询数据提供，不新增端点。
  useEffect(() => {
    onStats?.({ running: runningCount, done: doneTasks.length });
  }, [runningCount, doneTasks.length, onStats]);

  /** 拖拽落列：就绪走 dispatch（快照/位次），其余走原生 PATCH 状态机 */
  const flowDrop = useCallback(
    async (taskId: string, status: ColKey) => {
      const task = tasks.find((t) => t.id === taskId);
      if (!task) return;
      const cur = task.status as ColKey;

      if (status === "running") {
        showToast("运行中由调度器接管，不能手动设置");
        return;
      }
      if (status === "ready" && isPluginHost) {
        showToast(PLUGIN_READONLY_NOTICE);
        return;
      }
      if (cur === status) return;

      // 仓库卡不能从看板移回待整理/待执行：triage 会被 triage specifier
      // 接走，todo 会被 Hermes recompute_ready 自动提升为 ready。允许
      // todo -> scheduled（停车），不允许反向。
      if (
        !isPluginHost &&
        (status === "triage" || status === "todo") &&
        (cur === "triage" || cur === "todo" || cur === "scheduled")
      ) {
        showToast(
          "仓库卡不能移到「" + colLabel(status) + "」；请从待办页拖入流水线",
        );
        return;
      }

      const from = ENTERABLE[status];
      if (cur && from && from.indexOf(cur) < 0) {
        showToast("不能从「" + colLabel(cur) + "」移到「" + colLabel(status) + "」：" + enterHint(status));
        return;
      }

      // 独立版拖到就绪 = 派发：必须走 dispatchTask，才能写快照/位次并过闸门。
      // 其他列仍是原生 PATCH 状态机。
      if (status === "ready") {
        try {
          await dispatchTask(taskId);
          showToast("已派发");
          void load();
        } catch (e) {
          showToast(gateMessage(e));
        }
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
    const time = timeLabel(t.completed_at || t.created_at);
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
          {t.assignee ? <span className="wb-tag">{t.assignee}</span> : null}
          {t.block_kind ? <span className="wb-block">{t.block_kind}</span> : null}
          {t.status === "done" && onOpenSession ? (
            <button
              className="wb-card-session"
              type="button"
              draggable={false}
              data-task-id={t.id}
              title="查看该轮执行会话"
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                onOpenSession(t.id, t.title);
              }}
            >
              会话
            </button>
          ) : null}
          {time ? <span className="wb-time">{time}</span> : null}
        </div>
        {t.latest_summary ? <div className="wb-sum">{t.latest_summary}</div> : null}
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
          {colLabel("blocked")} <b>{blockedCount}</b>
        </span>
        <span>
          待启动 <b>{startCount}</b>
        </span>
        <span className="wb-hint">
          {isPluginHost ? PLUGIN_READONLY_NOTICE : "看板是流程视图：拖拽卡片换列 = 改状态"}
        </span>
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
              title={isPluginHost && col.key === "ready" ? PLUGIN_READONLY_NOTICE : undefined}
              onDragOver={(e) => {
                // 插件宿主就绪列不是投放目标：不 preventDefault 就不会触发 drop。
                if (isPluginHost && col.key === "ready") return;
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