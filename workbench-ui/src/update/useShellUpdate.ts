import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 独立版外壳更新探测：/api/workbench/version 是 index.js + style.css + sw.js
 * 的指纹。首屏拿到基线后，每 60s 和窗口重新获得焦点时比对；只有服务端指纹
 * 变了才提示，避免普通轮询把 UI 打扰成噪音。
 */

const VERSION_URL = "/api/workbench/version";
const POLL_MS = 60_000;

interface VersionPayload {
  ok?: unknown;
  version?: unknown;
}

export interface ShellUpdate {
  available: boolean;
  reload: () => void;
  dismiss: () => void;
}

export function useShellUpdate(enabled: boolean): ShellUpdate {
  const [available, setAvailable] = useState(false);
  const baseline = useRef<string | null>(null);
  const latest = useRef<string>("");
  const dismissed = useRef<string>("");
  const checking = useRef(false);

  const check = useCallback(async () => {
    if (!enabled || checking.current) return;
    checking.current = true;
    try {
      const response = await fetch(VERSION_URL, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      const data = (await response.json()) as VersionPayload;
      const next = typeof data.version === "string" ? data.version : "";
      if (!next) return;
      if (baseline.current === null) {
        baseline.current = next;
        // Exposed for diagnostics and the headless probe; this is the version
        // the currently loaded bundle belongs to.
        window.__WB_SHELL_BASELINE__ = next;
        return;
      }
      if (next === baseline.current) return;
      latest.current = next;
      if (next !== dismissed.current) setAvailable(true);
    } catch {
      // 服务重启或网络抖动：下一轮/下次聚焦再试，不弹错误。
    } finally {
      checking.current = false;
    }
  }, [enabled]);

  useEffect(() => {
    if (!enabled) return;
    void check();
    const timer = window.setInterval(() => void check(), POLL_MS);
    const onFocus = () => void check();
    const onVisibility = () => {
      if (document.visibilityState === "visible") void check();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [enabled, check]);

  const dismiss = useCallback(() => {
    dismissed.current = latest.current;
    setAvailable(false);
  }, []);

  const reload = useCallback(() => {
    void (async () => {
      try {
        const registration = await navigator.serviceWorker?.getRegistration();
        await registration?.update();
      } catch {
        // 没有 SW 或被浏览器拒绝时，直接 reload 也会重新请求外壳。
      }
      window.location.reload();
    })();
  }, []);

  return { available, reload, dismiss };
}

declare global {
  interface Window {
    __WB_SHELL_BASELINE__?: string;
  }
}