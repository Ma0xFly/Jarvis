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
  | "metadata_failed";

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

type ErrorContext = "dispatch" | "create" | "metadata";

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
    case 500:
      return context === "dispatch"
        ? "invalid_defaults"
        : context === "create"
          ? "create_failed"
          : "metadata_failed";
    case 502:
      return "upstream_unavailable";
    case 503:
      return "metadata_unavailable";
    default:
      return context === "dispatch"
        ? "dispatch_failed"
        : context === "create"
          ? "create_failed"
          : "metadata_failed";
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
