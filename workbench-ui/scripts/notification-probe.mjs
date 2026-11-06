#!/usr/bin/env node
/**
 * Headless probe for Story 2.2 (P0 notifications + notification actions).
 *
 * Boots an isolated workbench server (throwaway kanban / workbench DBs, a
 * logger script instead of notify-send), seeds blocked + review cards through
 * the public REST surfaces and direct kanban status writes, then drives the
 * standalone UI in headless Chrome over CDP.
 *
 * Covered:
 *   - P0 pinned (blocked needs_input + review with unfinished child)
 *   - P1 panel, non-review actions = remind + open board only
 *   - review countdown, acceptance slots 3 bundles (siblings bundled) + queue
 *   - P2 daily line
 *   - desktop notification (logged critical urgency)
 *   - remind snoozes and is visible; reject reason rules; accept → done
 *   - fixed top-right layer + light/dark tokens
 *   - plugin host has no notification center and never fetches notifications
 *
 * Usage: node scripts/notification-probe.mjs
 *   CHROME_BIN, WORKBENCH_PYTHON override auto-discovery.
 */
import { execFileSync, spawn } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const uiRoot = resolve(here, "..");
const repoRoot = resolve(uiRoot, "..");
const appDir = join(repoRoot, "workbench-app");
const serverPath = join(appDir, "server.py");
const defaultsPath = join(appDir, "defaults.json");

function findPython() {
  const override = process.env.WORKBENCH_PYTHON;
  if (override && existsSync(override)) return override;
  const tools = join(homedir(), ".hermes", "tools");
  if (existsSync(tools)) {
    const candidates = readdirSync(tools)
      .filter((name) => name.startsWith("python-3.14"))
      .sort()
      .reverse();
    for (const name of candidates) {
      const candidate = join(tools, name, "bin", "python3");
      if (existsSync(candidate)) return candidate;
    }
  }
  const venv = join(homedir(), ".hermes", "hermes-agent", "venv", "bin", "python");
  if (existsSync(venv)) return venv;
  return "python3";
}

function findChrome() {
  const explicit = process.env.CHROME_BIN;
  if (explicit && existsSync(explicit)) return explicit;
  const direct = [
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ];
  for (const candidate of direct) {
    if (existsSync(candidate)) return candidate;
  }
  const cache = join(homedir(), ".cache", "ms-playwright");
  if (existsSync(cache)) {
    const dirs = readdirSync(cache)
      .filter((name) => name.startsWith("chromium-") && !name.includes("headless"))
      .sort()
      .reverse();
    for (const name of dirs) {
      const candidate = join(cache, name, "chrome-linux", "chrome");
      if (existsSync(candidate)) return candidate;
    }
  }
  throw new Error("找不到 Chrome；用 CHROME_BIN 指定");
}

function freePort() {
  return new Promise((resolvePort, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolvePort(port));
    });
  });
}

async function httpJson(method, url, payload) {
  const response = await fetch(url, {
    method,
    headers:
      payload === undefined
        ? { Accept: "application/json" }
        : { Accept: "application/json", "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  return { status: response.status, data };
}

async function waitFor(predicate, { timeout = 10000, interval = 100, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await predicate()) return;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, interval));
  }
  throw new Error(`等待超时：${label}${lastError ? `（${lastError.message}）` : ""}`);
}

function cdpConnect(url) {
  return new Promise((resolveConnection, reject) => {
    const socket = new WebSocket(url);
    let nextId = 0;
    let closed = false;
    const pending = new Map();

    const rejectAll = (error) => {
      for (const entry of pending.values()) {
        clearTimeout(entry.timer);
        entry.reject(error);
      }
      pending.clear();
    };

    socket.addEventListener("open", () => {
      resolveConnection({
        send(method, params = {}) {
          if (closed) return Promise.reject(new Error("CDP socket closed"));
          return new Promise((resolveSend, rejectSend) => {
            const id = ++nextId;
            const timer = setTimeout(() => {
              pending.delete(id);
              rejectSend(new Error(`CDP timeout: ${method}`));
            }, 15000);
            pending.set(id, { resolve: resolveSend, reject: rejectSend, timer });
            try {
              socket.send(JSON.stringify({ id, method, params }));
            } catch (error) {
              clearTimeout(timer);
              pending.delete(id);
              rejectSend(error);
            }
          });
        },
        close() {
          socket.close();
        },
      });
    });
    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message.id) return;
      const entry = pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id);
      clearTimeout(entry.timer);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
      else entry.resolve(message.result);
    });
    socket.addEventListener("close", () => {
      closed = true;
      rejectAll(new Error("CDP socket closed"));
    });
    socket.addEventListener("error", (event) => {
      closed = true;
      const error = event.error || new Error("CDP 连接失败");
      rejectAll(error);
      reject(error);
    });
  });
}

async function main() {
  const python = findPython();
  const chromeBin = findChrome();
  const tmp = mkdtempSync(join(tmpdir(), "wb-notify-probe-"));
  const defaults = join(tmp, "defaults.json");
  writeFileSync(defaults, readFileSync(defaultsPath));
  const notifyLog = join(tmp, "notify.log");
  const notifyFailFlag = join(tmp, "notify-fail.flag");
  const notifyScript = join(tmp, "notify-logger.sh");
  writeFileSync(
    notifyScript,
    "#!/bin/sh\n{\n  printf '%s\\n' \"$@\"\n  printf -- '---\\n'\n} >> " +
      JSON.stringify(notifyLog) +
      "\nif [ -f " +
      JSON.stringify(notifyFailFlag) +
      " ]; then\n  echo 'forced notify failure' >&2\n  exit 1\nfi\nexit 0\n",
  );
  chmodSync(notifyScript, 0o755);

  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  const kanbanDb = join(tmp, "kanban-home", "kanban.db");
  let serverLog = "";
  let server;
  let chrome;
  let cdp;

  const checks = [];
  function check(name, condition, detail = "") {
    if (!condition) throw new Error(`✗ ${name}${detail ? `：${detail}` : ""}`);
    checks.push(name);
    console.log(`✓ ${name}`);
  }

  const sqlRun = (sql, ...values) => {
    const output = execFileSync(
      python,
      [
        "-I",
        "-c",
        "import sqlite3,sys,json; conn=sqlite3.connect(sys.argv[1], timeout=5); "
          + "cur=conn.execute(sys.argv[2], json.loads(sys.argv[3])); conn.commit(); "
          + "print(cur.rowcount); conn.close()",
        kanbanDb,
        sql,
        JSON.stringify(values),
      ],
      { encoding: "utf8" },
    );
    return Number(String(output).trim());
  };
  const setReviewEventAge = (taskId, ageSeconds) => {
    const rows = sqlRun(
      "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='review_requested'",
      Math.floor(Date.now() / 1000) - ageSeconds,
      taskId,
    );
    if (rows !== 1) {
      throw new Error(`review 事件应命中 1 行，实际 ${rows} 行：${taskId}`);
    }
  };

  try {
    server = spawn(python, ["-I", serverPath, "--host", "127.0.0.1", "--port", String(port)], {
      env: {
        ...process.env,
        HERMES_KANBAN_HOME: join(tmp, "kanban-home"),
        HERMES_KANBAN_DB: join(tmp, "kanban-home", "kanban.db"),
        WORKBENCH_DB: join(tmp, "workbench.db"),
        WORKBENCH_DEFAULTS: defaults,
        WORKBENCH_REVIEW_SWEEP_SECONDS: "0",
        WORKBENCH_INBOX_CACHE_SECONDS: "0",
        // The probe drives sweeps explicitly; no background notification race.
        WORKBENCH_NOTIFY_SWEEP_SECONDS: "0",
        WORKBENCH_NOTIFY_COMMAND: notifyScript,
        // Make the daily digest eligible at any hour of the probe run.
        WORKBENCH_NOTIFY_DAILY_HOUR: "0",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    server.stdout.on("data", (chunk) => (serverLog += chunk));
    server.stderr.on("data", (chunk) => (serverLog += chunk));
    server.once("exit", (code) => {
      if (code !== null && code !== 0) serverLog += `\n[server exited ${code}]`;
    });

    await waitFor(
      async () => {
        try {
          return (await httpJson("GET", `${base}/healthz`)).status === 200;
        } catch {
          return false;
        }
      },
      { timeout: 30000, label: "workbench /healthz" },
    );

    const bundle = await fetch(`${base}/static/index.js`).then((response) => response.text());
    check(
      "独立版产物包含通知中心",
      bundle.includes("wb-notify-center") && bundle.includes("wb-notify-pinned"),
      "先运行 npm run build",
    );

    // ---- seed cards through public REST --------------------------------
    const taskUrl = (taskId) =>
      `${base}/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`;
    const createTask = async (title, parents) => {
      const result = await httpJson("POST", `${base}/api/plugins/kanban/tasks`, {
        title,
        body: "probe",
        triage: true,
        ...(parents ? { parents } : {}),
      });
      if (result.status !== 200 || !result.data?.task?.id) {
        throw new Error(`建卡失败 ${title}: ${result.status} ${JSON.stringify(result.data)}`);
      }
      return result.data.task.id;
    };
    const setMetadata = async (taskId, patch) => {
      const result = await httpJson(
        "PATCH",
        `${base}/api/workbench/metadata/${encodeURIComponent(taskId)}`,
        patch,
      );
      if (result.status !== 200) {
        throw new Error(`写 metadata 失败 ${taskId}: ${result.status} ${JSON.stringify(result.data)}`);
      }
    };
    const dispatch = async (taskId) => {
      const result = await httpJson(
        "POST",
        `${base}/api/workbench/dispatch/${encodeURIComponent(taskId)}`,
        {},
      );
      if (result.status !== 200) {
        throw new Error(`派发失败 ${taskId}: ${result.status} ${JSON.stringify(result.data)}`);
      }
    };
    const patchStatus = async (taskId, status, extra = {}) => {
      const result = await httpJson("PATCH", taskUrl(taskId), { status, ...extra });
      if (result.status !== 200) {
        throw new Error(`改状态失败 ${taskId} → ${status}: ${result.status} ${JSON.stringify(result.data)}`);
      }
    };
    const seedReady = async (title, parents) => {
      const taskId = await createTask(title, parents);
      await setMetadata(taskId, {
        bucket: "today",
        contract: { accept: "probe", input: "probe", ready: false },
      });
      await dispatch(taskId);
      return taskId;
    };
    const seedReview = async (title, { parents, age, solidified = true }) => {
      const taskId = await seedReady(title, parents);
      await patchStatus(taskId, "review", {
        summary: `完成：${title}`,
        ...(solidified
          ? {
              // 3.2 accept gate: review probe cards need a valid registration.
              metadata: {
                solidified: {
                  type: "script",
                  path: "scripts/notification-probe.mjs",
                  status: "pending",
                },
              },
            }
          : {}),
      });
      setReviewEventAge(taskId, age);
      return taskId;
    };
    const markBlocked = (taskId, kind) => {
      const rows = sqlRun(
        "UPDATE tasks SET status='blocked', block_kind=?, block_recurrences=1 WHERE id=?",
        kind,
        taskId,
      );
      if (rows !== 1) throw new Error(`blocked 更新应命中 1 行，实际 ${rows} 行：${taskId}`);
      // A sticky block event keeps later recompute_ready calls (triggered by
      // accept/reject) from promoting these probe cards back out of blocked.
      sqlRun(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
          + "VALUES (?, NULL, 'blocked', '{}', ?)",
        taskId,
        Math.floor(Date.now() / 1000),
      );
      return taskId;
    };
    const setBlocked = async (title, kind) => {
      const taskId = await createTask(title);
      return markBlocked(taskId, kind);
    };

    // One completed decompose parent → two sibling review cards (one bundle).
    const parent = await seedReady("兄弟父卡");
    await patchStatus(parent, "done", { summary: "父卡完成" });
    const siblingA = await seedReview("兄弟卡甲", { parents: [parent], age: 400 });
    const siblingB = await seedReview("兄弟卡乙", { parents: [parent], age: 300 });

    // A review card with an unfinished child → P0 but still a review bundle.
    const reviewWithChild = await seedReview("验收有子卡", { age: 200 });
    await createTask("未完成子卡", [reviewWithChild]);

    const p2a = await seedReview("普通验收甲", { age: 100 });
    const p2b = await seedReview("排队验收乙", { age: 50 });

    // Blocked cards go last: any later state change runs recompute_ready.
    // They are sticky (block event) but stay last for determinism.
    const blockedChildParent = await createTask("下游阻塞卡");
    await createTask("下游未完成子卡", [blockedChildParent]);
    markBlocked(blockedChildParent, "dependency");
    const blockedP0 = await setBlocked("等你回答的卡", "needs_input");
    const blockedP1 = await setBlocked("等依赖的卡", "dependency");

    const snapshotOf = async () =>
      (await httpJson("GET", `${base}/api/workbench/notifications`)).data;
    const sweep = async () => {
      const result = await httpJson("POST", `${base}/api/workbench/notifications/sweep`, {});
      if (result.status !== 200) {
        throw new Error(`通知采集失败: ${result.status} ${JSON.stringify(result.data)}`);
      }
      return result.data;
    };

    const first = await sweep();
    check(
      "采集分类 P0/P1/P2 与验收槽",
      first?.counts?.p0 === 3 &&
        first?.counts?.p1 === 1 &&
        first?.counts?.p2 === 4 &&
        first?.slots?.active?.length === 3 &&
        first?.slots?.queued_count === 1 &&
        first?.slots?.queued_cards === 1,
      JSON.stringify({ counts: first?.counts, slots: first?.slots }),
    );
    const siblingBundle = (first.slots.active || []).find((bundle) => bundle.count === 2);
    check(
      "同 parent 兄弟卡成束",
      Boolean(siblingBundle) &&
        siblingBundle.task_ids.includes(siblingA) &&
        siblingBundle.task_ids.includes(siblingB),
      JSON.stringify(siblingBundle?.task_ids),
    );

    const notifyArgs = existsSync(notifyLog)
      ? readFileSync(notifyLog, "utf8")
      : "";
    check(
      "P0 桌面通知（critical）已发出",
      notifyArgs.includes("--urgency=critical") && notifyArgs.includes("等你回答的卡"),
      notifyArgs.slice(0, 300),
    );

    // ---- launch headless Chrome --------------------------------------
    const debugPort = await freePort();
    chrome = spawn(
      chromeBin,
      [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        `--remote-debugging-port=${debugPort}`,
        `--user-data-dir=${join(tmp, "chrome-profile")}`,
        "--window-size=1440,900",
        `${base}/`,
      ],
      { stdio: ["ignore", "ignore", "pipe"] },
    );

    let page;
    await waitFor(
      async () => {
        try {
          const response = await fetch(`http://127.0.0.1:${debugPort}/json/list`);
          const targets = await response.json();
          page = targets.find(
            (target) => target.type === "page" && target.url.startsWith(base),
          );
          return Boolean(page?.webSocketDebuggerUrl);
        } catch {
          return false;
        }
      },
      { timeout: 20000, label: "Chrome DevTools page" },
    );

    cdp = await cdpConnect(page.webSocketDebuggerUrl);
    await cdp.send("Runtime.enable");
    await cdp.send("Page.enable");

    const evaluate = async (expression) => {
      const result = await cdp.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) {
        throw new Error(`页面执行失败：${JSON.stringify(result.exceptionDetails)}`);
      }
      return result.result?.value;
    };
    const waitForEval = (expression, timeout, label) =>
      waitFor(async () => Boolean(await evaluate(expression)), { timeout, label });

    await waitForEval("Boolean(document.querySelector('.wb-notify-toggle'))", 15000, "通知铃铛");
    const defaultClosed = await evaluate("!document.querySelector('.wb-notify-center')");
    check("通知中心默认收起（不占主界面）", defaultClosed === true);
    await evaluate("document.querySelector('.wb-notify-toggle').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-notify-center'))", 8000, "打开通知中心");

    const layout = await evaluate(`(() => {
      const center = document.querySelector('.wb-notify-center');
      const pinned = document.querySelector('.wb-notify-pinned');
      const panel = document.querySelector('.wb-notify-panel');
      return {
        position: getComputedStyle(center).position,
        top: getComputedStyle(center).top,
        right: getComputedStyle(center).right,
        pinnedFirst: Boolean(pinned) && (
          pinned.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING
        ) > 0,
        p0Items: document.querySelectorAll('.wb-notify-pinned .wb-notify-item.p0').length,
        p0Text: pinned?.textContent || '',
        p1Text: panel?.textContent || '',
        countdowns: [...document.querySelectorAll('.wb-notify-countdown')]
          .map((node) => node.textContent),
        slotsText: document.querySelector('.wb-notify-slots')?.textContent || '',
        dailyText: document.querySelector('.wb-notify-daily')?.textContent || '',
      };
    })()`);
    check(
      "P0 置顶、右上角固定层",
      layout.position === "fixed" &&
        layout.pinnedFirst === true &&
        layout.p0Items === 3 &&
        layout.p0Text.includes("等你回答的卡") &&
        layout.p0Text.includes("验收有子卡") &&
        layout.p0Text.includes("下游阻塞卡") &&
        layout.p0Text.includes("有未完成子卡，下游被堵"),
      JSON.stringify(layout),
    );
    check(
      "通知中心在 app 框内右对齐（007 外壳偏移）",
      layout.right === "38px",
      layout.right,
    );
    check("桌面通知中心 top 84px（顶栏下方）", layout.top === "84px", layout.top);

    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 420,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((resolveWait) => setTimeout(resolveWait, 120));
    const narrowPanel = await evaluate(`(() => {
      const center = document.querySelector('.wb-notify-center');
      const topbar = document.querySelector('.wb-topbar');
      return {
        top: parseFloat(getComputedStyle(center).top),
        topbarBottom: topbar ? topbar.getBoundingClientRect().bottom : 0,
      };
    })()`);
    check(
      "窄屏通知面板不压住顶栏（top >= 63）",
      narrowPanel.top >= 63 && narrowPanel.top >= narrowPanel.topbarBottom,
      JSON.stringify(narrowPanel),
    );
    await cdp.send("Emulation.clearDeviceMetricsOverride");
    await evaluate("document.querySelector('.wb-notify-close').click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-notify-center'))", 5000, "关闭通知中心");
    check("通知中心可关闭", true);
    await evaluate("document.querySelector('.wb-notify-toggle').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-notify-center'))", 8000, "重新打开通知中心");
    check(
      "P1 面板展示 dependency 阻塞",
      layout.p1Text.includes("等依赖的卡"),
      JSON.stringify({ p1: layout.p1Text }),
    );

    const siblingItem = first.p2.find((item) => item.task_id === siblingA);
    const domCountdown = await evaluate(`(() => {
      const item = [...document.querySelectorAll('.wb-notify-item')]
        .find((node) => node.textContent.includes("兄弟卡甲"));
      return item?.querySelector('.wb-notify-countdown')?.textContent || '';
    })()`);
    const parseCountdown = (text) => {
      let match = text.match(/还有\s*(\d+)\s*小时\s*(\d+)\s*分自动收/);
      if (match) return Number(match[1]) * 3600 + Number(match[2]) * 60;
      match = text.match(/还有\s*(\d+)\s*分自动收/);
      return match ? Number(match[1]) * 60 : null;
    };
    const parsedCountdown = parseCountdown(domCountdown);
    check(
      "review 倒计时与 auto_collect_in_seconds 一致",
      typeof siblingItem?.auto_collect_in_seconds === "number" &&
        parsedCountdown !== null &&
        Math.abs(parsedCountdown - siblingItem.auto_collect_in_seconds) <= 180,
      JSON.stringify({ domCountdown, expected: siblingItem?.auto_collect_in_seconds }),
    );
    check(
      "验收槽 3 束 + 排队数 + 兄弟卡成束",
      layout.slotsText.includes("验收槽 3/3 束") &&
        layout.slotsText.includes("排队 1 束 / 1 张") &&
        layout.slotsText.includes("2 张兄弟卡"),
      layout.slotsText,
    );
    check(
      "P2 每日一行",
      layout.dailyText.includes("P2 待验收 4 张") &&
        layout.dailyText.includes("普通验收甲"),
      layout.dailyText,
    );

    const blockedActions = await evaluate(`(() => {
      const item = [...document.querySelectorAll('.wb-notify-item')]
        .find((node) => node.textContent.includes("等你回答的卡"));
      return item ? [...item.querySelectorAll('button')].map((node) => node.textContent.trim()) : [];
    })()`);
    check(
      "非 review 卡只有提醒 + 打开看板",
      blockedActions.includes("10 分钟后再提醒") &&
        blockedActions.includes("打开看板") &&
        !blockedActions.includes("收") &&
        !blockedActions.includes("不收"),
      JSON.stringify(blockedActions),
    );
    const reviewActions = await evaluate(`(() => {
      const item = [...document.querySelectorAll('.wb-notify-item')]
        .find((node) => node.textContent.includes("验收有子卡"));
      return item ? [...item.querySelectorAll('button')].map((node) => node.textContent.trim()) : [];
    })()`);
    check(
      "P0 review 卡有收 / 不收 / 提醒",
      reviewActions.includes("收") &&
        reviewActions.includes("不收") &&
        reviewActions.includes("10 分钟后再提醒"),
      JSON.stringify(reviewActions),
    );

    const theme = await evaluate(`(async () => {
      const root = document.documentElement;
      const center = document.querySelector('.wb-notify-center');
      const title = document.querySelector('.wb-notify-title');
      root.dataset.theme = 'light';
      await new Promise((resolveWait) => setTimeout(resolveWait, 60));
      const light = getComputedStyle(center).backgroundColor;
      const lightFg = getComputedStyle(title).color;
      root.dataset.theme = 'dark';
      await new Promise((resolveWait) => setTimeout(resolveWait, 60));
      return {
        light,
        dark: getComputedStyle(center).backgroundColor,
        lightFg,
        darkFg: getComputedStyle(title).color,
      };
    })()`);
    check(
      "通知中心明暗主题走 token",
      theme.light !== theme.dark && theme.lightFg !== theme.darkFg,
      JSON.stringify(theme),
    );

    // ---- new card after mount + external sweep + 5s UI poll -----------
    await seedReview("轮询新验收", { age: 30 });
    // No solidified registration: accept must be blocked (3.2 gate).
    const noSolid = await seedReview("缺固化验收", {
      age: 20,
      solidified: false,
    });
    await sweep();
    await waitForEval(
      `[...document.querySelectorAll('.wb-notify-item')]
        .some((node) => node.textContent.includes("轮询新验收"))`,
      12000,
      "5s 轮询出现新 review 卡",
    );
    check("挂载后新 review 卡经 5s 轮询出现", true);
    await waitForEval(
      `[...document.querySelectorAll('.wb-notify-item')]
        .some((node) => node.textContent.includes("缺固化验收"))`,
      12000,
      "缺固化卡出现在通知中心",
    );

    // ---- two-phase logger: non-zero exit surfaces desktop.last_error ----
    writeFileSync(notifyFailFlag, "1");
    await setBlocked("桌面失败卡", "needs_input");
    await sweep();
    await waitForEval(
      "Boolean(document.querySelector('.wb-notify-desktop-error'))",
      12000,
      "桌面通知失败提示",
    );
    const desktopError = await evaluate(
      "document.querySelector('.wb-notify-desktop-error')?.textContent || ''",
    );
    check(
      "notify 非零退出显示桌面错误",
      desktopError.includes("桌面通知失败") &&
        desktopError.includes("forced notify failure"),
      desktopError,
    );

    const clickInItem = (title, label) =>
      evaluate(`(() => {
        const item = [...document.querySelectorAll('.wb-notify-item')]
          .find((node) => node.textContent.includes(${JSON.stringify(title)}));
        if (!item) return { ok: false, reason: 'item' };
        const button = [...item.querySelectorAll('button')]
          .find((node) => node.textContent.trim() === ${JSON.stringify(label)});
        if (!button) {
          return {
            ok: false,
            reason: 'button',
            labels: [...item.querySelectorAll('button')].map((node) => node.textContent.trim()),
          };
        }
        button.click();
        return { ok: true };
      })()`);

    // ---- remind (P0 blocked) ------------------------------------------
    const remindClick = await clickInItem("等你回答的卡", "10 分钟后再提醒");
    check("点击 10 分钟后再提醒", remindClick.ok === true, JSON.stringify(remindClick));
    await waitFor(
      async () => {
        const snapshot = await snapshotOf();
        const item = snapshot?.p0?.find((entry) => entry.task_id === blockedP0);
        return typeof item?.snooze_until === "number";
      },
      { timeout: 8000, interval: 150, label: "remind 写入 snooze_until" },
    );
    await waitForEval(
      "[...document.querySelectorAll('.wb-notify-snoozed')].some((node) => node.textContent.includes('已稍后提醒'))",
      8000,
      "UI 显示已稍后提醒",
    );
    check("remind 后仍展示且标记已稍后提醒", true);

    // ---- reject (P2 queued card) --------------------------------------
    const rejectClick = await clickInItem("排队验收乙", "不收");
    check("P2 review 卡打开不收理由", rejectClick.ok === true, JSON.stringify(rejectClick));
    await waitForEval("Boolean(document.querySelector('.wb-notify-reason'))", 5000, "理由输入框");
    const emptyDisabled = await evaluate(
      "[...document.querySelectorAll('.wb-notify-reason-actions button')].find((node) => node.textContent.includes('提交不收'))?.disabled",
    );
    check("空理由不能提交", emptyDisabled === true, String(emptyDisabled));
    await evaluate(`(() => {
      const area = document.querySelector('.wb-notify-reason');
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value'
      ).set;
      setter.call(area, '请补测试再交');
      area.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.wb-notify-reason-actions button')]
        .find((node) => node.textContent.includes('提交不收'));
      button.click();
      return true;
    })()`);
    await waitFor(
      async () =>
        (await httpJson("GET", taskUrl(p2b))).data?.task?.status === "triage",
      { timeout: 10000, interval: 150, label: "不收后卡回 triage" },
    );
    await waitFor(
      async () => {
        const snapshot = await snapshotOf();
        return ![...snapshot.p2, ...snapshot.p0, ...snapshot.p1].some(
          (item) => item.task_id === p2b,
        );
      },
      { timeout: 8000, interval: 150, label: "不收后通知移除" },
    );
    check("通知内不收 → comment + triage + 移除通知", true);

    // ---- accept blocked without a valid solidified registration --------
    const blockedAccept = await clickInItem("缺固化验收", "收");
    check(
      "点击缺固化 review 卡的收",
      blockedAccept.ok === true,
      JSON.stringify(blockedAccept),
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-notify-error')].some(
        (node) => node.textContent.includes("没有可用的固化记录"),
      )`,
      8000,
      "缺固化收被阻断提示",
    );
    check(
      "缺固化在通知中心收被阻断，卡留在 review",
      (await httpJson("GET", taskUrl(noSolid))).data?.task?.status === "review",
    );

    // ---- accept sibling review ----------------------------------------
    const acceptClick = await clickInItem("兄弟卡甲", "收");
    check("点击验收槽 review 卡的收", acceptClick.ok === true, JSON.stringify(acceptClick));
    await waitFor(
      async () => (await httpJson("GET", taskUrl(siblingA))).data?.task?.status === "done",
      { timeout: 10000, interval: 150, label: "验收后 siblingA done" },
    );
    await waitFor(
      async () => {
        const snapshot = await snapshotOf();
        return ![...snapshot.p2, ...snapshot.p0].some((item) => item.task_id === siblingA);
      },
      { timeout: 8000, interval: 150, label: "accept 后通知移除" },
    );
    check("通知内收 → done + 移除通知", true);

    // ---- plugin host: no center, no polling ---------------------------
    const reactUmd = readFileSync(
      join(uiRoot, "node_modules/react/umd/react.production.min.js"),
      "utf8",
    );
    const reactDomUmd = readFileSync(
      join(uiRoot, "node_modules/react-dom/umd/react-dom.production.min.js"),
      "utf8",
    );
    const pluginBundle = readFileSync(join(uiRoot, "dist/plugin/index.js"), "utf8");

    await cdp.send("Page.navigate", { url: `${base}/healthz` });
    await waitFor(
      async () => {
        try {
          return (await evaluate("document.readyState")) === "complete";
        } catch {
          return false;
        }
      },
      { timeout: 10000, label: "plugin probe document" },
    );
    await evaluate(reactUmd);
    await evaluate(reactDomUmd);
    await evaluate(`(() => {
      document.body.innerHTML = '<div id="workbench-root"></div>';
      window.__notifyFetches = 0;
      window.__HERMES_PLUGIN_SDK__ = {
        React: window.React,
        ReactDOM: window.ReactDOM,
        fetchJSON: async (url, init) => {
          if (String(url).includes('/api/workbench/notifications')) window.__notifyFetches += 1;
          return (await fetch(url, init)).json();
        },
      };
      window.__HERMES_PLUGINS__ = {
        register: (name, component) => {
          window.__wbPlugin = { name };
          window.ReactDOM.createRoot(document.getElementById("workbench-root"))
            .render(window.React.createElement(component));
        },
      };
      return true;
    })()`);
    await evaluate(pluginBundle);
    await waitForEval(
      "document.querySelectorAll('.wb-board .wb-col').length === 8",
      15000,
      "插件宿主看板 8 列",
    );
    await new Promise((resolveWait) => setTimeout(resolveWait, 11000));
    const pluginState = await evaluate(`(() => ({
      registered: window.__wbPlugin?.name || '',
      notifyCenter: Boolean(document.querySelector('.wb-notify-center')),
      notifyFetches: window.__notifyFetches,
    }))()`);
    check(
      "插件宿主无通知中心且不轮询",
      pluginState.registered === "workbench" &&
        pluginState.notifyCenter === false &&
        pluginState.notifyFetches === 0,
      JSON.stringify(pluginState),
    );

    console.log(`\nNOTIFICATION PROBE OK — ${checks.length} checks passed`);
  } finally {
    if (cdp) cdp.close();
    if (chrome && chrome.exitCode === null) chrome.kill("SIGKILL");
    if (server && server.exitCode === null) server.kill("SIGTERM");
    await new Promise((resolveWait) => setTimeout(resolveWait, 200));
    rmSync(tmp, { recursive: true, force: true });
    if (serverLog && serverLog.trim()) {
      console.error("\n--- server log ---\n" + serverLog.trim());
    }
  }
}

main().catch((error) => {
  console.error("\n" + (error?.stack || error));
  process.exit(1);
});
