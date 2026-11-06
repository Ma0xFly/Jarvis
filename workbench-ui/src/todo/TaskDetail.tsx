import React, { useEffect, useRef, useState } from "react";
import type { Task } from "../board/Board";
import {
  WorkbenchError,
  type TaskMetadata,
  type WakeResult,
  type WorkbenchContractBudget,
} from "../api/workbench";

/**
 * 卡片详情抽屉：流水线卡只读 + 两个出口（去验收 / 查看执行会话）；
 * 仓库卡（triage/todo/scheduled）可编辑标题、验收与上下文。
 * 数据来自待办页已经合并好的 board task + workbench metadata，不新增读取请求。
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

const STORE_STATUSES = new Set(["triage", "todo", "scheduled"]);

function clock(ts?: number | null): string {
  // Epoch 0 is a real instant (and a valid wake_at), so only null/undefined
  // and non-finite values are "no time".
  if (ts === undefined || ts === null || !Number.isFinite(ts)) return "—";
  const date = new Date(ts * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

/** epoch seconds → `<input type="datetime-local">` 的本地时间值。 */
function wakeAtValue(wakeAt: number | null): string {
  if (wakeAt === null) return "";
  const date = new Date(wakeAt * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}` +
    `T${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  );
}

/** datetime-local 按浏览器本地时区解析；空/非法值返回 null。 */
function wakeAtFromInput(value: string): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return null;
  const seconds = Math.floor(parsed / 1000);
  return seconds >= 0 ? seconds : null;
}

function wakeErrorText(code: string): string {
  if (code === "missing_acceptance") return "缺验收，暂时无法自动唤醒";
  if (code === "upstream_unavailable") return "上游暂不可用，稍后自动重试";
  if (code === "invalid_defaults") return "派发默认值配置有误，先检查 defaults.json";
  if (code === "not_ready") return "上游暂时不让派发，稍后自动重试";
  return "唤醒失败：" + code;
}

function wakeActionError(error: unknown): string {
  if (error instanceof WorkbenchError) {
    if (error.code === "missing_acceptance") {
      return "缺验收，补一句怎样算做完再唤醒";
    }
    if (error.code === "not_in_store") return "卡已离开仓库，刷新后重试";
    if (error.code === "not_found") return "卡已不存在，刷新后重试";
    if (error.code === "metadata_unavailable") {
      return "本地 metadata 暂不可用，稍后重试";
    }
    if (error.code === "upstream_unavailable") return "上游暂不可用，稍后重试";
    if (error.code === "invalid_defaults") return "派发默认值配置有误，先检查 defaults.json";
    if (error.code === "not_ready") return "上游暂时不让派发，稍后重试";
    return "唤醒失败：" + error.message;
  }
  return "唤醒失败：" + errText(error);
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

function errText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export interface DetailTask extends Task {
  metadata?: TaskMetadata;
}

export interface DetailEditPatch {
  title: string;
  input: string;
  accept: string;
}

interface TaskDetailProps {
  task: DetailTask;
  /** ready 段的 1-based 位次；其他状态不传。 */
  position?: number;
  onClose: () => void;
  onVerify?: (taskId?: string) => void;
  onOpenSession?: (taskId: string, title?: string) => void;
  /** 仓库卡编辑保存；不传则整张卡只读。 */
  onSave?: (patch: DetailEditPatch) => Promise<void>;
  /** 设置/清除唤醒时间；null 表示清除。 */
  onWakeChange?: (wakeAt: number | null) => Promise<void>;
  /** 立即唤醒（与后台到点共用同一后端路径）。 */
  onWakeNow?: () => Promise<WakeResult | void>;
}

export default function TaskDetail({
  task,
  position,
  onClose,
  onVerify,
  onOpenSession,
  onSave,
  onWakeChange,
  onWakeNow,
}: TaskDetailProps): React.ReactElement {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const editingRef = useRef(false);

  const contract = task.metadata?.contract || {};
  const accept = typeof contract.accept === "string" ? contract.accept.trim() : "";
  const input = typeof contract.input === "string" ? contract.input.trim() : "";
  const budget = contract.budget;
  const status = STATUS_LABEL[task.status] || task.status;
  const canEdit = STORE_STATUSES.has(task.status) && Boolean(onSave);
  const sessionAvailable =
    task.status !== "ready" &&
    task.status !== "triage" &&
    task.status !== "todo" &&
    task.status !== "scheduled";
  const canVerify = task.status === "review" && Boolean(onVerify);

  const metadataWakeAt =
    typeof task.metadata?.wake_at === "number" &&
    Number.isFinite(task.metadata.wake_at) &&
    task.metadata.wake_at >= 0
      ? task.metadata.wake_at
      : null;
  const metadataWakeError =
    typeof task.metadata?.wake_error === "string" ? task.metadata.wake_error : "";
  const canWake =
    STORE_STATUSES.has(task.status) && (Boolean(onWakeChange) || Boolean(onWakeNow));

  const [editing, setEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState(task.title);
  const [acceptDraft, setAcceptDraft] = useState(accept);
  const [inputDraft, setInputDraft] = useState(input);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [wakeDraft, setWakeDraft] = useState(wakeAtValue(metadataWakeAt));
  const [wakeSaving, setWakeSaving] = useState(false);
  const [wakeError, setWakeError] = useState("");
  const [wakeNotice, setWakeNotice] = useState("");
  editingRef.current = editing;

  // Polling keeps replacing the task object; never clobber an active edit.
  useEffect(() => {
    if (editing) return;
    setTitleDraft(task.title);
    setAcceptDraft(accept);
    setInputDraft(input);
  }, [task.id, task.title, accept, input, editing]);

  // Sync the wake input with server truth (after set/clear or a poll update),
  // but never while the user is typing in the title/accept editor.
  useEffect(() => {
    setWakeDraft(wakeAtValue(metadataWakeAt));
  }, [task.id, metadataWakeAt]);

  // The card may leave the store (dispatch) while the drawer is open.
  useEffect(() => {
    if (!canEdit && editing) {
      setEditing(false);
      setSaveError("");
    }
  }, [canEdit, editing]);

  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (editingRef.current) {
        setEditing(false);
        setSaveError("");
      } else {
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

  const beginEdit = () => {
    setTitleDraft(task.title);
    setAcceptDraft(accept);
    setInputDraft(input);
    setSaveError("");
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setSaveError("");
  };

  const submitEdit = async () => {
    if (saving || !onSave) return;
    const nextTitle = titleDraft.trim();
    if (!nextTitle) {
      setSaveError("标题不能为空");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      await onSave({ title: nextTitle, input: inputDraft, accept: acceptDraft });
      setEditing(false);
    } catch (error) {
      setSaveError("保存失败：" + errText(error));
    } finally {
      setSaving(false);
    }
  };

  const runWake = async (action: () => Promise<WakeResult | void>) => {
    if (wakeSaving) return;
    setWakeSaving(true);
    setWakeError("");
    setWakeNotice("");
    try {
      await action();
    } catch (error) {
      setWakeError(wakeActionError(error));
    } finally {
      setWakeSaving(false);
    }
  };

  const saveWake = () => {
    if (!onWakeChange) return;
    const wakeAt = wakeAtFromInput(wakeDraft);
    if (wakeAt === null) {
      setWakeError("选择有效的唤醒时间");
      return;
    }
    void runWake(() => onWakeChange(wakeAt));
  };

  const clearWake = () => {
    if (!onWakeChange) return;
    void runWake(() => onWakeChange(null));
  };

  const wakeNow = () => {
    if (!onWakeNow) return;
    void runWake(async () => {
      const result = await onWakeNow();
      if (!result) return;
      if (result.status === "todo") {
        setWakeNotice("有未完成 parent，已转入待执行");
      } else if (typeof result.position === "number") {
        setWakeNotice(
          `已唤醒，队列第 ${result.position} 位` +
            (result.eta ? `，预计 ${result.eta}` : ""),
        );
      } else {
        setWakeNotice("已唤醒");
      }
    });
  };

  const quickWake = () => {
    setWakeError("");
    // datetime-local drops seconds, so round up to the next minute: flooring
    // would turn "5 minutes" into as little as 4m01s in the stored epoch.
    const target = Math.ceil((Date.now() / 1000 + 5 * 60) / 60) * 60;
    setWakeDraft(wakeAtValue(target));
  };

  return (
    <div
      className="wb-detail-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          if (editing) cancelEdit();
          else onClose();
        }
      }}
    >
      <aside
        className={"wb-detail-drawer" + (editing ? " editing" : "")}
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
              {editing ? titleDraft.trim() || task.title : task.title}
            </h2>
            <span className="wb-detail-id">{task.id}</span>
          </div>
          <button
            ref={closeRef}
            className="wb-detail-close"
            type="button"
            onClick={editing ? cancelEdit : onClose}
            aria-label={editing ? "取消编辑" : "关闭卡片详情"}
            title={editing ? "取消编辑（Esc）" : "关闭（Esc）"}
          >
            ✕
          </button>
        </header>

        <div className="wb-detail-body">
          {editing ? (
            <>
              <section className="wb-detail-section">
                <h3>标题</h3>
                <input
                  className="wb-detail-input"
                  value={titleDraft}
                  maxLength={500}
                  autoFocus
                  onChange={(event) => setTitleDraft(event.target.value)}
                  placeholder="要做什么？"
                />
              </section>

              <section className="wb-detail-section">
                <h3>验收标准</h3>
                <textarea
                  className="wb-detail-textarea"
                  rows={4}
                  maxLength={2000}
                  value={acceptDraft}
                  onChange={(event) => setAcceptDraft(event.target.value)}
                  placeholder="怎样算做完？派发前必须有一句。"
                />
              </section>

              <section className="wb-detail-section">
                <h3>输入 / 上下文</h3>
                <textarea
                  className="wb-detail-textarea"
                  rows={4}
                  maxLength={4000}
                  value={inputDraft}
                  onChange={(event) => setInputDraft(event.target.value)}
                  placeholder="背景、链接、约束（可选）"
                />
              </section>
            </>
          ) : (
            <>
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
            </>
          )}

          {canWake && !editing ? (
            <section className="wb-detail-section wb-detail-wake">
              <h3>定时唤醒</h3>
              <div className="wb-detail-wake-row">
                <input
                  className="wb-detail-wake-input"
                  type="datetime-local"
                  value={wakeDraft}
                  disabled={wakeSaving}
                  aria-label="唤醒时间"
                  onChange={(event) => {
                    setWakeDraft(event.target.value);
                    setWakeError("");
                  }}
                />
                <button
                  className="wb-detail-btn"
                  type="button"
                  onClick={quickWake}
                  disabled={wakeSaving}
                >
                  5 分钟后
                </button>
                <button
                  className="wb-detail-btn"
                  type="button"
                  onClick={saveWake}
                  disabled={wakeSaving || !onWakeChange}
                >
                  设置
                </button>
                <button
                  className="wb-detail-btn"
                  type="button"
                  onClick={clearWake}
                  disabled={wakeSaving || !onWakeChange || metadataWakeAt === null}
                >
                  清除
                </button>
                <button
                  className="wb-detail-btn"
                  type="button"
                  onClick={wakeNow}
                  disabled={wakeSaving || !onWakeNow}
                >
                  提前唤醒
                </button>
              </div>
              <p className="wb-detail-wake-hint">
                {metadataWakeAt !== null
                  ? `已排期：${clock(metadataWakeAt)}`
                  : "未设置；也可以直接提前唤醒。"}
              </p>
              {wakeNotice ? (
                <p className="wb-detail-wake-ok" role="status">
                  {wakeNotice}
                </p>
              ) : null}
              {wakeError ? (
                <div className="wb-detail-wake-error" role="alert">
                  {wakeError}
                </div>
              ) : metadataWakeError ? (
                <div className="wb-detail-wake-error" role="alert">
                  {wakeErrorText(metadataWakeError)}
                </div>
              ) : null}
            </section>
          ) : null}

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

          {saveError ? (
            <div className="wb-detail-error" role="alert">
              {saveError}
            </div>
          ) : null}
        </div>

        <footer className="wb-detail-actions">
          {editing ? (
            <>
              <button
                className="wb-detail-btn primary"
                type="button"
                onClick={() => void submitEdit()}
                disabled={saving}
              >
                {saving ? "保存中…" : "保存"}
              </button>
              <button
                className="wb-detail-btn"
                type="button"
                onClick={cancelEdit}
                disabled={saving}
              >
                取消
              </button>
              <span className="wb-detail-session-hint">Esc 取消编辑</span>
            </>
          ) : (
            <>
              {canEdit ? (
                <button className="wb-detail-btn" type="button" onClick={beginEdit}>
                  编辑
                </button>
              ) : null}
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
                  {sessionAvailable ? "" : "执行后才有会话"}
                </span>
              )}
              <button className="wb-detail-btn" type="button" onClick={onClose}>
                关闭
              </button>
            </>
          )}
        </footer>
      </aside>
    </div>
  );
}