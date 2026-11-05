import React from "react";

/**
 * 一次派发的回执状态。pending 在 drop 时立即出现，dispatch 响应后替换为
 * final。回执只透传后端 position/eta：不重算 ETA、不持久化，6 秒后自动消失。
 * 多次派发采用 latest-attempt 语义：新尝试的 pending 会替换上一条成功回执。
 */
export interface PendingReceipt {
  kind: "pending";
  /** 尝试序号；关闭当前回执后，同一次尝试的最终结果不再显示。 */
  seq: number;
}

export interface ReceiptData {
  kind: "final";
  seq: number;
  /** 1-based ready 队列位次；0 表示卡已在运行 */
  position: number;
  /** dispatch 返回的 HH:MM；position 0 时为 null */
  eta: string | null;
}

export type ReceiptState = PendingReceipt | ReceiptData;

/**
 * 回执文案。position 必须是有限非负整数：
 * - 0 → 「已在运行」
 * - >0 且有 ETA → 「已入队 · 第 N 位 · 预计 HH:MM」
 * - >0 且 eta 为 null → 「已入队 · 第 N 位」
 * 非法值（NaN / Infinity / 负数 / 小数）返回 null，不渲染谎报文案。
 */
export function receiptText(receipt: Pick<ReceiptData, "position" | "eta">): string | null {
  const { position, eta } = receipt;
  if (!Number.isFinite(position) || !Number.isInteger(position) || position < 0) {
    return null;
  }
  if (position === 0) return "已在运行";
  return eta
    ? `已入队 · 第 ${position} 位 · 预计 ${eta}`
    : `已入队 · 第 ${position} 位`;
}

/**
 * 顶部回执条。外层 live region 常驻（不随状态挂载/卸载），只替换文本子节点；
 * 槽位始终保留高度，出现/消失不会让三列重新排版。
 */
export function ReceiptBar({
  receipt,
  onDismiss,
}: {
  receipt: ReceiptState | null;
  onDismiss: () => void;
}): React.ReactElement {
  const pending = receipt?.kind === "pending";
  const final = receipt?.kind === "final" ? receipt : null;
  const finalText = final ? receiptText(final) : null;
  const text = pending ? "正在派发…" : finalText ?? "";
  const visible = text !== "";
  const running = final !== null && finalText !== null && final.position === 0;
  const className =
    "wb-todo-receipt" +
    (visible ? " visible" : "") +
    (pending ? " pending" : "") +
    (running ? " running" : "");

  return (
    <div className="wb-todo-receipt-slot">
      <div
        className={className}
        role="status"
        aria-live="polite"
        aria-atomic="true"
        aria-busy={pending || undefined}
      >
        <span className="wb-todo-receipt-dot" aria-hidden="true" />
        <span className="wb-todo-receipt-text">{text}</span>
        <button
          type="button"
          className="wb-todo-receipt-dismiss"
          aria-label="关闭派发回执"
          aria-hidden={!visible}
          disabled={!visible}
          tabIndex={visible ? 0 : -1}
          onClick={onDismiss}
        >
          ×
        </button>
      </div>
    </div>
  );
}

/**
 * 流水线卡片的位次徽标：ready 显示按 `/board` 顺序的 1-based 位次，
 * running 显示「已在运行」。位次是派生值，每次渲染/轮询重算。
 */
export function PositionBadge({
  status,
  position,
}: {
  status: string;
  position: number;
}): React.ReactElement | null {
  if (status === "running") {
    return <span className="wb-todo-badge running">已在运行</span>;
  }
  if (status === "ready" && position > 0) {
    return <span className="wb-todo-badge">第 {position} 位</span>;
  }
  return null;
}
