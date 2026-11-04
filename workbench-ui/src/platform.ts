/**
 * 数据层：宿主有 dashboard SDK 就用它的 fetchJSON（自带 loopback token），
 * 独立版退回原生 fetch。两边的错误形状统一成 Error(message)；独立版额外
 * 保留 HTTP status 与后端 error 码（见 ApiError）。
 */
export type FetchJSON = (url: string, init?: RequestInit) => Promise<any>;

interface HermesPluginSDK {
  React?: unknown;
  ReactDOM?: unknown;
  hooks?: Record<string, unknown>;
  fetchJSON?: FetchJSON;
}

declare global {
  interface Window {
    __HERMES_PLUGIN_SDK__?: HermesPluginSDK;
    __HERMES_PLUGINS__?: { register: (name: string, component: unknown) => void };
  }
}

const sdk = typeof window === "undefined" ? undefined : window.__HERMES_PLUGIN_SDK__;

/** 是否跑在 Hermes dashboard 的插件宿主里 */
export const isPluginHost = Boolean(sdk && sdk.fetchJSON);

/**
 * HTTP 错误：保留状态码与后端的 `error` 码（workbench 端点是
 * `{ok:false,error:"missing_acceptance"}`），message 仍带后端原文，
 * 这样插件宿主的 SDK 错误与独立版行为一致。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function plainFetch(url: string, init?: RequestInit): Promise<any> {
  const opts: RequestInit = { ...(init || {}) };
  opts.headers = { Accept: "application/json", ...((init && init.headers) || {}) };
  const res = await fetch(url, opts);
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) {
    const detail = data && (data.detail || data.error);
    const msg = Array.isArray(detail)
      ? detail.map((d: any) => d.msg || JSON.stringify(d)).join("; ")
      : detail;
    const explicitCode =
      data && typeof data === "object" && typeof data.error === "string" && /^[a-z][a-z0-9_]*$/.test(data.error)
        ? data.error
        : undefined;
    throw new ApiError(msg || `HTTP ${res.status}`, res.status, explicitCode);
  }
  return data;
}

export const api: FetchJSON = (url, init) =>
  isPluginHost && sdk && sdk.fetchJSON ? sdk.fetchJSON(url, init) : plainFetch(url, init);

/** kanban 插件的 REST 基址（与 CLI / gateway 同一份 kanban_db，只走 API） */
export const KANBAN = "/api/plugins/kanban";