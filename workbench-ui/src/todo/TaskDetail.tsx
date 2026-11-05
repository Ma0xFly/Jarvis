import React, { useEffect, useRef } from "react";
import type { Task } from "../board/Board";
import type { TaskMetadata, WorkbenchContractBudget } from "../api/workbench";

/**
 * 流水线卡详情抽屉（只读 + 两个出口：去验收 / 查看执行会话）。
 * 数据来自待办页已经合并好的 board task + workbench metadata，不新增请求。
 */

const STATUS_LABEL: Record<string, string> = {
  triage: "待整理",
  todo: "待执行",
  scheduled: "已排期",
  ready: "就绪",
  running: "运行中",
  blocked: "待拍板",
  review: "待验收",
  done: "已完成",
};

function clock(ts?: number | null): string {
  if (!ts) return "—";
  const date = new Date(ts * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function budgetText(budget?: WorkbenchContractBudget | null): string {
  if (!budget || typeof budget !== "object") return "未设置";
  const parts: string[] = [];
  if (typeof budget.max_minutes === "number") parts.push(`${budget.max_minutes} 分钟`);
  if (typeof budget.max_cost === "number") parts.push(`¥${budget.max_cost}`);
  if (Array.isArray(budget.deny_paths) && budget.deny_paths.length > 0) {
    parts.push(`禁写 ${budget.deny_paths.join(", ")}`);
  }
  return parts.length > 0 ? parts.join(" · ") : "未设置";
}

export interface DetailTask extends Task {
  metadata?: TaskMetadata;
}

interface TaskDetailProps {
  task: DetailTask;
  /** ready 段的 1-based 位次；其他状态不传。 */
  position?: number;
  onClose: () => void;
  onVerify?: (taskId?: string) => void;
  onOpenSession?: (taskId: string, title?: string) => void;
}

export default function TaskDetail({
  task,
  position,
  onClose,
  onVerify,
  onOpenSession,
}: TaskDetailProps): React.ReactElement {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus && document.contains(previousFocus) && typeof previousFocus.focus === "function") {
        previousFocus.focus();
      }
    };
  }, []);

  const contract = task.metadata?.contract || {};
  const accept = typeof contract.accept === "string" ? contract.accept.trim() : "";
  const input = typeof contract.input === "string" ? contract.input.trim() : "";
  const budget = contract.budget;
  const status = STATUS_LABEL[task.status] || task.status;
  const sessionAvailable =
    task.status !== "ready" && task.status !== "triage" && task.status !== "todo";
  const canVerify = task.status === "review" && Boolean(onVerify);

  return (
    <div
      className="wb-detail-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        className="wb-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wb-detail-title"
      >
        <header className="wb-detail-head">
          <div className="wb-detail-headtext">
            <span className={"wb-detail-badge " + task.status}>{status}</span>
            {task.status === "ready" && position ? (
              <span className="wb-detail-position">队列第 {position} 位</span>
            ) : null}
            <h2 className="wb-detail-title" id="wb-detail-title">
              {task.title}
            </h2>
            <span className="wb-detail-id">{task.id}</span>
          </div>
          <button
            ref={closeRef}
            className="wb-detail-close"
            type="button"
            onClick={onClose}
            aria-label="关闭卡片详情"
            title="关闭（Esc）"
          >
            ✕
          </button>
        </header>

        <div className="wb-detail-body">
          <section className="wb-detail-section">
            <h3>验收标准</h3>
            <p className={"wb-detail-value" + (accept ? "" : " empty")}>
              {accept || "未写验收。派发前必须补一句怎样算做完。"}
            </p>
          </section>

          <section className="wb-detail-section">
            <h3>输入 / 上下文</h3>
            <p className={"wb-detail-value" + (input ? "" : " empty")}>
              {input || "未填写"}
            </p>
          </section>

          {task.latest_summary ? (
            <section className="wb-detail-section">
              <h3>最近结果</h3>
              <p className="wb-detail-value">{task.latest_summary}</p>
            </section>
          ) : null}

          <section className="wb-detail-section">
            <h3>执行信息</h3>
            <dl className="wb-detail-meta">
              <div>
                <dt>执行者</dt>
                <dd>{task.assignee || "未分配"}</dd>
              </div>
              <div>
                <dt>优先级</dt>
                <dd>{task.priority ? `P${task.priority}` : "—"}</dd>
              </div>
              <div>
                <dt>创建</dt>
                <dd>{clock(task.created_at)}</dd>
              </div>
              <div>
                <dt>完成</dt>
                <dd>{clock(task.completed_at)}</dd>
              </div>
              <div>
                <dt>预算</dt>
                <dd>{budgetText(budget)}</dd>
              </div>
              {task.block_kind ? (
                <div>
                  <dt>阻塞</dt>
                  <dd>{task.block_kind}</dd>
                </div>
              ) : null}
            </dl>
          </section>
        </div>

        <footer className="wb-detail-actions">
          {canVerify ? (
            <button
              className="wb-detail-btn primary"
              type="button"
              onClick={() => {
                onVerify?.(task.id);
                onClose();
              }}
            >
              去验收
            </button>
          ) : null}
          {onOpenSession && sessionAvailable ? (
            <button
              className="wb-detail-btn"
              type="button"
              onClick={() => onOpenSession(task.id, task.title)}
            >
              查看执行会话
            </button>
          ) : (
            <span className="wb-detail-session-hint">
              {task.status === "ready" || task.status === "todo" || task.status === "triage"
                ? "执行后才有会话"
                : ""}
            </span>
          )}
          <button className="wb-detail-btn" type="button" onClick={onClose}>
            关闭
          </button>
        </footer>
      </aside>
    </div>
  );
}