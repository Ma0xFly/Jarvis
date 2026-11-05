import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api, KANBAN } from "../platform";
import { COLUMNS } from "../board/columns";
import type { BoardData, Task } from "../board/Board";

/**
 * 已完成（只读）：取看板 done 列，按完成时间（completed_at）的本地日期倒序分组。
 * 数据与看板同源（GET /api/plugins/kanban/board），不新增端点、不写任何状态。
 * 会话入口复用 App 的只读抽屉；会话缺失由查看器自己提示。
 */

const POLL_MS = 5000;
const DONE_KEY = COLUMNS.find((column) => column.key === "done")?.key ?? "done";

interface DoneGroup {
  key: string;
  label: string;
  tasks: Task[];
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** 本地日期 key（YYYY-MM-DD），跨年也不会把不同年份的同一月日混在一起。 */
function localDayKey(ts?: number | null): string {
  const date = ts ? new Date(ts * 1000) : new Date(0);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function localDayLabel(key: string): string {
  const [year, month, day] = key.split("-").map(Number);
  const now = new Date();
  const todayKey = localDayKey(now.getTime() / 1000);
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  const yesterdayKey = localDayKey(yesterday.getTime() / 1000);
  if (key === todayKey) return "今天";
  if (key === yesterdayKey) return "昨天";
  if (year === now.getFullYear()) return `${month} 月 ${day} 日`;
  return `${year} 年 ${month} 月 ${day} 日`;
}

function clockLabel(ts?: number | null): string {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

interface DonePageProps {
  /** 打开该轮执行会话（只读抽屉）；插件宿主不挂载本页。 */
  onOpenSession?: (taskId: string, title?: string) => void;
}

export default function DonePage({ onOpenSession }: DonePageProps): React.ReactElement {
  const [board, setBoard] = useState<BoardData | null>(null);
  const [err, setErr] = useState("");
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = (await api(KANBAN + "/board")) as Partial<BoardData> | null;
      if (!data || !Array.isArray(data.columns)) {
        throw new Error("board 响应缺少 columns");
      }
      setBoard({ columns: data.columns });
      setErr("");
    } catch (error) {
      setErr(error instanceof Error ? error.message : String(error));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const doneTasks = useMemo<Task[]>(() => {
    if (!board) return [];
    const column = board.columns.find((entry) => entry.name === DONE_KEY);
    return (column?.tasks || [])
      .slice()
      .sort(
        (a, b) =>
          (b.completed_at ?? 0) - (a.completed_at ?? 0) ||
          (a.id < b.id ? 1 : a.id > b.id ? -1 : 0),
      );
  }, [board]);

  const groups = useMemo<DoneGroup[]>(() => {
    const byDay = new Map<string, Task[]>();
    for (const task of doneTasks) {
      const key = task.completed_at ? localDayKey(task.completed_at) : "unknown";
      const list = byDay.get(key);
      if (list) list.push(task);
      else byDay.set(key, [task]);
    }
    return Array.from(byDay.entries()).map(([key, tasks]) => ({
      key,
      label: key === "unknown" ? "时间未知" : localDayLabel(key),
      tasks,
    }));
  }, [doneTasks]);

  const retry = useCallback(() => {
    setLoaded(false);
    setErr("");
    void load();
  }, [load]);

  if (!loaded) {
    return (
      <div className="wb-done-wrap">
        <div className="wb-done-empty" role="status">
          加载中…
        </div>
      </div>
    );
  }

  if (err) {
    return (
      <div className="wb-done-wrap">
        <div className="wb-done-error" role="alert">
          <div>kanban 不可达：{err}</div>
          <button className="wb-btn wb-done-retry" type="button" onClick={retry}>
            重试
          </button>
        </div>
      </div>
    );
  }

  if (doneTasks.length === 0) {
    return (
      <div className="wb-done-wrap">
        <div className="wb-done-empty">
          还没有已完成的卡
          <span className="wb-done-empty-sub">
            完成的卡会按完成日期出现在这里。
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="wb-done-wrap">
      {groups.map((group) => (
        <section className="wb-done-group" key={group.key} aria-label={group.label}>
          <h3 className="wb-done-day">
            <span>{group.label}</span>
            <span className="wb-done-rule" />
            <span className="wb-done-day-count">{group.tasks.length}</span>
          </h3>
          {group.tasks.map((task) => (
            <div className="wb-done-row" key={task.id} data-task-id={task.id}>
              <span className="wb-done-ok" aria-hidden="true">
                ✓
              </span>
              <div className="wb-done-main">
                <div className="wb-done-title">{task.title}</div>
                {task.latest_summary ? (
                  <div className="wb-done-summary">{task.latest_summary}</div>
                ) : null}
              </div>
              <span className="wb-done-time">{clockLabel(task.completed_at)}</span>
              {onOpenSession ? (
                <button
                  className="wb-done-session"
                  type="button"
                  data-task-id={task.id}
                  aria-label={"查看「" + task.title + "」的执行会话"}
                  title="查看该轮执行会话"
                  onClick={() => onOpenSession(task.id, task.title)}
                >
                  会话
                </button>
              ) : null}
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}