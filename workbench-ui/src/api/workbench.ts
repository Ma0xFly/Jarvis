/**
 * 工作台自有 API（/api/workbench/*）：派发闸门与契约 metadata。
 *
 * 预算数值绝不在这里写死 —— 生效值由后端在派发瞬间从
 * workbench-app/defaults.json + 卡上 contract.budget 合并后快照。
 */
import { api } from "../platform";

/** workbench 自有端点的基址（与 kanban 上游 REST 分开） */
export const WORKBENCH = "/api/workbench";

export interface WorkbenchContractBudget {
  max_minutes?: number;
  max_cost?: number;
  deny_paths?: string[];
}

export interface WorkbenchContract {
  input?: string;
  accept?: string;
  ready?: boolean;
  budget?: WorkbenchContractBudget;
  [key: string]: unknown;
}

export interface ContractDefaults {
  max_minutes: number;
  max_cost: number;
  deny_paths: string[];
}

/** workbench.db 中按 task_id 存的一份 metadata（不会出现在上游 /board 卡上） */
export interface TaskMetadata {
  bucket?: Bucket;
  contract?: WorkbenchContract;
  contract_defaults?: ContractDefaults;
  [key: string]: unknown;
}

export type WorkbenchErrorCode =
  | "missing_acceptance"
  | "blocked_by_parent"
  | "not_ready"
  | "not_dispatchable"
  | "not_found"
  | "invalid_defaults"
  | "invalid_title"
  | "invalid_bucket"
  | "invalid_metadata"
  | "invalid_contract"
  | "invalid_request"
  | "unsupported_media_type"
  | "forbidden_origin"
  | "payload_too_large"
  | "too_many_ids"
  | "gate_unavailable"
  | "metadata_unavailable"
  | "upstream_unavailable"
  | "dispatch_failed"
  | "dispatch_timeout"
  | "create_failed"
  | "metadata_failed"
  | "not_in_review"
  | "reason_required"
  | "reason_too_long"
  | "stale_review"
  | "invalid_action"
  | "verify_failed"
  | "inbox_failed"
  | "sweep_failed"
  | "notification_failed";

const ERROR_CODES: readonly WorkbenchErrorCode[] = [
  "missing_acceptance",
  "blocked_by_parent",
  "not_ready",
  "not_dispatchable",
  "not_found",
  "invalid_defaults",
  "invalid_title",
  "invalid_bucket",
  "invalid_metadata",
  "invalid_contract",
  "invalid_request",
  "unsupported_media_type",
  "forbidden_origin",
  "payload_too_large",
  "too_many_ids",
  "gate_unavailable",
  "metadata_unavailable",
  "upstream_unavailable",
  "dispatch_failed",
  "dispatch_timeout",
  "create_failed",
  "metadata_failed",
  "not_in_review",
  "reason_required",
  "reason_too_long",
  "stale_review",
  "invalid_action",
  "verify_failed",
  "inbox_failed",
  "sweep_failed",
  "notification_failed",
];

/** 后端错误码优先；宿主 SDK 只给 message 时按文本兜底，最后按 HTTP 状态兜底 */
export class WorkbenchError extends Error {
  readonly code: WorkbenchErrorCode;
  readonly status?: number;

  constructor(code: WorkbenchErrorCode, message: string, status?: number) {
    super(message || code);
    this.name = "WorkbenchError";
    this.code = code;
    this.status = status;
  }
}

export interface DispatchResult {
  ok: true;
  /** 1-based ready 队列位次；running 卡重派返回 0 */
  position: number;
  /** HH:MM；running 卡没有排队时间 */
  eta: string | null;
  status: string;
}

/** 仓库卡归属：只允许 today / long，不占用 todo */
export type Bucket = "today" | "long";

export interface CreatedTask {
  id: string;
  title: string;
  status: string;
  created_at?: number | null;
  [key: string]: unknown;
}

export interface CreateTaskResult {
  ok: true;
  task: CreatedTask;
  metadata: TaskMetadata;
}

export interface TaskMetadataPatch {
  bucket?: Bucket;
  contract?: WorkbenchContract;
}

interface ApiErrorLike extends Error {
  code?: unknown;
  status?: unknown;
}

function isWorkbenchCode(value: unknown): value is WorkbenchErrorCode {
  return typeof value === "string" && (ERROR_CODES as readonly string[]).includes(value);
}

type ErrorContext =
  | "dispatch"
  | "create"
  | "metadata"
  | "verify"
  | "inbox"
  | "sweep"
  | "notifications";

/**
 * HTTP 状态兜底只在没有后端 error 码时使用。400 的含义随调用而变：
 * 只有 dispatch 是 missing_acceptance，create/save 一律按 invalid_* 处理。
 */
function fallbackCode(status: number | undefined, context: ErrorContext): WorkbenchErrorCode {
  switch (status) {
    case 400:
      return context === "dispatch" ? "missing_acceptance" : "invalid_request";
    case 404:
      return "not_found";
    case 409:
      return "not_in_review";
    case 500:
      if (context === "dispatch") return "invalid_defaults";
      if (context === "create") return "create_failed";
      if (context === "verify") return "verify_failed";
      if (context === "inbox") return "inbox_failed";
      if (context === "sweep") return "sweep_failed";
      if (context === "notifications") return "notification_failed";
      return "metadata_failed";
    case 502:
      return "upstream_unavailable";
    case 503:
      return "metadata_unavailable";
    default:
      if (context === "dispatch") return "dispatch_failed";
      if (context === "create") return "create_failed";
      if (context === "verify") return "verify_failed";
      if (context === "inbox") return "inbox_failed";
      if (context === "sweep") return "sweep_failed";
      if (context === "notifications") return "notification_failed";
      return "metadata_failed";
  }
}

function toWorkbenchError(error: unknown, context: ErrorContext = "dispatch"): WorkbenchError {
  if (error instanceof WorkbenchError) return error;
  const err = (error instanceof Error ? error : new Error(String(error))) as ApiErrorLike;
  const status = typeof err.status === "number" ? err.status : undefined;
  if (isWorkbenchCode(err.code)) {
    return new WorkbenchError(err.code, err.message, status);
  }
  const code = ERROR_CODES.find((candidate) => err.message.includes(candidate));
  return new WorkbenchError(code ?? fallbackCode(status, context), err.message, status);
}

/** 派发请求的最长等待时间；超时后放弃并让调用方清理 pending 状态。 */
const DISPATCH_TIMEOUT_MS = 5000;

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

/**
 * 派发一张卡。前端不做预算计算；只把卡 id 交给后端闸门。
 * 请求最多等 5s：后端预算 <3s，超出即视为挂起并 abort，避免待办页
 * 永久卡在「正在派发」与 in-flight 串行锁里。
 */
export async function dispatchTask(taskId: string): Promise<DispatchResult> {
  if (!taskId || !taskId.trim()) {
    throw new WorkbenchError("dispatch_failed", "缺少任务 id");
  }
  const controller = new AbortController();
  let timer: number | undefined;
  // Race explicitly: an SDK fetchJSON may ignore AbortSignal, so the timer
  // itself must reject to keep the wait bounded.
  const timeout = new Promise<never>((_, reject) => {
    timer = window.setTimeout(() => {
      controller.abort();
      reject(new WorkbenchError("dispatch_timeout", "派发超时，请刷新后重试"));
    }, DISPATCH_TIMEOUT_MS);
  });
  try {
    const data = (await Promise.race([
      api(WORKBENCH + "/dispatch/" + encodeURIComponent(taskId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        signal: controller.signal,
      }),
      timeout,
    ])) as Partial<DispatchResult> | null;
    if (!data || data.ok !== true || typeof data.position !== "number") {
      throw new WorkbenchError("dispatch_failed", "派发响应格式不正确", 200);
    }
    return {
      ok: true,
      position: data.position,
      eta: typeof data.eta === "string" ? data.eta : null,
      status: typeof data.status === "string" ? data.status : "ready",
    };
  } catch (error) {
    if (error instanceof WorkbenchError) throw error;
    if (isAbortError(error)) {
      throw new WorkbenchError("dispatch_timeout", "派发超时，请刷新后重试");
    }
    throw toWorkbenchError(error, "dispatch");
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

/**
 * 建一张待办卡：后端先走上游 `POST /tasks`（triage=true，1.2 闸门放行），
 * 再写 workbench metadata（bucket + 空 contract）。不自动派发。
 * `idempotencyKey` 由 UI 每次草稿尝试生成并在失败重试时复用；上游按 key
 * 去重，metadata 失败后的重试不会建出第二张卡。
 */
export async function createTask(
  title: string,
  bucket: Bucket = "today",
  idempotencyKey?: string,
): Promise<CreateTaskResult> {
  const trimmed = title.trim();
  if (!trimmed) {
    throw new WorkbenchError("invalid_title", "先写一句要做什么");
  }
  const payload: Record<string, unknown> = { title: trimmed, bucket };
  if (idempotencyKey) payload.idempotency_key = idempotencyKey;
  try {
    const data = (await api(WORKBENCH + "/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })) as Partial<CreateTaskResult> | null;
    if (!data || data.ok !== true || !data.task || typeof data.task.id !== "string") {
      throw new WorkbenchError("create_failed", "建卡响应格式不正确", 200);
    }
    const metadata =
      data.metadata && typeof data.metadata === "object" ? data.metadata : { bucket };
    return { ok: true, task: data.task, metadata };
  } catch (error) {
    throw toWorkbenchError(error, "create");
  }
}

/**
 * 就地写 workbench metadata（目前是 bucket / contract）。后端按 key 合并，
 * 不会覆盖派发瞬间的 contract_defaults。
 */
export async function updateTaskMetadata(
  taskId: string,
  patch: TaskMetadataPatch,
): Promise<TaskMetadata> {
  if (!taskId || !taskId.trim()) {
    throw new WorkbenchError("metadata_failed", "缺少任务 id");
  }
  try {
    const data = (await api(WORKBENCH + "/metadata/" + encodeURIComponent(taskId), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })) as { ok?: unknown; metadata?: unknown } | null;
    if (!data || data.ok !== true || !data.metadata || typeof data.metadata !== "object") {
      throw new WorkbenchError("metadata_failed", "metadata 响应格式不正确", 200);
    }
    return data.metadata as TaskMetadata;
  } catch (error) {
    throw toWorkbenchError(error, "metadata");
  }
}

/**
 * 验收闸门的前端预检。`metadata` 必传：契约存在 workbench.db，上游
 * /board 卡片不携带 metadata，不能从 task 上兜底。
 * 后端仍是最终裁决者，这里只用于“不发起网络请求”的即时拒绝。
 */
export function hasAcceptance(metadata: TaskMetadata | null | undefined): boolean {
  if (!metadata || typeof metadata !== "object") return false;
  const contract = (metadata as { contract?: unknown }).contract;
  if (!contract || typeof contract !== "object") return false;
  const accept = (contract as { accept?: unknown }).accept;
  return typeof accept === "string" && accept.trim().length > 0;
}

/**
 * 批量读取 workbench metadata；无记录的 id 不会出现在返回 map 里。
 * 按 100 个 id/请求分块，服务端单次上限 200，因此长列表不会丢记录。
 */
export async function getTaskMetadata(ids: string[]): Promise<Record<string, TaskMetadata>> {
  const unique = Array.from(new Set(ids.map((id) => id.trim()).filter(Boolean)));
  if (unique.length === 0) return {};
  const chunks: string[][] = [];
  for (let index = 0; index < unique.length; index += 100) {
    chunks.push(unique.slice(index, index + 100));
  }
  const merged: Record<string, TaskMetadata> = {};
  try {
    for (const chunk of chunks) {
      const data = await api(
        WORKBENCH + "/metadata?ids=" + encodeURIComponent(chunk.join(",")),
      );
      if (data && typeof data === "object") {
        Object.assign(merged, data as Record<string, TaskMetadata>);
      }
    }
    return merged;
  } catch (error) {
    throw toWorkbenchError(error, "metadata");
  }
}

/**
 * 把 metadata map 合并回卡列表；没有记录（或取 metadata 失败时传空 map）
 * 的卡原样保留。返回新数组，不修改入参。
 */
export function mergeTaskMetadata<T extends { id: string }>(
  tasks: T[],
  metadataById: Record<string, TaskMetadata>,
): Array<T & { metadata?: TaskMetadata }> {
  return tasks.map((task) => {
    const patch = metadataById[task.id];
    if (!patch) return task;
    const existing = (task as { metadata?: unknown }).metadata;
    const merged =
      existing && typeof existing === "object"
        ? ({ ...(existing as Record<string, unknown>), ...patch } as TaskMetadata)
        : patch;
    return { ...task, metadata: merged };
  });
}

export interface AutoCollectError {
  message: string;
  attempts?: number;
  retry_in_seconds?: number;
}

/** 验收队列的一张 review 卡；`review_age_seconds` 缺失 = 无 review_requested 事件。 */
export interface InboxTask {
  id: string;
  title: string;
  status: string;
  assignee?: string | null;
  priority?: number | null;
  created_at?: number | null;
  completed_at?: number | null;
  result?: string | null;
  latest_summary?: string | null;
  metadata: TaskMetadata;
  review_requested_at?: number | null;
  review_age_seconds?: number | null;
  /** 已到自动收时限；sweep 端点是唯一的写入口。 */
  auto_collect_due?: boolean;
  /** PATCH 失败后的退避信息；窗口内 sweeper 不再重试。 */
  auto_collect_error?: AutoCollectError | null;
}

export interface InboxResult {
  ok: true;
  tasks: InboxTask[];
  count: number;
  /** 自动收窗口（秒），后端权威值。 */
  review_ttl_seconds: number;
  /** 详情读取失败被跳过的卡；队列本身仍返回 200。 */
  failed_ids: string[];
}

export interface InboxCountResult {
  ok: true;
  count: number;
  review_ttl_seconds: number;
}

export interface SweepResult {
  ok: true;
  collected: number;
  failed: number;
  skipped: number;
  collected_ids: string[];
  failed_ids: string[];
}

export type VerifyAction = "accept" | "reject" | "skip";

export interface VerifyResult {
  ok: true;
  action: VerifyAction;
  status: string;
  comment_written: boolean;
}

/** 与服务端 MAX_REJECT_REASON_CHARS 保持一致。 */
export const MAX_REJECT_REASON_CHARS = 2000;

/**
 * 验收队列：pure read。48h 自动收由 POST /reviews/sweep 显式触发，
 * 不在轮询里写卡。
 */
export async function getInbox(): Promise<InboxResult> {
  try {
    const data = (await api(WORKBENCH + "/inbox")) as Partial<InboxResult> | null;
    if (!data || data.ok !== true || !Array.isArray(data.tasks)) {
      throw new WorkbenchError("inbox_failed", "验收队列响应格式不正确", 200);
    }
    return {
      ok: true,
      tasks: data.tasks as InboxTask[],
      count: typeof data.count === "number" ? data.count : data.tasks.length,
      review_ttl_seconds:
        typeof data.review_ttl_seconds === "number" ? data.review_ttl_seconds : 0,
      failed_ids: Array.isArray(data.failed_ids) ? data.failed_ids : [],
    };
  } catch (error) {
    throw toWorkbenchError(error, "inbox");
  }
}

/** 右上角计数专用：只读 board，不拉每张卡的详情。 */
export async function getInboxCount(): Promise<InboxCountResult> {
  try {
    const data = (await api(WORKBENCH + "/inbox/count")) as Partial<InboxCountResult> | null;
    if (!data || data.ok !== true || typeof data.count !== "number") {
      throw new WorkbenchError("inbox_failed", "验收计数响应格式不正确", 200);
    }
    return {
      ok: true,
      count: data.count,
      review_ttl_seconds:
        typeof data.review_ttl_seconds === "number" ? data.review_ttl_seconds : 0,
    };
  } catch (error) {
    throw toWorkbenchError(error, "inbox");
  }
}

/** 显式触发 48h 自动收（POST 带同源/JSON 守卫）；失败不阻塞队列读取。 */
export async function sweepReviews(): Promise<SweepResult> {
  try {
    const data = (await api(WORKBENCH + "/reviews/sweep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })) as Partial<SweepResult> | null;
    if (!data || data.ok !== true) {
      throw new WorkbenchError("sweep_failed", "自动收响应格式不正确", 200);
    }
    return {
      ok: true,
      collected: typeof data.collected === "number" ? data.collected : 0,
      failed: typeof data.failed === "number" ? data.failed : 0,
      skipped: typeof data.skipped === "number" ? data.skipped : 0,
      collected_ids: Array.isArray(data.collected_ids) ? data.collected_ids : [],
      failed_ids: Array.isArray(data.failed_ids) ? data.failed_ids : [],
    };
  } catch (error) {
    throw toWorkbenchError(error, "sweep");
  }
}

/**
 * 验收动作统一入口：accept → done；reject → comment + triage；skip 不改状态。
 * reject 的前端预检在发请求前拦下空理由；后端仍会再校验一次。
 */
export async function verifyTask(
  taskId: string,
  action: VerifyAction,
  reason?: string,
): Promise<VerifyResult> {
  if (!taskId || !taskId.trim()) {
    throw new WorkbenchError("verify_failed", "缺少任务 id");
  }
  const trimmedReason = typeof reason === "string" ? reason.trim() : "";
  if (action === "reject" && !trimmedReason) {
    throw new WorkbenchError("reason_required", "不收必须写理由");
  }
  if (action === "reject" && trimmedReason.length > MAX_REJECT_REASON_CHARS) {
    throw new WorkbenchError(
      "reason_too_long",
      `理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`,
    );
  }
  const payload: Record<string, unknown> = { action };
  if (action === "reject") payload.reason = trimmedReason;
  try {
    const data = (await api(WORKBENCH + "/verify/" + encodeURIComponent(taskId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })) as {
      ok?: unknown;
      action?: unknown;
      status?: unknown;
      comment_written?: unknown;
    } | null;
    if (!data || data.ok !== true) {
      throw new WorkbenchError("verify_failed", "验收响应格式不正确", 200);
    }
    return {
      ok: true,
      action: (data.action as VerifyAction) || action,
      status: typeof data.status === "string" ? data.status : "",
      comment_written: data.comment_written === true,
    };
  } catch (error) {
    throw toWorkbenchError(error, "verify");
  }
}

/* -------------------------------------------------------------------------
 * 通知中心（2.2）：P0/P1/P2 快照 + 动作。
 * 后端只在独立版 8787 提供；事件 id 含冒号，路径参数必须 encodeURIComponent。
 * ---------------------------------------------------------------------- */

export type NotificationLevel = "p0" | "p1" | "p2";
export type NotificationActionName = "accept" | "reject" | "remind";

export interface NotificationItem {
  id: string;
  task_id: string;
  kind: "blocked" | "review";
  level: NotificationLevel;
  title: string;
  status: string;
  /** 中文原因，UI 直接展示。 */
  reason: string;
  block_kind: string | null;
  block_recurrences: number;
  review_requested_at: number | null;
  /** 距 48h 自动收的剩余秒数；null = 无 review 记录。 */
  auto_collect_in_seconds: number | null;
  auto_collect_due: boolean;
  parent_ids: string[];
  first_seen_at: number;
  age_seconds: number;
  notified_at: number | null;
  escalated: boolean;
  snooze_until: number | null;
  actions: NotificationActionName[];
  slot: "active" | "queued" | null;
  bundle_key: string | null;
}

export interface NotificationBundle {
  key: string;
  parent_ids: string[];
  count: number;
  task_ids: string[];
  cards: NotificationItem[];
  oldest_review_at: number | null;
}

export interface NotificationSlots {
  capacity: number;
  active: NotificationBundle[];
  queued: NotificationBundle[];
  queued_count: number;
  queued_cards: number;
}

export interface NotificationDaily {
  date: string;
  sent: boolean;
  count: number;
  line: string;
  hour: number;
}

export interface NotificationDesktopState {
  available: boolean;
  command: string;
  last_error: string | null;
  last_sent_at: number | null;
  last_kind: string | null;
  last_urgency: string | null;
}

export interface NotificationSnapshot {
  ok: true;
  generated_at: number;
  now: number;
  counts: { p0: number; p1: number; p2: number; total: number };
  p0: NotificationItem[];
  p1: NotificationItem[];
  p2: NotificationItem[];
  review_ttl_seconds: number;
  slots: NotificationSlots;
  daily: NotificationDaily;
  desktop: NotificationDesktopState;
  sweep: { last_at: number | null; last_error: string | null };
  channels: { desktop: string; mobile: string };
  thresholds?: {
    batch_size?: number;
    batch_seconds?: number;
    escalate_seconds?: number;
    snooze_seconds?: number;
    daily_hour?: number;
  };
}

export interface NotificationActionResult {
  ok: true;
  id?: string;
  action: string;
  status?: string;
  comment_written?: boolean;
  snooze_until?: number;
}

const NOTIFICATION_ACTIONS: readonly NotificationActionName[] = [
  "accept",
  "reject",
  "remind",
];

function numberOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === "string" && entry.length > 0)
    : [];
}

function normalizeNotificationItem(raw: unknown): NotificationItem | null {
  if (!raw || typeof raw !== "object") return null;
  const source = raw as Record<string, unknown>;
  if (typeof source.id !== "string" || !source.id) return null;
  const kind = source.kind === "blocked" || source.kind === "review" ? source.kind : null;
  const level =
    source.level === "p0" || source.level === "p1" || source.level === "p2"
      ? source.level
      : null;
  if (!kind || !level) return null;
  const actions = Array.isArray(source.actions)
    ? source.actions.filter(
        (entry): entry is NotificationActionName =>
          typeof entry === "string" &&
          (NOTIFICATION_ACTIONS as readonly string[]).includes(entry),
      )
    : [];
  return {
    id: source.id,
    task_id: typeof source.task_id === "string" ? source.task_id : "",
    kind,
    level,
    title: typeof source.title === "string" ? source.title : source.id,
    status: typeof source.status === "string" ? source.status : kind,
    reason: typeof source.reason === "string" ? source.reason : "",
    block_kind: typeof source.block_kind === "string" ? source.block_kind : null,
    block_recurrences: numberOr(source.block_recurrences, 0),
    review_requested_at: nullableNumber(source.review_requested_at),
    auto_collect_in_seconds: nullableNumber(source.auto_collect_in_seconds),
    auto_collect_due: source.auto_collect_due === true,
    parent_ids: stringList(source.parent_ids),
    first_seen_at: numberOr(source.first_seen_at, 0),
    age_seconds: numberOr(source.age_seconds, 0),
    notified_at: nullableNumber(source.notified_at),
    escalated: source.escalated === true,
    snooze_until: nullableNumber(source.snooze_until),
    actions,
    slot:
      source.slot === "active" || source.slot === "queued" ? source.slot : null,
    bundle_key: typeof source.bundle_key === "string" ? source.bundle_key : null,
  };
}

function normalizeItems(value: unknown): NotificationItem[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(normalizeNotificationItem)
    .filter((item): item is NotificationItem => item !== null);
}

function normalizeBundles(value: unknown): NotificationBundle[] {
  if (!Array.isArray(value)) return [];
  const bundles: NotificationBundle[] = [];
  for (const raw of value) {
    if (!raw || typeof raw !== "object") continue;
    const source = raw as Record<string, unknown>;
    if (typeof source.key !== "string") continue;
    const cards = normalizeItems(source.cards);
    bundles.push({
      key: source.key,
      parent_ids: stringList(source.parent_ids),
      count: numberOr(source.count, cards.length),
      task_ids: stringList(source.task_ids),
      cards,
      oldest_review_at: nullableNumber(source.oldest_review_at),
    });
  }
  return bundles;
}

function normalizeNotificationSnapshot(data: unknown): NotificationSnapshot {
  if (!data || typeof data !== "object") {
    throw new WorkbenchError("notification_failed", "通知快照响应格式不正确", 200);
  }
  const source = data as Record<string, unknown>;
  if (source.ok !== true) {
    throw new WorkbenchError("notification_failed", "通知快照响应格式不正确", 200);
  }
  const p0 = normalizeItems(source.p0);
  const p1 = normalizeItems(source.p1);
  const p2 = normalizeItems(source.p2);
  const slotsRaw =
    source.slots && typeof source.slots === "object"
      ? (source.slots as Record<string, unknown>)
      : {};
  const dailyRaw =
    source.daily && typeof source.daily === "object"
      ? (source.daily as Record<string, unknown>)
      : {};
  const desktopRaw =
    source.desktop && typeof source.desktop === "object"
      ? (source.desktop as Record<string, unknown>)
      : {};
  const sweepRaw =
    source.sweep && typeof source.sweep === "object"
      ? (source.sweep as Record<string, unknown>)
      : {};
  const channelsRaw =
    source.channels && typeof source.channels === "object"
      ? (source.channels as Record<string, unknown>)
      : {};
  const generatedAt = numberOr(source.generated_at, Math.floor(Date.now() / 1000));
  return {
    ok: true,
    generated_at: generatedAt,
    now: numberOr(source.now, generatedAt),
    counts: {
      p0: p0.length,
      p1: p1.length,
      p2: p2.length,
      total: p0.length + p1.length + p2.length,
    },
    p0,
    p1,
    p2,
    review_ttl_seconds: numberOr(source.review_ttl_seconds, 0),
    slots: {
      capacity: numberOr(slotsRaw.capacity, 3),
      active: normalizeBundles(slotsRaw.active),
      queued: normalizeBundles(slotsRaw.queued),
      queued_count: numberOr(slotsRaw.queued_count, 0),
      queued_cards: numberOr(slotsRaw.queued_cards, 0),
    },
    daily: {
      date: typeof dailyRaw.date === "string" ? dailyRaw.date : "",
      sent: dailyRaw.sent === true,
      count: numberOr(dailyRaw.count, p2.length),
      line: typeof dailyRaw.line === "string" ? dailyRaw.line : "",
      hour: numberOr(dailyRaw.hour, 8),
    },
    desktop: {
      available: desktopRaw.available === true,
      command: typeof desktopRaw.command === "string" ? desktopRaw.command : "",
      last_error:
        typeof desktopRaw.last_error === "string" ? desktopRaw.last_error : null,
      last_sent_at: nullableNumber(desktopRaw.last_sent_at),
      last_kind: typeof desktopRaw.last_kind === "string" ? desktopRaw.last_kind : null,
      last_urgency:
        typeof desktopRaw.last_urgency === "string" ? desktopRaw.last_urgency : null,
    },
    sweep: {
      last_at: nullableNumber(sweepRaw.last_at),
      last_error:
        typeof sweepRaw.last_error === "string" ? sweepRaw.last_error : null,
    },
    channels: {
      desktop: typeof channelsRaw.desktop === "string" ? channelsRaw.desktop : "",
      mobile:
        typeof channelsRaw.mobile === "string" ? channelsRaw.mobile : "reserved",
    },
    thresholds:
      source.thresholds && typeof source.thresholds === "object"
        ? (source.thresholds as NotificationSnapshot["thresholds"])
        : undefined,
  };
}

/** 通知快照：pure read，后端内存态；采集由 sweep 或后台轮询完成。 */
export async function getNotifications(): Promise<NotificationSnapshot> {
  try {
    const data = await api(WORKBENCH + "/notifications");
    return normalizeNotificationSnapshot(data);
  } catch (error) {
    throw toWorkbenchError(error, "notifications");
  }
}

/** 显式采集一轮（guarded POST）；后台 5s 轮询在服务端也做同样的事。 */
export async function sweepNotifications(): Promise<NotificationSnapshot> {
  try {
    const data = await api(WORKBENCH + "/notifications/sweep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    return normalizeNotificationSnapshot(data);
  } catch (error) {
    throw toWorkbenchError(error, "notifications");
  }
}

/**
 * 通知动作：accept / reject 直接复用 2.1 verify；remind 由后端 snooze 10 分钟。
 * reject 的空理由在前端先拦一次，后端仍会校验。
 */
export async function notificationAction(
  eventId: string,
  action: NotificationActionName,
  reason?: string,
  expectedReviewAt?: number | null,
): Promise<NotificationActionResult> {
  if (!eventId || !eventId.trim()) {
    throw new WorkbenchError("notification_failed", "缺少通知 id");
  }
  const trimmedReason = typeof reason === "string" ? reason.trim() : "";
  if (action === "reject" && !trimmedReason) {
    throw new WorkbenchError("reason_required", "不收必须写理由");
  }
  if (action === "reject" && trimmedReason.length > MAX_REJECT_REASON_CHARS) {
    throw new WorkbenchError(
      "reason_too_long",
      `理由最多 ${MAX_REJECT_REASON_CHARS} 字，请精简后再提交`,
    );
  }
  const payload: Record<string, unknown> = { action };
  if (action === "reject") payload.reason = trimmedReason;
  if (typeof expectedReviewAt === "number" && Number.isFinite(expectedReviewAt)) {
    payload.expected_review_at = expectedReviewAt;
  }
  try {
    const data = (await api(
      WORKBENCH + "/notifications/" + encodeURIComponent(eventId) + "/action",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    )) as Partial<NotificationActionResult> | null;
    if (!data || data.ok !== true) {
      throw new WorkbenchError("notification_failed", "通知动作响应格式不正确", 200);
    }
    return {
      ok: true,
      id: typeof data.id === "string" ? data.id : eventId,
      action: typeof data.action === "string" ? data.action : action,
      status: typeof data.status === "string" ? data.status : undefined,
      comment_written: data.comment_written === true,
      snooze_until: nullableNumber(data.snooze_until) ?? undefined,
    };
  } catch (error) {
    throw toWorkbenchError(error, "notifications");
  }
}
