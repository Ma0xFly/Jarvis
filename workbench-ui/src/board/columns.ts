/**
 * 看板 8 列 —— 全库唯一的列定义。
 *
 * 列名、列序、status 字符串必须与 Hermes 原生看板逐项一致：
 *   原生 Triage / Todo / Scheduled / Ready / In Progress / Blocked / Review / Done
 *
 * 任何地方需要列名或列序，都从这里取；不要在别处再写一份 8 列数组，
 * 也不要在这里之外硬编码「待整理」这类中文列名。
 */
export type ColKey =
  | "triage"
  | "todo"
  | "scheduled"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done";

export interface Column {
  /** kanban 的 status 字符串，与原生一致 */
  key: ColKey;
  /** 展示名，与原生中文标签一致 */
  label: string;
  /** 列头小圆点颜色，走主题 token */
  color: string;
}

/** 唯一的列定义：顺序即原生顺序 */
export const COLUMNS: readonly Column[] = [
  { key: "triage", label: "待整理", color: "var(--wb-muted)" },
  { key: "todo", label: "待执行", color: "var(--wb-warn)" },
  { key: "scheduled", label: "已排期", color: "var(--wb-warn)" },
  { key: "ready", label: "就绪", color: "var(--wb-accent)" },
  { key: "running", label: "运行中", color: "var(--wb-ok)" },
  { key: "blocked", label: "待拍板", color: "var(--wb-danger)" },
  { key: "review", label: "待验收", color: "#8b5cf6" },
  { key: "done", label: "已完成", color: "var(--wb-muted)" },
] as const;

/** 由 COLUMNS 派生的查找表（不是第二份定义） */
export const COL_BY_KEY: Record<string, Column> = COLUMNS.reduce(
  (acc, col) => {
    acc[col.key] = col;
    return acc;
  },
  {} as Record<string, Column>,
);

export const COL_KEYS: ColKey[] = COLUMNS.map((c) => c.key);

/** 取展示名：未知 status 原样返回 */
export function colLabel(key: string): string {
  const col = COL_BY_KEY[key];
  return col ? col.label : key;
}

/**
 * 落列白名单：目标列 ← 允许的来源列。
 * running 不在任何白名单里 —— 它只能由调度器接管，不能手动拖入。
 */
export const ENTERABLE: Partial<Record<ColKey, ColKey[]>> = {
  done: ["running", "ready", "blocked", "review"],
  review: ["running", "ready"],
  blocked: ["running", "ready"],
  scheduled: ["todo", "ready", "running", "blocked"],
};

/** 被拒时的原因，列名全部从 COLUMNS 派生 */
export function enterHint(target: ColKey): string {
  switch (target) {
    case "done":
      return "先在待办页勾选完成，或先拖到「" + colLabel("ready") + "」再拖进来";
    case "review":
    case "blocked":
      return "「" + colLabel(target) + "」只能从「" + colLabel("ready") + " / " + colLabel("running") + "」进入";
    case "scheduled":
      return (
        "「" + colLabel("scheduled") + "」不能从「" + colLabel("triage") + "」直接进入，先拖到「" +
        colLabel("todo") + "」或「" + colLabel("ready") + "」"
      );
    default:
      return "";
  }
}