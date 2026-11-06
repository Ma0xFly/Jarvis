#!/usr/bin/env node
/**
 * Headless probe for Story 1.3/1.4 (todo three columns + dispatch receipt).
 *
 * Boots an isolated workbench server (throwaway HERMES_KANBAN_HOME /
 * HERMES_KANBAN_DB / WORKBENCH_DB), seeds cards through the public REST
 * surfaces, then drives the standalone UI in headless Chrome over CDP. No npm
 * dependencies: Node 22 ships a global WebSocket and fetch.
 *
 * Covered matrix rows:
 *   - three column order / counts == rendered cards
 *   - repository card shows only title + acceptance, no status decorations
 *   - empty acceptance drop: column highlight, 300ms shake, red hint, 0 request
 *   - acceptance inline edit writes through to workbench.db
 *   - dispatch with acceptance leaves the repository and reaches ready
 *   - receipt live region is always mounted; pending「正在派发…」at drop then
 *     the final text verbatim from the dispatch response (controlled hold,
 *     no wall-clock latency assertion)
 *   - dispatch is globally serialized: a second drop while one is in flight is
 *     ignored with「正在派发上一张」and sends no request
 *   - receipts showing during 409 / empty-acceptance rejections are cleared
 *     immediately while the existing toast / shake stays intact
 *   - latest-attempt replacement, dismiss button, and RECEIPT_MS auto-hide
 *   - ready cards show 1-based「第 N 位」from the same /board query the UI uses
 *   - external queue change shifts inline positions on the next 5s poll
 *   - refresh re-derives positions from /board only (no receipt / ETA)
 *   - already-running re-dispatch shows「已在运行」without a position (poll paused)
 *   - badge contrast >= 4.5:1 in both themes
 *   - new card lands in today as a todo store card, empty acceptance, not auto-dispatched
 *   - 409 parent: toast「已回到仓库，等 parent 完成」and card stays in repository
 *   - 007 shell: 24px canvas + 12px app frame, 224px sidebar, 50px topbar,
 *     three real nav entries (no 活动/统计), tabs + 验收 + theme toggle
 *   - theme: inline first-paint script before stylesheet, toggle updates
 *     data-theme / localStorage / theme-color, reload keeps the theme
 *   - 已完成: empty text, done column grouped by local completed_at day
 *     (今天 → 昨天), completed time + session entry, missing session prompt
 *   - board: 8 columns keep the native names/order (no 007 five-column IA)
 *   - plugin host: board only, no sidebar / todo / done / theme toggle
 *
 * Usage: node scripts/todo-probe.mjs
 *   CHROME_BIN, WORKBENCH_PYTHON override auto-discovery.
 */
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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

/** 与 TodoPage 的 RECEIPT_MS 保持一致（回执自动消失时长）。 */
const RECEIPT_MS = 6000;
/** 与 api/workbench.ts 的 DISPATCH_TIMEOUT_MS 保持一致。 */
const DISPATCH_TIMEOUT_MS = 5000;
/** 派发快照必须与 defaults.json 的 budget 三键一致。 */
const DEFAULTS = JSON.parse(readFileSync(defaultsPath, "utf8"));

/**
 * Runs before app code on the reloaded document. Records the 5s board poll and
 * exposes pause/resume so the running-race test can stop polling around its
 * direct kanban DB write.
 */
const POLL_INTERVAL_SOURCE = `(() => {
  const intervals = [];
  const originalSetInterval = window.setInterval.bind(window);
  window.setInterval = (fn, ms, ...args) => {
    const id = originalSetInterval(fn, ms, ...args);
    intervals.push({ id, fn, ms, args, resumed: false });
    return id;
  };
  window.__wbPausePoll = () => {
    for (const entry of intervals) {
      if (entry.ms === 5000) {
        clearInterval(entry.id);
        entry.resumed = false;
      }
    }
  };
  window.__wbResumePoll = () => {
    for (const entry of intervals) {
      if (entry.ms === 5000 && !entry.resumed) {
        entry.id = originalSetInterval(entry.fn, entry.ms, ...entry.args);
        entry.resumed = true;
      }
    }
  };
})();`;

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
    headers: payload === undefined ? { Accept: "application/json" } : {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
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
  const tmp = mkdtempSync(join(tmpdir(), "wb-todo-probe-"));
  const defaults = join(tmp, "defaults.json");
  writeFileSync(defaults, readFileSync(defaultsPath));
  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
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

  // Direct writes to the throwaway kanban DB: running / priority transitions are
  // kernel-only through the public REST surface, but the probe needs them to
  // exercise the running receipt and an external queue change. The server is
  // pinned to the exact same file via HERMES_KANBAN_DB.
  const kanbanDb = join(tmp, "kanban-home", "kanban.db");
  const updateKanban = (sql, taskId, value) => {
    const output = execFileSync(
      python,
      [
        "-I",
        "-c",
        `import sqlite3,sys; conn=sqlite3.connect(sys.argv[1], timeout=5); ` +
          `cur=conn.execute(${JSON.stringify(sql)}, (sys.argv[2], sys.argv[3])); ` +
          `conn.commit(); print(cur.rowcount); conn.close()`,
        kanbanDb,
        String(value),
        taskId,
      ],
      { encoding: "utf8" },
    );
    const rows = Number(String(output).trim());
    if (rows !== 1) {
      throw new Error(`kanban UPDATE 应命中 1 行，实际 ${rows} 行：${sql} / ${taskId}`);
    }
    return rows;
  };
  const setKanbanStatus = (taskId, status) =>
    updateKanban("UPDATE tasks SET status=? WHERE id=?", taskId, status);
  const setKanbanPriority = (taskId, priority) =>
    updateKanban("UPDATE tasks SET priority=? WHERE id=?", taskId, priority);

  try {
    server = spawn(python, ["-I", serverPath, "--host", "127.0.0.1", "--port", String(port)], {
      env: {
        ...process.env,
        HERMES_KANBAN_HOME: join(tmp, "kanban-home"),
        HERMES_KANBAN_DB: join(tmp, "kanban-home", "kanban.db"),
        WORKBENCH_DB: join(tmp, "workbench.db"),
        WORKBENCH_DEFAULTS: defaults,
        // Notification polling is covered by notification-probe.mjs; keep
        // this probe free of background desktop notifications.
        WORKBENCH_NOTIFY_SWEEP_SECONDS: "0",
        WORKBENCH_NOTIFY_COMMAND: "true",
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

    // The standalone host must serve the fresh build, not stale artifacts.
    const bundle = await fetch(`${base}/static/index.js`).then((response) => response.text());
    check(
      "独立版产物包含待办三列",
      bundle.includes("wb-todo-cname") && bundle.includes("wb-todo-flow"),
      "先运行 npm run build",
    );
    check(
      "独立版产物包含 007 外壳与已完成视图",
      ["wb-shell", "wb-sidebar", "wb-topbar", "wb-theme-toggle", "wb-done-wrap", "wb-done-session"].every(
        (name) => bundle.includes(name),
      ),
      "先运行 npm run build",
    );
    const standaloneHtml = await fetch(`${base}/`).then((response) => response.text());
    check(
      "首屏内联脚本先于样式表定主题（无闪烁）",
      standaloneHtml.includes("wb-theme") &&
        standaloneHtml.indexOf("wb-theme") <
          standaloneHtml.indexOf('<link rel="stylesheet" href="/static/theme.css"'),
      "standalone.html 的 inline theme 必须在 theme.css 之前",
    );

    // ---- seed through public REST -------------------------------------
    const createUpstream = async (title, parents = []) => {
      const result = await httpJson("POST", `${base}/api/plugins/kanban/tasks`, {
        title,
        body: "probe",
        triage: true,
        parents,
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
      return result.data.metadata;
    };

    const emptyCard = await createUpstream("空验收卡");
    await setMetadata(emptyCard, {
      bucket: "today",
      contract: { input: "", accept: "", ready: false },
    });

    const readyCard = await createUpstream("有验收卡");
    await setMetadata(readyCard, { bucket: "today", contract: { accept: "跑通测试" } });

    const longCard = await createUpstream("长期卡");
    await setMetadata(longCard, { bucket: "long", contract: { accept: "交付文档" } });

    const parent = await createUpstream("未完成 parent");
    const blockedChild = await createUpstream("等 parent 卡", [parent]);
    await setMetadata(blockedChild, { bucket: "today", contract: { accept: "子任务标准" } });

    // 只进看板、不进仓库：给对比度矩阵提供一个 .wb-block 样本。
    const blockTagCard = await createUpstream("看板阻塞标签卡");
    updateKanban("UPDATE tasks SET status=? WHERE id=?", blockTagCard, "blocked");
    updateKanban("UPDATE tasks SET block_kind=? WHERE id=?", blockTagCard, "needs_input");

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

    const readReceiptText = () =>
      evaluate("document.querySelector('.wb-todo-receipt-text')?.textContent || ''");

    const readReceipt = () =>
      evaluate(`(() => {
        const node = document.querySelector('.wb-todo-receipt');
        return node
          ? {
              text: node.querySelector('.wb-todo-receipt-text')?.textContent || '',
              visible: node.classList.contains('visible'),
              pending: node.classList.contains('pending'),
              running: node.classList.contains('running'),
              role: node.getAttribute('role'),
              dismissLabel: node.querySelector('.wb-todo-receipt-dismiss')?.getAttribute('aria-label') || '',
            }
          : null;
      })()`);

    const readFlowSegment = (label) =>
      evaluate(`(() => {
        const seg = [...document.querySelectorAll('.wb-todo-seg')].find(
          (node) => node.querySelector('.wb-todo-seghead > span:first-child, .wb-todo-seghead > button:first-child')?.textContent === ${JSON.stringify(label)},
        );
        if (!seg) return [];
        return [...seg.querySelectorAll('.wb-todo-flow-card')].map((card) => ({
          title: card.querySelector('.wb-todo-title')?.textContent || '',
          badge: card.querySelector('.wb-todo-badge')?.textContent || '',
        }));
      })()`);

    const readReadyBadges = () => readFlowSegment("队列");

    /** 在明暗两套 token 下量一组选择器的文本对比度（含透明背景向上合成）。 */
    const contrastMatrix = (selectors, { forceActiveReview = false } = {}) =>
      evaluate(`(() => {
        const parse = (value) => {
          const rgba = value.match(/^rgba?\\(([^)]+)\\)$/);
          if (rgba) {
            const parts = rgba[1].split(/[ ,\\/]+/).filter(Boolean).map(Number);
            return { r: parts[0], g: parts[1], b: parts[2], a: parts.length > 3 ? parts[3] : 1 };
          }
          const srgb = value.match(/^color\\(srgb ([^)]+)\\)$/);
          if (srgb) {
            const parts = srgb[1].split(/[ \\/]+/).filter(Boolean).map(Number);
            return {
              r: parts[0] * 255,
              g: parts[1] * 255,
              b: parts[2] * 255,
              a: parts.length > 3 ? parts[3] : 1,
            };
          }
          return null;
        };
        const over = (top, bottom) => ({
          r: top.r * top.a + bottom.r * (1 - top.a),
          g: top.g * top.a + bottom.g * (1 - top.a),
          b: top.b * top.a + bottom.b * (1 - top.a),
          a: 1,
        });
        const lum = (color) => {
          const channel = (value) => {
            const n = value / 255;
            return n <= 0.03928 ? n / 12.92 : Math.pow((n + 0.055) / 1.055, 2.4);
          };
          return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
        };
        const ratio = (a, b) => {
          const l1 = lum(a);
          const l2 = lum(b);
          return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        };
        const backdrop = (node) => {
          const layers = [];
          for (let cur = node; cur; cur = cur.parentElement) {
            const bg = parse(getComputedStyle(cur).backgroundColor);
            if (bg && bg.a > 0) layers.push(bg);
            if (bg && bg.a >= 1) break;
          }
          let result = { r: 255, g: 255, b: 255, a: 1 };
          for (let index = layers.length - 1; index >= 0; index -= 1) {
            result = over(layers[index], result);
          }
          return result;
        };
        const contrastOf = (selector) => {
          const node = document.querySelector(selector);
          if (!node) return null;
          const text = parse(getComputedStyle(node).color);
          if (!text) return null;
          const bg = backdrop(node);
          return ratio(text.a < 1 ? over(text, bg) : text, bg);
        };
        const root = document.documentElement;
        // 背景 token 有 250ms 过渡：测量时禁掉，避免读到切换中途的颜色。
        const freeze = document.createElement('style');
        freeze.textContent = '*,*::before,*::after{transition:none !important;animation:none !important}';
        document.head.appendChild(freeze);
        const reviewNode = document.querySelector('.wb-review-entry');
        const reviewWasActive = reviewNode?.classList.contains('active') ?? false;
        ${forceActiveReview ? "reviewNode?.classList.add('active');" : ""}
        const selectors = ${JSON.stringify(selectors)};
        const result = {};
        for (const theme of ['light', 'dark']) {
          root.dataset.theme = theme;
          result[theme] = {};
          for (const key of Object.keys(selectors)) {
            result[theme][key] = contrastOf(selectors[key]);
          }
        }
        root.dataset.theme = 'dark';
        if (reviewNode && !reviewWasActive) reviewNode.classList.remove('active');
        freeze.remove();
        return result;
      })()`);

    // Must match the exact query the UI polls (no include_archived).
    const boardReadyTasks = async () => {
      const board = (await httpJson("GET", `${base}/api/plugins/kanban/board`)).data;
      return board?.columns?.find((column) => column.name === "ready")?.tasks || [];
    };

    const expectedBadges = (tasks) =>
      tasks.map((task, index) => ({ title: task.title, badge: `第 ${index + 1} 位` }));

    /** ready 段标题/位次必须与 /board 的 ready 顺序逐项一致。 */
    const expectBadgesMatch = async (label) => {
      await waitFor(
        async () => {
          const expected = expectedBadges(await boardReadyTasks());
          const actual = await readReadyBadges();
          return actual.length === expected.length &&
            JSON.stringify(actual) === JSON.stringify(expected);
        },
        { timeout: 9000, interval: 200, label },
      );
      check(label, true);
    };

    const dragCardToFlow = (title) =>
      evaluate(`(async () => {
        const card = [...document.querySelectorAll('.wb-todo-repo-card')]
          .find((node) => node.textContent.includes(${JSON.stringify(title)}));
        if (!card) return false;
        const flow = document.querySelector('.wb-todo-flow');
        const dataTransfer = new DataTransfer();
        card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
        flow.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
        await new Promise((resolveWait) => setTimeout(resolveWait, 50));
        flow.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
        return true;
      })()`);

    /** 仓库互拖：把卡片拖到「今日/长期」仓库，只改 bucket，不派发。 */
    const dragRepoCardToStore = (title, storeLabel) =>
      evaluate(`(async () => {
        const card = [...document.querySelectorAll('.wb-todo-repo-card')]
          .find((node) => node.textContent.includes(${JSON.stringify(title)}));
        const section = [...document.querySelectorAll('.wb-todo-plane')]
          .find((node) => node.querySelector('.wb-todo-cname')?.textContent === ${JSON.stringify(storeLabel)});
        if (!card || !section) return false;
        const dataTransfer = new DataTransfer();
        card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
        section.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
        await new Promise((resolveWait) => setTimeout(resolveWait, 50));
        section.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
        return true;
      })()`);

    /** 分区右上角「＋ 新建」：打开该分区内的创建信息卡。 */
    const openCreateCard = (storeLabel) =>
      evaluate(`(async () => {
        const section = [...document.querySelectorAll('.wb-todo-plane')]
          .find((node) => node.querySelector('.wb-todo-cname')?.textContent === ${JSON.stringify(storeLabel)});
        const button = section?.querySelector('.wb-todo-newbtn');
        if (!button) return false;
        button.click();
        await new Promise((resolveWait) => setTimeout(resolveWait, 80));
        return Boolean(section.querySelector('.wb-todo-create-card'));
      })()`);

    /** 新建卡字段：input/textarea 都走原生 setter + input 事件。 */
    const setCreateField = (selector, value) =>
      evaluate(`(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return false;
        const proto = node.tagName === 'TEXTAREA'
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, ${JSON.stringify(value)});
        node.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()`);

    const submitCreateCard = () =>
      evaluate(`(() => {
        const button = document.querySelector('.wb-todo-create-btn.primary');
        if (!button) return false;
        button.click();
        return true;
      })()`);

    /** 看板：把卡片拖到指定列，返回 dragover 是否被接收（preventDefault）。 */
    const dragBoardCardToColumn = (title, columnLabel) =>
      evaluate(`(async () => {
        const card = [...document.querySelectorAll('.wb-card')]
          .find((node) => node.textContent.includes(${JSON.stringify(title)}));
        const column = [...document.querySelectorAll('.wb-col')]
          .find((node) => node.querySelector('.wb-cname')?.textContent === ${JSON.stringify(columnLabel)});
        if (!card || !column) return { ok: false };
        const dataTransfer = new DataTransfer();
        card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
        const over = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer });
        column.dispatchEvent(over);
        await new Promise((resolveWait) => setTimeout(resolveWait, 50));
        column.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
        return { ok: true, overPrevented: over.defaultPrevented };
      })()`);

    const boardToastText = () =>
      evaluate("document.querySelector('.wb-board-wrap .wb-toast')?.textContent || ''");

    // Instrument workbench calls; also keeps the dispatch response so the probe
    // can compare the receipt text with what the backend actually returned.
    const installProbe = () =>
      evaluate(`(() => {
        window.__wbProbe = {
          dispatch: 0,
          create: 0,
          metadata: 0,
          version: 0,
          lastDispatch: null,
          holdMs: 0,
          fakeVersion: null,
        };
        const original = window.fetch.bind(window);
        window.fetch = (input, init) => {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          if (url.includes("/api/workbench/version")) {
            window.__wbProbe.version += 1;
            if (window.__wbProbe.fakeVersion) {
              return Promise.resolve(new Response(
                JSON.stringify({ ok: true, version: window.__wbProbe.fakeVersion }),
                { status: 200, headers: { "Content-Type": "application/json" } },
              ));
            }
          }
          if (url.includes("/api/workbench/dispatch/")) {
            window.__wbProbe.dispatch += 1;
            if (window.__wbProbe.hangDispatch) {
              // Never settles: exercises the client-side dispatch timeout.
              return new Promise(() => {});
            }
          }
          const promise = original(input, init);
          if (url.includes("/api/workbench/dispatch/")) {
            promise.then((response) => {
              response.clone().json()
                .then((data) => { window.__wbProbe.lastDispatch = data; })
                .catch(() => {});
            }).catch(() => {});
            // 受控响应：给探针时间观察 pending 状态，不依赖机器快慢。
            const hold = window.__wbProbe.holdMs;
            if (hold > 0) {
              return promise.then((response) => new Promise((resolveHold) => {
                setTimeout(() => resolveHold(response), hold);
              }));
            }
          } else if (/\\/api\\/workbench\\/metadata\\/[^?]+$/.test(url)) {
            window.__wbProbe.metadata += 1;
          } else if (url.endsWith("/api/workbench/tasks")) {
            window.__wbProbe.create += 1;
          }
          return promise;
        };
        return true;
      })()`);

    await waitForEval(
      "document.querySelectorAll('.wb-todo-col').length === 3 && document.querySelectorAll('.wb-todo-repo-card').length === 4",
      15000,
      "待办三列与仓库卡渲染",
    );

    // 007 待办视觉：面板有底色/边框，仓库卡无边框透明底，流水线是凹槽。
    const todoVisual = await evaluate(`(() => {
      const plane = document.querySelector('.wb-todo-plane');
      const repo = document.querySelector('.wb-todo-repo-card');
      const flow = document.querySelector('.wb-todo-flow');
      return {
        planeBg: getComputedStyle(plane).backgroundColor,
        planeBorder: getComputedStyle(plane).borderTopWidth,
        repoBorder: getComputedStyle(repo).borderTopWidth,
        repoBg: getComputedStyle(repo).backgroundColor,
        flowBg: getComputedStyle(flow).backgroundColor,
      };
    })()`);
    check(
      "待办 007 面板 / 轻卡片 / 凹槽流水线",
      todoVisual.planeBorder === "1px" &&
        todoVisual.planeBg !== "rgba(0, 0, 0, 0)" &&
        todoVisual.repoBorder === "0px" &&
        todoVisual.repoBg === "rgba(0, 0, 0, 0)" &&
        todoVisual.flowBg !== todoVisual.planeBg,
      JSON.stringify(todoVisual),
    );

    // ---- 首屏主题：index.js 未运行时就已把 dark 写进 DOM ----
    await evaluate("window.localStorage.removeItem('wb-theme'); true");
    await cdp.send("Network.enable");
    // SW 是 network-first，但断网时回退缓存；先绕过它再屏蔽 bundle。
    await cdp.send("Network.setBypassServiceWorker", { bypass: true });
    await cdp.send("Network.setBlockedURLs", { urls: ["*index.js*"] });
    await cdp.send("Emulation.setEmulatedMedia", {
      media: "",
      features: [{ name: "prefers-color-scheme", value: "dark" }],
    });
    await cdp.send("Page.reload", { ignoreCache: true });
    await waitForEval("document.readyState === 'complete'", 10000, "首屏 bootstrap 文档");
    const firstPaint = await evaluate(`(() => ({
      theme: document.documentElement.dataset.theme || '',
      bodyBg: getComputedStyle(document.body).backgroundColor,
      bundleMounted: Boolean(document.querySelector('.wb-root')) || Boolean(window.__wbProbe),
    }))()`);
    check(
      "首屏 bundle 前已定 dark（无 localStorage、系统 dark）",
      firstPaint.theme === "dark" &&
        firstPaint.bodyBg === "rgb(7, 8, 9)" &&
        firstPaint.bundleMounted === false,
      JSON.stringify(firstPaint),
    );
    await cdp.send("Network.setBlockedURLs", { urls: [] });
    await cdp.send("Network.setBypassServiceWorker", { bypass: false });
    await cdp.send("Emulation.setEmulatedMedia", { media: "", features: [] });
    await cdp.send("Page.reload", { ignoreCache: true });
    await waitForEval(
      "document.querySelectorAll('.wb-todo-col').length === 3",
      15000,
      "首屏测试后恢复待办",
    );

    // ---- 0. 007 shell: canvas frame + 224px sidebar + 50px topbar ----
    const shellState = await evaluate(`(() => {
      const root = document.querySelector('.wb-root');
      const shell = document.querySelector('.wb-shell');
      const sidebar = document.querySelector('.wb-sidebar');
      const topbar = document.querySelector('.wb-topbar');
      const views = document.querySelector('.wb-views');
      const nav = [...document.querySelectorAll('.wb-nav-btn')];
      return {
        rootPadding: getComputedStyle(root).padding,
        shellBorderRadius: getComputedStyle(shell).borderTopLeftRadius,
        shellBorderWidth: getComputedStyle(shell).borderTopWidth,
        sidebarWidth: sidebar.getBoundingClientRect().width,
        topbarHeight: topbar.getBoundingClientRect().height,
        views: Boolean(views),
        navLabels: nav.map((node) => node.querySelector('span')?.textContent || ''),
        navCounts: nav.map((node) => node.querySelector('.wb-nav-count')?.textContent || ''),
        tabs: [...document.querySelectorAll('.wb-tab-label')].map((node) => node.textContent),
        themeToggle: Boolean(document.querySelector('.wb-theme-toggle')),
        reviewEntry: document.querySelector('.wb-review-entry')?.textContent.replace(/\\s+/g, ' ').trim() || '',
        hasOldNav: ['活动', '统计'].some((label) => nav.some((node) => node.textContent.includes(label))),
      };
    })()`);
    check(
      "standalone 画布 24px + 圆角 app 框",
      shellState.rootPadding === '24px' &&
        shellState.shellBorderRadius === '12px' &&
        shellState.shellBorderWidth === '1px',
      JSON.stringify(shellState),
    );
    check(
      "侧栏 224px、顶栏 50px、视图容器存在",
      Math.round(shellState.sidebarWidth) === 224 &&
        Math.round(shellState.topbarHeight) === 50 &&
        shellState.views === true,
      JSON.stringify(shellState),
    );
    check(
      "侧栏只有待办/看板/已完成三个真实入口",
      JSON.stringify(shellState.navLabels) === JSON.stringify(["待办", "看板", "已完成"]) &&
        shellState.hasOldNav === false,
      JSON.stringify(shellState),
    );
    check(
      "侧栏计数 = 可见数据（4 待办 / 0 运行 / 0 已完成）",
      JSON.stringify(shellState.navCounts) === JSON.stringify(["4", "0 运行", "0"]),
      JSON.stringify(shellState.navCounts),
    );
    check(
      "顶栏三个 tab + 验收 + 主题切换",
      JSON.stringify(shellState.tabs) === JSON.stringify(["待办", "看板", "已完成"]) &&
        shellState.reviewEntry.includes("验收") &&
        shellState.themeToggle === true,
      JSON.stringify(shellState),
    );

    // 侧栏三个入口各点一次：对应面板必须成为唯一可见 pane。
    const navViews = [];
    for (const label of ["待办", "看板", "已完成"]) {
      const clicked = await evaluate(`(() => {
        const button = [...document.querySelectorAll('.wb-nav-btn')].find(
          (node) => node.querySelector('span')?.textContent === ${JSON.stringify(label)},
        );
        if (!button) return false;
        button.click();
        return true;
      })()`);
      await new Promise((resolveWait) => setTimeout(resolveWait, 150));
      const pane = await evaluate(`(() => {
        const visible = document.querySelector('.wb-pane:not(.wb-pane-hidden)');
        return {
          todo: Boolean(visible?.querySelector('.wb-todo-wrap')),
          board: Boolean(visible?.querySelector('.wb-board-wrap')),
          done: Boolean(visible?.querySelector('.wb-done-wrap')),
          current: visible?.querySelector('[aria-current="page"]')?.textContent || '',
        };
      })()`);
      navViews.push({ label, clicked, ...pane });
    }
    check(
      "侧栏入口切到对应 pane",
      navViews.every(
        (view) =>
          view.clicked === true &&
          ((view.label === "待办" && view.todo && !view.board && !view.done) ||
            (view.label === "看板" && view.board && !view.todo && !view.done) ||
            (view.label === "已完成" && view.done && !view.todo && !view.board)),
      ),
      JSON.stringify(navViews),
    );

    // 从看板点「新建待办」：切回待办并打开今日新建信息卡。
    await evaluate(`(() => {
      const button = [...document.querySelectorAll('.wb-nav-btn')].find(
        (node) => node.querySelector('span')?.textContent === '看板',
      );
      button?.click();
      return true;
    })()`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
    await evaluate("document.querySelector('.wb-newbtn').click(); true");
    await waitForEval(
      "document.querySelector('.wb-pane:not(.wb-pane-hidden) .wb-todo-create-card') !== null && document.activeElement === document.querySelector('.wb-todo-create-title')",
      5000,
      "新建待办打开今日新建卡",
    );
    check("新建待办切到待办并打开今日新建卡", true);
    check(
      "新建待办后待办入口为 aria-current",
      (await evaluate(
        "document.querySelector('.wb-nav-btn[aria-current=\"page\"]')?.querySelector('span')?.textContent || ''",
      )) === "待办",
    );
    // 收起草稿，避免影响后续卡片计数与互拖用例。
    await evaluate(
      " [...document.querySelectorAll('.wb-todo-create-btn')].find((node) => node.textContent.trim() === '取消')?.click(); true",
    );
    await waitForEval(
      "!document.querySelector('.wb-todo-create-card')",
      3000,
      "关闭侧栏新建卡",
    );

    // 验收 0：入口仍然在，打开后必须停在空态，而不是 3 秒后像消失。
    const emptyVerifyOpen = await evaluate("document.querySelector('.wb-review-entry').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-verify-empty'))", 8000, "验收 0 空态");
    const emptyVerifyText = await evaluate(
      "document.querySelector('.wb-verify-empty')?.textContent || ''",
    );
    check(
      "验收 0 打开明确空态",
      emptyVerifyOpen === true && emptyVerifyText.includes("现在没有待验收的卡"),
      emptyVerifyText,
    );
    await evaluate("document.querySelector('.wb-verify-empty .wb-btn')?.click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-verify-overlay'))", 5000, "验收空态关闭");
    check("验收空态可关闭", true);

    // Instrument the workbench calls made after this point.
    await installProbe();

    // ---- 1. column order and counts ----------------------------------
    const columns = await evaluate(
      "[...document.querySelectorAll('.wb-todo-cname')].map((node) => node.textContent)",
    );
    check(
      "列顺序 流水线→今日→长期",
      JSON.stringify(columns) === JSON.stringify(["流水线", "今日", "长期"]),
      JSON.stringify(columns),
    );

    const columnInfo = await evaluate(`(() => {
      return [...document.querySelectorAll('.wb-todo-col')].map((column) => ({
        count: Number(column.querySelector('.wb-todo-count').textContent || 0),
        cards: column.querySelectorAll('.wb-todo-card').length,
        repoCards: column.querySelectorAll('.wb-todo-repo-card').length,
      }));
    })()`);
    check(
      "计数 = 渲染数（流水线 0 / 今日 3 / 长期 1）",
      columnInfo[0].count === 0 && columnInfo[0].cards === 0 &&
        columnInfo[1].count === 3 && columnInfo[1].repoCards === 3 &&
        columnInfo[2].count === 1 && columnInfo[2].repoCards === 1,
      JSON.stringify(columnInfo),
    );

    const segments = await evaluate(
      "[...document.querySelectorAll('.wb-todo-seghead > span:first-child, .wb-todo-seghead > button:first-child')].map((node) => node.textContent)",
    );
    check(
      "流水线分段 队列→运行→待收",
      JSON.stringify(segments) === JSON.stringify(["队列", "运行", "待收"]),
      JSON.stringify(segments),
    );

    // ---- 2. repository card has only title + acceptance --------------
    const repoCard = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      if (!card) return null;
      const accept = card.querySelector('.wb-todo-accept');
      return {
        children: [...card.children].map((node) => node.className),
        decorated: Boolean(card.querySelector('.wb-dot, .wb-tag, .wb-prio, .wb-block, .wb-sum')),
        borderTop: getComputedStyle(card).borderTopWidth,
        acceptEmpty: accept.classList.contains('empty'),
        placeholder: accept.textContent,
        draggableAttr: card.getAttribute('draggable'),
      };
    })()`);
    check(
      "仓库卡只有标题 + 验收一行",
      repoCard && repoCard.children.length === 2 &&
        repoCard.children[0].startsWith("wb-todo-title") &&
        repoCard.children[1].includes("wb-todo-accept"),
      JSON.stringify(repoCard),
    );
    check("仓库卡无状态点/进度/assignee 装饰", repoCard && repoCard.decorated === false);
    check("仓库卡无边框高亮", repoCard && repoCard.borderTop === "0px", repoCard?.borderTop);
    check(
      "空验收以警示色标注",
      repoCard && repoCard.acceptEmpty === true && repoCard.placeholder === "补一句怎样算做完",
      JSON.stringify(repoCard),
    );
    check("仓库卡真实可拖拽", repoCard && repoCard.draggableAttr === "true", String(repoCard?.draggableAttr));

    const hoverRule = await evaluate(`(() => {
      const rules = [];
      for (const sheet of document.styleSheets) {
        let list = [];
        try { list = [...sheet.cssRules]; } catch { continue; }
        for (const rule of list) {
          if (rule.cssText.includes('.wb-todo-repo-card:hover')) rules.push(rule.cssText);
        }
      }
      return rules.join("\\n");
    })()`);
    check(
      "hover 只换背景",
      hoverRule.includes("background") &&
        !hoverRule.includes("border") &&
        !hoverRule.includes("box-shadow"),
      hoverRule,
    );

    // 仓库互拖：长期 → 今日 → 长期，只改 bucket，不派发。
    check(
      "仓库互拖：长期 → 今日",
      (await dragRepoCardToStore("长期卡", "今日")) === true,
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')][0].textContent.includes('长期卡')`,
      8000,
      "长期卡移到今日",
    );
    const todayMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(longCard)}`,
    )).data?.[longCard];
    check("仓库互拖只改 bucket（长期 → 今日）", todayMeta?.bucket === "today", JSON.stringify(todayMeta));
    check(
      "仓库互拖不派发",
      (await evaluate("window.__wbProbe.dispatch || 0")) === 0,
      String(await evaluate("window.__wbProbe.dispatch || 0")),
    );
    check(
      "仓库互拖：今日 → 长期",
      (await dragRepoCardToStore("长期卡", "长期")) === true,
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')][1].textContent.includes('长期卡')`,
      8000,
      "长期卡移回长期",
    );
    await waitFor(
      async () =>
        (await httpJson(
          "GET",
          `${base}/api/workbench/metadata?ids=${encodeURIComponent(longCard)}`,
        )).data?.[longCard]?.bucket === "long",
      { timeout: 3000, interval: 100, label: "仓库互拖恢复 bucket（今日 → 长期）" },
    );
    const longMetaAgain = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(longCard)}`,
    )).data?.[longCard];
    check(
      "仓库互拖恢复 bucket（今日 → 长期）",
      longMetaAgain?.bucket === "long",
      JSON.stringify(longMetaAgain),
    );

    const dragEndClear = await evaluate(`(async () => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      const flow = document.querySelector('.wb-todo-flow');
      const dataTransfer = new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
      flow.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const highlighted = flow.classList.contains('drop');
      card.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      return { highlighted, cleared: !flow.classList.contains('drop') };
    })()`);
    check(
      "源卡 dragend 清除整列高亮",
      dragEndClear.highlighted === true && dragEndClear.cleared === true,
      JSON.stringify(dragEndClear),
    );

    // ---- 3. empty acceptance drop: shake + hint + zero request -------
    const emptyDrop = await evaluate(`(async () => {
      window.__wbProbe.dispatch = 0;
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      const flow = document.querySelector('.wb-todo-flow');
      const dataTransfer = new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
      flow.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const highlighted = flow.classList.contains('drop');
      flow.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 40));
      const dropCleared = !flow.classList.contains('drop');
      await new Promise((resolveWait) => setTimeout(resolveWait, 80));
      const during = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      const shakingDuring = during.classList.contains('wb-todo-shake');
      await new Promise((resolveWait) => setTimeout(resolveWait, 400));
      const after = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      return {
        highlighted,
        dropCleared,
        shakingDuring,
        shakingAfter: after.classList.contains('wb-todo-shake'),
        warn: after.querySelector('.wb-todo-warn')?.textContent || '',
        dispatch: window.__wbProbe.dispatch,
        stillToday: Boolean(after.closest('.wb-todo-plane')),
      };
    })()`);
    check("拖入流水线时整列高亮", emptyDrop.highlighted === true, JSON.stringify(emptyDrop));
    check("drop 后清除整列高亮", emptyDrop.dropCleared === true);
    check("空验收回原位", emptyDrop.stillToday === true);
    check("空验收抖动完整 300ms 窗口", emptyDrop.shakingDuring === true && emptyDrop.shakingAfter === false, JSON.stringify(emptyDrop));
    check(
      "空验收红字提示",
      emptyDrop.warn === "先补一句怎样算做完",
      emptyDrop.warn,
    );
    check("空验收零请求", emptyDrop.dispatch === 0, String(emptyDrop.dispatch));
    check("空验收被拦不出回执", (await readReceipt())?.text === "");

    // ---- 4. inline acceptance edit -----------------------------------
    const editStart = await evaluate(`(async () => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      card.querySelector('.wb-todo-accept').click();
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const input = card.querySelector('.wb-todo-accept-input');
      const draggableWhileEditing = card.getAttribute('draggable');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '跑通测试');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 30));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      return { draggableWhileEditing };
    })()`);
    check(
      "编辑验收时禁止拖拽",
      editStart.draggableWhileEditing === "false",
      String(editStart.draggableWhileEditing),
    );
    await waitForEval("window.__wbProbe.metadata >= 1", 5000, "验收 PATCH");
    await waitForEval(
      `(() => {
        const card = [...document.querySelectorAll('.wb-todo-repo-card')]
          .find((node) => node.textContent.includes('空验收卡'));
        return Boolean(card) && !card.querySelector('.wb-todo-accept').classList.contains('empty');
      })()`,
      5000,
      "警示色消失",
    );
    const edited = await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(emptyCard)}`,
    );
    check(
      "验收写入 workbench.db 且去掉首尾空白",
      edited.data?.[emptyCard]?.contract?.accept === "跑通测试",
      JSON.stringify(edited.data),
    );

    // 回执存活期拒绝测试用卡：一张空验收、一张有验收（等 5s 轮询渲染）。
    const rejectEmptyCard = await createUpstream("待补验收卡");
    await setMetadata(rejectEmptyCard, {
      bucket: "today",
      contract: { input: "", accept: "", ready: false },
    });
    const rejectValidCard = await createUpstream("回执测试卡");
    await setMetadata(rejectValidCard, { bucket: "today", contract: { accept: "回执测试" } });
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('待补验收卡')) &&
       [...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('回执测试卡'))`,
      8000,
      "拒绝/回执测试卡渲染",
    );

    // ---- 5. dispatch: pending → final receipt, serialized ------------
    const liveRegion = await readReceipt();
    check(
      "回执 live region 常驻（role=status）",
      liveRegion?.role === "status" &&
        liveRegion.text === "" &&
        liveRegion.visible === false &&
        (await evaluate("Boolean(document.querySelector('.wb-todo-receipt-slot'))")) === true,
      JSON.stringify(liveRegion),
    );
    const gridTopBefore = await evaluate(
      "document.querySelector('.wb-todo-grid').getBoundingClientRect().top",
    );
    await evaluate("void (window.__wbReceiptNode = document.querySelector('.wb-todo-receipt'))");
    await evaluate(
      "window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null; window.__wbProbe.holdMs = 500;",
    );
    check("拖拽有验收卡触发派发", (await dragCardToFlow("空验收卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "drop 后 pending 回执",
    );
    check("drop 后立即显示 pending 回执", true);
    const gridTopPending = await evaluate(
      "document.querySelector('.wb-todo-grid').getBoundingClientRect().top",
    );
    check(
      "回执出现不改变三列布局",
      gridTopBefore === gridTopPending,
      `${gridTopBefore} -> ${gridTopPending}`,
    );

    // 全局串行：上一张在途时第二次 drop 被忽略，只发一次请求。
    check("并发 drop 第二张卡", (await dragCardToFlow("有验收卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-toast')?.textContent || '') === '正在派发上一张'",
      1500,
      "并发派发 toast",
    );
    check("并发派发只发一次请求", (await evaluate("window.__wbProbe.dispatch")) === 1);

    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "dispatch 响应",
    );
    const firstDispatch = await evaluate("window.__wbProbe.lastDispatch");
    const expectedFirstReceipt =
      `已入队 · 第 ${firstDispatch.position} 位 · 预计 ${firstDispatch.eta}`;
    await waitFor(
      async () => (await readReceiptText()) === expectedFirstReceipt,
      { timeout: 5000, interval: 50, label: "最终回执" },
    );
    const firstReceipt = await readReceipt();
    check(
      "最终回执文案 = dispatch 返回值（位次 + ETA）",
      firstReceipt?.text === expectedFirstReceipt &&
        firstReceipt.running === false &&
        firstReceipt.pending === false,
      JSON.stringify({ expected: expectedFirstReceipt, actual: firstReceipt }),
    );
    check(
      "回执 live region 节点未重建",
      (await evaluate(
        "document.querySelector('.wb-todo-receipt') === window.__wbReceiptNode",
      )) === true,
    );
    await evaluate("window.__wbProbe.holdMs = 0;");
    await waitForEval(
      `![...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('空验收卡'))`,
      8000,
      "派发后离开仓库",
    );
    const dispatched = await evaluate(`(() => {
      const flow = document.querySelector('.wb-todo-flow');
      return {
        inFlow: flow.textContent.includes('空验收卡'),
        toast: document.querySelector('.wb-toast')?.textContent || '',
      };
    })()`);
    check("派发成功卡进入流水线", dispatched.inFlow === true, JSON.stringify(dispatched));
    check("派发成功不再显示旧「已派发」toast", dispatched.toast !== "已派发", dispatched.toast);
    await expectBadgesMatch("ready 卡片按 /board 顺序显示 1-based 位次");
    const firstOwnBadge = (await readReadyBadges()).find((card) => card.title === "空验收卡");
    check(
      "派发卡自身徽标 = dispatch position",
      firstOwnBadge?.badge === `第 ${firstDispatch.position} 位`,
      JSON.stringify({ position: firstDispatch.position, badge: firstOwnBadge?.badge }),
    );
    const emptyStatus = (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${emptyCard}`)).data?.task?.status;
    check("派发后上游状态 ready", emptyStatus === "ready", String(emptyStatus));

    // 流水线卡（ready/running）点开详情：标题、状态、验收、位次。
    const readyDetailOpened = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-flow-card.clickable')]
        .find((node) => node.textContent.includes('空验收卡'));
      if (!card) return false;
      card.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "流水线卡详情");
    const readyDetail = await evaluate(`(() => ({
      title: document.querySelector('.wb-detail-title')?.textContent || '',
      badge: document.querySelector('.wb-detail-badge')?.textContent || '',
      text: document.querySelector('.wb-detail-drawer')?.textContent || '',
      position: document.querySelector('.wb-detail-position')?.textContent || '',
    }))()`);
    check(
      "流水线卡点开详情（标题/状态/验收/位次）",
      readyDetailOpened === true &&
        readyDetail.title.includes("空验收卡") &&
        readyDetail.badge.includes("就绪") &&
        readyDetail.text.includes("跑通测试") &&
        readyDetail.position.includes("队列第"),
      JSON.stringify(readyDetail),
    );
    await evaluate("document.querySelector('.wb-detail-close').click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "关闭详情");
    check("详情可关闭", true);

    // ---- 5b. second dispatch: latest-attempt replaces the receipt ----
    await evaluate("window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null;");
    check("拖拽第二张有验收卡", (await dragCardToFlow("有验收卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "第二次 pending 回执",
    );
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "第二次 dispatch 响应",
    );
    const secondDispatch = await evaluate("window.__wbProbe.lastDispatch");
    const expectedSecondReceipt =
      `已入队 · 第 ${secondDispatch.position} 位 · 预计 ${secondDispatch.eta}`;
    await waitFor(
      async () => (await readReceiptText()) === expectedSecondReceipt,
      { timeout: 5000, interval: 50, label: "第二次最终回执" },
    );
    const secondReceipt = await readReceipt();
    check(
      "第二次回执位次/ETA 与 dispatch 返回值一致",
      secondReceipt?.text === expectedSecondReceipt,
      JSON.stringify({ dispatch: secondDispatch, receipt: secondReceipt }),
    );
    check(
      "新尝试回执替换旧回执（latest-attempt）",
      secondReceipt?.text !== expectedFirstReceipt,
      `${expectedFirstReceipt} -> ${secondReceipt?.text}`,
    );
    check(
      "第二张卡位次排在已入队卡之后",
      secondDispatch.position > firstDispatch.position,
      `${firstDispatch.position} -> ${secondDispatch.position}`,
    );
    await waitForEval(
      `![...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('有验收卡'))`,
      8000,
      "第二张卡离开仓库",
    );

    // ---- 5c. rejection while a receipt is live -----------------------
    check("空验收 drop 前回执仍显示", (await readReceiptText()) === expectedSecondReceipt);
    const dispatchBeforeEmptyReject = await evaluate("window.__wbProbe.dispatch");
    check("拖拽待补验收卡", (await dragCardToFlow("待补验收卡")) === true);
    const emptyRejectNow = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('待补验收卡'));
      return {
        receipt: document.querySelector('.wb-todo-receipt-text')?.textContent || '',
        shaking: card?.classList.contains('wb-todo-shake') || false,
        warn: card?.querySelector('.wb-todo-warn')?.textContent || '',
        dispatch: window.__wbProbe.dispatch,
      };
    })()`);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === ''",
      1500,
      "空验收立即清回执",
    );
    check(
      "空验收：回执立即清空",
      emptyRejectNow.receipt === "",
      JSON.stringify(emptyRejectNow),
    );
    check(
      "空验收：抖动/红字保留",
      emptyRejectNow.shaking === true && emptyRejectNow.warn === "先补一句怎样算做完",
      JSON.stringify(emptyRejectNow),
    );
    check(
      "空验收：拒绝不发请求",
      emptyRejectNow.dispatch === dispatchBeforeEmptyReject,
      `${dispatchBeforeEmptyReject} -> ${emptyRejectNow.dispatch}`,
    );

    // 新回执 → 立刻拖 409 parent：pending 出现在 drop，拒绝后清掉。
    await evaluate(
      "window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null; window.__wbProbe.holdMs = 400;",
    );
    check("拖拽回执测试卡", (await dragCardToFlow("回执测试卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "回执测试卡 pending",
    );
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "回执测试卡 dispatch 响应",
    );
    const testDispatch = await evaluate("window.__wbProbe.lastDispatch");
    const expectedTestReceipt =
      `已入队 · 第 ${testDispatch.position} 位 · 预计 ${testDispatch.eta}`;
    await waitFor(
      async () => (await readReceiptText()) === expectedTestReceipt,
      { timeout: 5000, interval: 50, label: "回执测试卡最终回执" },
    );
    check("409 前回执仍显示", (await readReceiptText()) === expectedTestReceipt);

    await evaluate("window.__wbProbe.dispatch = 0; window.__wbProbe.holdMs = 400;");
    check("拖拽等 parent 卡", (await dragCardToFlow("等 parent 卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "409 pending 回执",
    );
    check("409 drop 后先显示 pending 回执", true);
    await waitForEval(
      `(document.querySelector('.wb-toast')?.textContent || '') === '已回到仓库，等 parent 完成'`,
      8000,
      "409 toast",
    );
    await evaluate("window.__wbProbe.holdMs = 0;");
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('等 parent 卡'))`,
      8000,
      "409 卡回到仓库",
    );
    check("409 卡回到仓库（scheduled 入库，等 parent）", true);
    const blockedState = await evaluate(`(() => ({
      receipt: document.querySelector('.wb-todo-receipt-text')?.textContent || '',
      toast: document.querySelector('.wb-toast')?.textContent || '',
    }))()`);
    check(
      "409 拒绝后清掉 pending，不出成功回执",
      blockedState.receipt === "",
      JSON.stringify(blockedState),
    );
    check(
      "409 既有 toast 保留",
      blockedState.toast === "已回到仓库，等 parent 完成",
      blockedState.toast,
    );
    const blockedStatus = (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${blockedChild}`)).data?.task?.status;
    check("有未完成 parent 的卡落 scheduled 仓库", blockedStatus === "scheduled", String(blockedStatus));

    await expectBadgesMatch("三张 ready 卡的位次与 /board 顺序一致");
    const readyAfterRejects = await readReadyBadges();
    const secondOwnBadge = readyAfterRejects.find((card) => card.title === "有验收卡");
    check(
      "第二张派发卡自身徽标 = dispatch position",
      secondOwnBadge?.badge === `第 ${secondDispatch.position} 位`,
      JSON.stringify({ position: secondDispatch.position, badge: secondOwnBadge?.badge }),
    );

    // ---- 5d. external queue change: next poll recomputes positions ---
    const jumpCard = await createUpstream("队列插队卡");
    await setMetadata(jumpCard, { bucket: "today", contract: { accept: "插队" } });
    setKanbanPriority(jumpCard, 9);
    const jumpDispatch = await httpJson(
      "POST",
      `${base}/api/workbench/dispatch/${encodeURIComponent(jumpCard)}`,
      {},
    );
    check(
      "外部派发的高优先级卡排到第 1 位",
      jumpDispatch.status === 200 && jumpDispatch.data?.position === 1,
      JSON.stringify(jumpDispatch.data),
    );
    await expectBadgesMatch("外部改队列后 5s 轮询重算位次");
    const jumpBadge = (await readReadyBadges()).find((card) => card.title === "队列插队卡");
    check("外部插队卡自身徽标 = 第 1 位", jumpBadge?.badge === "第 1 位", JSON.stringify(jumpBadge));

    // ---- 5e. refresh: positions re-derived from /board, no ETA -------
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", { source: POLL_INTERVAL_SOURCE });
    await cdp.send("Page.reload");
    await waitForEval(
      `document.querySelectorAll('.wb-todo-col').length === 3 &&
        document.querySelectorAll('.wb-todo-flow-card').length >= 3`,
      15000,
      "刷新后待办渲染",
    );
    await expectBadgesMatch("刷新后位次按 /board 重新派生");
    const reloadState = await evaluate(`(() => ({
      receiptText: document.querySelector('.wb-todo-receipt-text')?.textContent || '',
      slot: Boolean(document.querySelector('.wb-todo-receipt-slot')),
      eta: [...document.querySelectorAll('.wb-todo-receipt, .wb-todo-badge')]
        .some((node) => node.textContent.includes('预计')),
    }))()`);
    check(
      "刷新后无回执、无 ETA，回执槽位仍在",
      reloadState.receiptText === "" && reloadState.eta === false && reloadState.slot === true,
      JSON.stringify(reloadState),
    );
    await installProbe();

    // ---- 5e2. shell version banner ----------------------------------
    const versionEndpoint = await httpJson("GET", `${base}/api/workbench/version`);
    check(
      "外壳版本端点可用",
      versionEndpoint.status === 200 &&
        versionEndpoint.data?.ok === true &&
        typeof versionEndpoint.data?.version === "string" &&
        versionEndpoint.data.version.length > 0,
      JSON.stringify(versionEndpoint.data),
    );
    await waitForEval(
      "typeof window.__WB_SHELL_BASELINE__ === 'string' && window.__WB_SHELL_BASELINE__.length > 0",
      5000,
      "客户端记录版本基线",
    );
    check(
      "客户端基线与服务端版本一致",
      (await evaluate("window.__WB_SHELL_BASELINE__")) === versionEndpoint.data.version,
    );
    await evaluate(
      "window.__wbProbe.fakeVersion = 'probe-shell-next'; window.dispatchEvent(new Event('focus')); true",
    );
    await waitForEval("Boolean(document.querySelector('.wb-update-banner'))", 5000, "新版本提示");
    const updateBanner = await evaluate(`(() => ({
      text: document.querySelector('.wb-update-text')?.textContent || '',
      action: document.querySelector('.wb-update-action')?.textContent || '',
      dismiss: Boolean(document.querySelector('.wb-update-dismiss')),
      fixed: getComputedStyle(document.querySelector('.wb-update-banner')).position,
      zIndex: Number(getComputedStyle(document.querySelector('.wb-update-banner')).zIndex || 0),
    }))()`);
    check(
      "新版本提示出现且有刷新/稍后按钮",
      updateBanner.text.includes("已更新") &&
        updateBanner.action === "刷新" &&
        updateBanner.dismiss === true &&
        updateBanner.fixed === "fixed" &&
        updateBanner.zIndex < 90,
      JSON.stringify(updateBanner),
    );
    const bannerContrast = await contrastMatrix({
      text: ".wb-update-text",
      action: ".wb-update-action",
    });
    check(
      "版本提示明暗主题对比度 >= 4.5",
      ["light", "dark"].every((mode) =>
        Object.values(bannerContrast?.[mode] || {}).every(
          (value) => typeof value === "number" && value >= 4.5,
        ),
      ),
      JSON.stringify(bannerContrast),
    );
    await evaluate("document.querySelector('.wb-update-dismiss').click(); true");
    await waitForEval("!document.querySelector('.wb-update-banner')", 3000, "稍后提醒");
    check("新版本提示可稍后", true);
    await evaluate("window.__wbProbe.fakeVersion = null; true");

    // ---- 5f. running: 已在运行, no position --------------------------
    const runningCard = await createUpstream("运行竞态卡");
    await setMetadata(runningCard, { bucket: "today", contract: { accept: "运行中" } });
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('运行竞态卡'))`,
      8000,
      "运行竞态卡渲染",
    );
    // 客户端仍按 triage 显示时把后端置为 running，模拟 worker 刚领走后的重派。
    await evaluate("window.__wbPausePoll()");
    // Let any board read already in flight settle before mutating status.
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
    setKanbanStatus(runningCard, "running");
    await evaluate(
      "window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null; window.__wbProbe.holdMs = 400;",
    );
    check("拖拽已 running 的卡", (await dragCardToFlow("运行竞态卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "running pending 回执",
    );
    await evaluate("window.__wbResumePoll()");
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "running dispatch 响应",
    );
    const runningDispatch = await evaluate("window.__wbProbe.lastDispatch");
    check(
      "running 重派返回 position=0 / eta=null",
      runningDispatch.position === 0 &&
        runningDispatch.eta === null &&
        runningDispatch.status === "running",
      JSON.stringify(runningDispatch),
    );
    await waitFor(
      async () => (await readReceiptText()) === "已在运行",
      { timeout: 5000, interval: 50, label: "running 回执" },
    );
    const runningReceipt = await readReceipt();
    check(
      "running 回执「已在运行」且不显示位次",
      runningReceipt?.text === "已在运行" &&
        runningReceipt.running === true &&
        !runningReceipt.text.includes("第"),
      JSON.stringify(runningReceipt),
    );
    await waitForEval(
      `(() => {
        const seg = [...document.querySelectorAll('.wb-todo-seg')].find(
          (node) => node.querySelector('.wb-todo-seghead > span:first-child, .wb-todo-seghead > button:first-child')?.textContent === '运行',
        );
        return Boolean(seg) && [...seg.querySelectorAll('.wb-todo-flow-card')].some(
          (card) => card.textContent.includes('运行竞态卡') &&
            card.querySelector('.wb-todo-badge')?.textContent === '已在运行',
        );
      })()`,
      8000,
      "running 卡「已在运行」徽标",
    );
    check("running 卡显示「已在运行」", true);
    await evaluate("window.__wbProbe.holdMs = 0;");

    // ---- 5g. dismiss + 6s auto-hide ---------------------------------
    check(
      "回执有关闭按钮（aria-label）",
      runningReceipt?.dismissLabel === "关闭派发回执",
      JSON.stringify(runningReceipt),
    );
    await evaluate("document.querySelector('.wb-todo-receipt-dismiss').click()");
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === ''",
      1000,
      "关闭回执",
    );
    const dismissed = await readReceipt();
    check(
      "关闭按钮立即隐藏回执",
      dismissed?.text === "" && dismissed.visible === false,
      JSON.stringify(dismissed),
    );

    // 新一次成功派发 → 等待超过 RECEIPT_MS 自动消失。
    await evaluate("window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null;");
    check("拖拽长期卡获取新回执", (await dragCardToFlow("长期卡")) === true);
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "回执寿命 dispatch 响应",
    );
    await waitFor(
      async () => (await readReceiptText()).startsWith("已入队 · 第 "),
      { timeout: 5000, interval: 50, label: "回执寿命最终回执" },
    );
    check("自动消失前回执可见", (await readReceipt())?.visible === true);
    await new Promise((resolveWait) => setTimeout(resolveWait, RECEIPT_MS - 1500));
    check(
      "RECEIPT_MS 内回执仍可见",
      (await readReceipt())?.visible === true,
      JSON.stringify(await readReceipt()),
    );
    await waitFor(
      async () => (await readReceiptText()) === "",
      { timeout: 4000, interval: 200, label: "回执自动消失" },
    );
    check("超过 RECEIPT_MS 后回执自动消失", true);

    // ---- 6. create info card lands in today, not dispatched -----------
    check("今日分区右上角有新建按钮", (await openCreateCard("今日")) === true);
    const emptyTitle = await evaluate(`(async () => {
      window.__wbProbe.create = 0;
      document.querySelector('.wb-todo-create-title').focus();
      document.querySelector('.wb-todo-create-title').dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
      );
      await new Promise((resolveWait) => setTimeout(resolveWait, 150));
      return {
        create: window.__wbProbe.create,
        error: document.querySelector('.wb-todo-create-error')?.textContent || '',
      };
    })()`);
    check("空标题不建卡", emptyTitle.create === 0, JSON.stringify(emptyTitle));
    check("空标题就地提示", emptyTitle.error.includes("先写一句"), emptyTitle.error);

    await evaluate("window.__wbProbe.create = 0; window.__wbProbe.dispatch = 0;");
    check("创建卡可填标题", (await setCreateField(".wb-todo-create-title", "探针新建卡")) === true);
    check("创建卡可填上下文", (await setCreateField(".wb-todo-create-input", "来自今日新建卡")) === true);
    check("创建卡可填验收", (await setCreateField(".wb-todo-create-accept", "跑通测试")) === true);
    check("创建卡提交", (await submitCreateCard()) === true);
    await waitForEval("window.__wbProbe.create === 1", 5000, "建卡请求");
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('探针新建卡'))`,
      8000,
      "新卡渲染",
    );
    const createdInfo = await evaluate(`(() => {
      const planes = [...document.querySelectorAll('.wb-todo-plane')];
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('探针新建卡'));
      return {
        inToday: planes[0].contains(card),
        inLong: planes[1].contains(card),
        accept: card.querySelector('.wb-todo-accept')?.textContent || '',
        open: Boolean(document.querySelector('.wb-todo-create-card')),
        dispatch: window.__wbProbe.dispatch,
      };
    })()`);
    check("新建卡落今日", createdInfo.inToday === true && createdInfo.inLong === false);
    check("新建卡保留验收信息", createdInfo.accept.includes("跑通测试"), createdInfo.accept);
    check("建卡后收起创建卡", createdInfo.open === false);
    check("新建卡不自动派发", createdInfo.dispatch === 0, String(createdInfo.dispatch));

    const boardAfterCreate = (await httpJson("GET", `${base}/api/plugins/kanban/board?include_archived=true`)).data;
    const createdCard = boardAfterCreate.columns
      .flatMap((column) => column.tasks)
      .find((task) => task.title === "探针新建卡");
    check("新建卡上游状态 scheduled（仓库，不是 triage/todo）", createdCard?.status === "scheduled", String(createdCard?.status));
    const createdMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
    )).data?.[createdCard.id];
    check("新建卡 metadata bucket=today", createdMeta?.bucket === "today", JSON.stringify(createdMeta));
    check(
      "创建卡写入验收/上下文",
      createdMeta?.contract?.accept === "跑通测试" && createdMeta?.contract?.input === "来自今日新建卡",
      JSON.stringify(createdMeta?.contract),
    );

    // ---- 7. 5s polling must not steal the acceptance input focus -----
    await evaluate(`(async () => {
      window.__wbProbe.metadata = 0;
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('探针新建卡'));
      card.querySelector('.wb-todo-accept').click();
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const input = card.querySelector('.wb-todo-accept-input');
      input.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '临时输入');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    await new Promise((resolveWait) => setTimeout(resolveWait, 5600));
    const focusState = await evaluate(`(() => {
      const active = document.activeElement;
      return {
        focused: Boolean(active && active.classList.contains('wb-todo-accept-input')),
        value: active && active.value ? active.value : '',
      };
    })()`);
    check(
      "5s 轮询不丢输入焦点",
      focusState.focused === true && focusState.value === "临时输入",
      JSON.stringify(focusState),
    );
    const cancelState = await evaluate(`(async () => {
      const active = document.activeElement;
      active.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 120));
      return {
        inputs: document.querySelectorAll('.wb-todo-accept-input').length,
        metadata: window.__wbProbe.metadata,
      };
    })()`);
    check(
      "Esc 取消验收编辑且不发保存请求",
      cancelState.inputs === 0 && cancelState.metadata === 0,
      JSON.stringify(cancelState),
    );

    // ---- 7b. repository card detail: edit title/accept/context ------
    const openedStoreDetail = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('探针新建卡'));
      const title = card?.querySelector('.wb-todo-title-link');
      if (!title) return false;
      title.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "仓库卡详情");
    const storeDetail = await evaluate(`(() => ({
      title: document.querySelector('.wb-detail-title')?.textContent || '',
      badge: document.querySelector('.wb-detail-badge')?.textContent || '',
      text: document.querySelector('.wb-detail-drawer')?.textContent || '',
      edit: Boolean(
        [...document.querySelectorAll('.wb-detail-btn')].find(
          (node) => node.textContent.trim() === '编辑',
        ),
      ),
    }))()`);
    check("仓库卡点标题打开详情", openedStoreDetail === true);
    check(
      "仓库卡详情显示已排期/验收/编辑入口",
      storeDetail.title.includes("探针新建卡") &&
        storeDetail.badge.includes("已排期") &&
        storeDetail.text.includes("跑通测试") &&
        storeDetail.edit === true,
      JSON.stringify(storeDetail),
    );

    await evaluate(
      `[...document.querySelectorAll('.wb-detail-btn')].find((node) => node.textContent.trim() === '编辑')?.click(); true`,
    );
    await waitForEval("Boolean(document.querySelector('.wb-detail-input'))", 3000, "仓库卡进入编辑");
    const editPrefilled = await evaluate(`(() => ({
      title: document.querySelector('.wb-detail-input')?.value || '',
      values: [...document.querySelectorAll('.wb-detail-textarea')].map((node) => node.value),
    }))()`);
    check(
      "编辑表单带出原值",
      editPrefilled.title === "探针新建卡" &&
        editPrefilled.values.includes("跑通测试") &&
        editPrefilled.values.includes("来自今日新建卡"),
      JSON.stringify(editPrefilled),
    );

    await evaluate(`(() => {
      const setValue = (node, value) => {
        const proto = node.tagName === 'TEXTAREA'
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
        node.dispatchEvent(new Event('input', { bubbles: true }));
      };
      setValue(document.querySelector('.wb-detail-input'), '探针改名卡');
      const areas = [...document.querySelectorAll('.wb-detail-textarea')];
      setValue(areas[0], '改名后验收');
      setValue(areas[1], '详情编辑上下文');
      return true;
    })()`);
    await evaluate(
      `[...document.querySelectorAll('.wb-detail-btn')].find((node) => node.textContent.trim() === '保存')?.click(); true`,
    );
    await waitForEval("!document.querySelector('.wb-detail-input')", 8000, "保存后退出编辑");
    await waitForEval(
      `document.querySelector('.wb-detail-title')?.textContent.includes('探针改名卡')`,
      8000,
      "详情标题刷新",
    );
    const savedDetail = await evaluate(`(() => ({
      title: document.querySelector('.wb-detail-title')?.textContent || '',
      text: document.querySelector('.wb-detail-drawer')?.textContent || '',
    }))()`);
    check(
      "详情保存后显示新标题/验收",
      savedDetail.title.includes("探针改名卡") && savedDetail.text.includes("改名后验收"),
      JSON.stringify(savedDetail),
    );
    check(
      "详情保存后仓库卡改名",
      (await evaluate(
        `[...document.querySelectorAll('.wb-todo-repo-card')].some((node) => node.textContent.includes('探针改名卡'))`,
      )) === true,
    );
    const renamedBoard = (await httpJson("GET", `${base}/api/plugins/kanban/board?include_archived=true`)).data;
    const renamedTask = renamedBoard.columns
      .flatMap((column) => column.tasks)
      .find((task) => task.id === createdCard.id);
    const renamedMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
    )).data?.[createdCard.id];
    check("详情编辑标题写回上游", renamedTask?.title === "探针改名卡", String(renamedTask?.title));
    check(
      "详情编辑写回验收/上下文",
      renamedMeta?.contract?.accept === "改名后验收" &&
        renamedMeta?.contract?.input === "详情编辑上下文",
      JSON.stringify(renamedMeta?.contract),
    );
    await evaluate("document.querySelector('.wb-detail-close').click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "关闭仓库卡详情");
    check("仓库卡详情可关闭", true);

    // ---- 7c. scheduled 卡也能互拖（不是只有旧 triage 卡） -------------
    await evaluate("window.__wbProbe.dispatch = 0; true");
    check(
      "scheduled 卡互拖：今日 → 长期",
      (await dragRepoCardToStore("探针改名卡", "长期")) === true,
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')][1].textContent.includes('探针改名卡')`,
      8000,
      "scheduled 卡移到长期",
    );
    await waitFor(
      async () =>
        (await httpJson(
          "GET",
          `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
        )).data?.[createdCard.id]?.bucket === "long",
      { timeout: 3000, interval: 100, label: "scheduled 卡 bucket=long" },
    );
    const scheduledLongMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
    )).data?.[createdCard.id];
    check(
      "scheduled 卡互拖只改 bucket（今日 → 长期）",
      scheduledLongMeta?.bucket === "long" && scheduledLongMeta?.contract?.accept === "改名后验收",
      JSON.stringify(scheduledLongMeta),
    );
    check(
      "scheduled 卡互拖不派发",
      (await evaluate("window.__wbProbe.dispatch || 0")) === 0,
      String(await evaluate("window.__wbProbe.dispatch || 0")),
    );
    check(
      "scheduled 卡互拖：长期 → 今日",
      (await dragRepoCardToStore("探针改名卡", "今日")) === true,
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')][0].textContent.includes('探针改名卡')`,
      8000,
      "scheduled 卡移回今日",
    );
    await waitFor(
      async () =>
        (await httpJson(
          "GET",
          `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
        )).data?.[createdCard.id]?.bucket === "today",
      { timeout: 3000, interval: 100, label: "scheduled 卡 bucket=today" },
    );
    check(
      "scheduled 卡互拖恢复 today",
      (await httpJson(
        "GET",
        `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
      )).data?.[createdCard.id]?.bucket === "today",
    );

    // ---- 7d. 定时唤醒：chip / 设置 / 清除 / 提前唤醒 / 错误态 --------
    const wakeCard = await createUpstream("定时唤醒卡");
    setKanbanStatus(wakeCard, "scheduled");
    await setMetadata(wakeCard, {
      bucket: "today",
      contract: { accept: "到点自动跑" },
      wake_at: Math.floor(Date.now() / 1000) + 3600,
    });
    const wakeErrorCard = await createUpstream("缺验收唤醒卡");
    setKanbanStatus(wakeErrorCard, "scheduled");
    await setMetadata(wakeErrorCard, {
      bucket: "today",
      contract: { input: "", accept: "", ready: false },
      wake_at: Math.floor(Date.now() / 1000) + 3600,
    });
    const wakeMetadata = async (taskId) =>
      (await httpJson(
        "GET",
        `${base}/api/workbench/metadata?ids=${encodeURIComponent(taskId)}`,
      )).data?.[taskId];

    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('定时唤醒卡'))`,
      12000,
      "唤醒卡经轮询出现",
    );
    const wakeChip = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('定时唤醒卡'));
      const chip = card?.querySelector('.wb-todo-wake');
      return { text: chip?.textContent || '', hasChip: Boolean(chip) };
    })()`);
    check(
      "仓库卡显示唤醒时间 chip",
      wakeChip.hasChip === true &&
        /^将于 (\d{2}-\d{2} )?\d{2}:\d{2} 唤醒$/.test(wakeChip.text),
      JSON.stringify(wakeChip),
    );
    const noWakeChip = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('探针改名卡'));
      return Boolean(card) && !card.querySelector('.wb-todo-wake');
    })()`);
    check("未设置 wake_at 的仓库卡无唤醒 chip", noWakeChip === true);

    await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('定时唤醒卡'));
      card?.querySelector('.wb-todo-title-link')?.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-detail-wake'))", 5000, "唤醒详情区");
    const wakePanel = await evaluate(`(() => ({
      input: document.querySelector('.wb-detail-wake-input')?.value || '',
      buttons: [...document.querySelectorAll('.wb-detail-wake button')]
        .map((node) => node.textContent.trim()),
      hint: document.querySelector('.wb-detail-wake-hint')?.textContent || '',
    }))()`);
    check(
      "详情显示唤醒时间/快捷设置/清除/提前唤醒",
      wakePanel.input.length > 0 &&
        ["5 分钟后", "设置", "清除", "提前唤醒"].every((label) =>
          wakePanel.buttons.includes(label),
        ),
      JSON.stringify(wakePanel),
    );

    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '5 分钟后')?.click(); true`,
    );
    const wakeSetFrom = Math.floor(Date.now() / 1000);
    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '设置')?.click(); true`,
    );
    await waitFor(
      async () => {
        const meta = await wakeMetadata(wakeCard);
        return (
          typeof meta?.wake_at === "number" &&
          meta.wake_at >= wakeSetFrom + 300 &&
          meta.wake_at <= wakeSetFrom + 420
        );
      },
      { timeout: 5000, label: "设置唤醒时间" },
    );
    const wakeSetMeta = await wakeMetadata(wakeCard);
    check(
      "详情「5 分钟后 + 设置」写入 wake_at",
      typeof wakeSetMeta?.wake_at === "number",
      JSON.stringify(wakeSetMeta),
    );

    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '清除')?.click(); true`,
    );
    await waitFor(
      async () => {
        const meta = await wakeMetadata(wakeCard);
        return Boolean(meta) && !("wake_at" in meta) && !("wake_error" in meta);
      },
      { timeout: 5000, label: "清除唤醒时间" },
    );
    const clearedChip = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('定时唤醒卡'));
      return Boolean(card) && !card.querySelector('.wb-todo-wake');
    })()`);
    check("清除后仓库卡 chip 消失", clearedChip === true);

    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '5 分钟后')?.click(); true`,
    );
    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '设置')?.click(); true`,
    );
    await waitFor(
      async () => typeof (await wakeMetadata(wakeCard))?.wake_at === "number",
      { timeout: 5000, label: "再次设置唤醒时间" },
    );
    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '提前唤醒')?.click(); true`,
    );
    await waitForEval(
      "(document.querySelector('.wb-toast')?.textContent || '').includes('队列第')",
      5000,
      "提前唤醒回执",
    );
    const wakeToast = await evaluate(
      "document.querySelector('.wb-toast')?.textContent || ''",
    );
    check(
      "提前唤醒回执显示位次/ETA",
      wakeToast.includes("队列第"),
      wakeToast,
    );
    await waitFor(
      async () => {
        const task = (await httpJson(
          "GET",
          `${base}/api/plugins/kanban/tasks/${encodeURIComponent(wakeCard)}`,
        )).data?.task;
        return task?.status === "ready";
      },
      { timeout: 8000, label: "提前唤醒进 ready" },
    );
    await waitFor(
      async () => {
        const meta = await wakeMetadata(wakeCard);
        return Boolean(meta) && !("wake_at" in meta) && !("wake_next_try_at" in meta);
      },
      { timeout: 5000, label: "提前唤醒清空 wake 字段" },
    );
    const wokenMeta = await wakeMetadata(wakeCard);
    check(
      "提前唤醒进 ready 并清空 wake 字段",
      wokenMeta?.contract_defaults !== undefined &&
        !("wake_at" in wokenMeta) &&
        !("wake_error" in wokenMeta),
      JSON.stringify(wokenMeta),
    );
    if (await evaluate("Boolean(document.querySelector('.wb-detail-close'))")) {
      await evaluate("document.querySelector('.wb-detail-close').click(); true");
      await waitForEval("!Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "关闭唤醒详情");
    }

    await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('缺验收唤醒卡'));
      card?.querySelector('.wb-todo-title-link')?.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-detail-wake'))", 5000, "缺验收唤醒详情");
    await evaluate(
      `[...document.querySelectorAll('.wb-detail-wake button')]
        .find((node) => node.textContent.trim() === '提前唤醒')?.click(); true`,
    );
    await waitForEval(
      "Boolean(document.querySelector('.wb-detail-wake-error'))",
      5000,
      "缺验收唤醒错误",
    );
    const wakeErrorState = await evaluate(`(() => ({
      text: document.querySelector('.wb-detail-wake-error')?.textContent || '',
      badge: document.querySelector('.wb-detail-badge')?.textContent || '',
    }))()`);
    check(
      "缺验收提前唤醒显示警示且卡留仓库",
      wakeErrorState.text.includes("缺验收") && wakeErrorState.badge.includes("已排期"),
      JSON.stringify(wakeErrorState),
    );
    const wakeErrorMeta = await wakeMetadata(wakeErrorCard);
    const wakeErrorStatus = (await httpJson(
      "GET",
      `${base}/api/plugins/kanban/tasks/${encodeURIComponent(wakeErrorCard)}`,
    )).data?.task?.status;
    check(
      "缺验收唤醒写 wake_error 并退避，不派发",
      wakeErrorStatus === "scheduled" &&
        wakeErrorMeta?.wake_error === "missing_acceptance" &&
        typeof wakeErrorMeta?.wake_at === "number" &&
        typeof wakeErrorMeta?.wake_next_try_at === "number",
      JSON.stringify({ status: wakeErrorStatus, metadata: wakeErrorMeta }),
    );
    await evaluate("document.querySelector('.wb-detail-close').click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "关闭缺验收详情");

    // The error now comes from server metadata, not the local click handler:
    // the repository card must show it, and reopening the drawer must render
    // the same server-owned wake_error without another action.
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) =>
          node.textContent.includes('缺验收唤醒卡') &&
          node.querySelector('.wb-todo-wake-error')
        )`,
      5000,
      "仓库卡 wake_error",
    );
    const cardWakeError = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('缺验收唤醒卡'));
      return card?.querySelector('.wb-todo-wake-error')?.textContent || '';
    })()`);
    check(
      "仓库卡显示 wake_error 和下次重试时间",
      cardWakeError.includes("缺验收") && /\d{2}:\d{2}/.test(cardWakeError),
      cardWakeError,
    );

    await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('缺验收唤醒卡'));
      card?.querySelector('.wb-todo-title-link')?.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-detail-wake-error'))", 5000, "重开缺验收详情");
    const reopenedWakeError = await evaluate(
      "document.querySelector('.wb-detail-wake-error')?.textContent || ''",
    );
    check(
      "重开详情仍显示服务端 wake_error",
      reopenedWakeError.includes("缺验收"),
      reopenedWakeError,
    );
    await evaluate("document.querySelector('.wb-detail-close').click(); true");
    await waitForEval("!Boolean(document.querySelector('.wb-detail-drawer'))", 5000, "再次关闭缺验收详情");

    // ---- 8. 409 result re-check (dispatch + live receipt done in 5c) --
    check(
      "409 卡回到仓库（等 parent）",
      (await evaluate(
        "[...document.querySelectorAll('.wb-todo-repo-card')].some((node) => node.textContent.includes('等 parent 卡'))",
      )) === true,
    );
    check("409 后回执仍为空", (await readReceiptText()) === "");

    // ---- final invariant: counts still equal rendered cards ----------
    const finalColumns = await evaluate(`(() => {
      return [...document.querySelectorAll('.wb-todo-col')].map((column) => ({
        count: Number(column.querySelector('.wb-todo-count').textContent || 0),
        cards: column.querySelectorAll('.wb-todo-card').length,
        repoCards: column.querySelectorAll('.wb-todo-repo-card').length,
      }));
    })()`);
    check(
      "最终计数 = 渲染数",
      finalColumns.every(
        (column) =>
          column.count === (column.repoCards > 0 ? column.repoCards : column.cards),
      ),
      JSON.stringify(finalColumns),
    );

    // ---- 9. theme tokens + narrow viewport ----------------------------
    const theme = await evaluate(`(async () => {
      const root = document.documentElement;
      const flow = document.querySelector('.wb-todo-flow');
      root.dataset.theme = 'dark';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const dark = getComputedStyle(flow).backgroundColor;
      root.dataset.theme = 'light';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const light = getComputedStyle(flow).backgroundColor;
      return { dark, light };
    })()`);
    check("明暗主题走 base.css token", theme.dark !== theme.light, JSON.stringify(theme));

    // Badge is 10.5px text and the receipt is 12px: require WCAG AA 4.5:1.
    const themeContrast = await contrastMatrix(
      {
        ready: ".wb-todo-badge:not(.running)",
        running: ".wb-todo-badge.running",
        receipt: ".wb-todo-receipt",
        navCountActive: ".wb-nav-btn.on .wb-nav-count",
        navCountIdle: ".wb-nav-btn:not(.on) .wb-nav-count",
        navActive: ".wb-nav-btn.on",
        tabLabelActive: ".wb-tab.on .wb-tab-label",
        tabLabelIdle: ".wb-tab:not(.on) .wb-tab-label",
        tabActive: ".wb-tab.on",
        reviewActive: ".wb-review-entry",
        acceptEmpty: ".wb-todo-accept.empty",
        block: ".wb-block",
      },
      { forceActiveReview: true },
    );
    const contrastOk = (entry) =>
      Object.values(entry || {}).every((value) => typeof value === "number" && value >= 4.5);
    check(
      "ready 徽标明暗主题对比度 >= 4.5",
      themeContrast?.light?.ready >= 4.5 && themeContrast?.dark?.ready >= 4.5,
      JSON.stringify(themeContrast),
    );
    check(
      "running 徽标明暗主题对比度 >= 4.5",
      themeContrast?.light?.running >= 4.5 && themeContrast?.dark?.running >= 4.5,
      JSON.stringify(themeContrast),
    );
    check(
      "回执条明暗主题对比度 >= 4.5",
      themeContrast?.light?.receipt >= 4.5 && themeContrast?.dark?.receipt >= 4.5,
      JSON.stringify(themeContrast),
    );
    check(
      "外壳/待办文本明暗主题对比度 >= 4.5",
      contrastOk(themeContrast?.light) && contrastOk(themeContrast?.dark),
      JSON.stringify(themeContrast),
    );

    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 420,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise((resolveWait) => setTimeout(resolveWait, 100));
    const narrow = await evaluate(`(() => {
      const grid = document.querySelector('.wb-todo-grid');
      const style = getComputedStyle(grid);
      return {
        overflowX: style.overflowX,
        columns: style.gridTemplateColumns.split(' ').length,
        width: grid.clientWidth,
        scrollWidth: grid.scrollWidth,
        planes: [...document.querySelectorAll('.wb-todo-plane')]
          .map((node) => node.getBoundingClientRect().width),
      };
    })()`);
    check(
      "窄视口仓库列 >=200px",
      narrow.planes.length === 2 && narrow.planes.every((width) => width >= 200),
      JSON.stringify(narrow.planes),
    );
    check(
      "窄视口整栏横向滚动",
      narrow.overflowX === "auto" && narrow.columns === 3 && narrow.scrollWidth > narrow.width,
      `${narrow.scrollWidth} > ${narrow.width}`,
    );
    const narrowShell = await evaluate(`(() => ({
      sidebar: getComputedStyle(document.querySelector('.wb-sidebar')).display,
      topbar: getComputedStyle(document.querySelector('.wb-topbar')).display,
      tabs: [...document.querySelectorAll('.wb-tab-label')].map((node) => node.textContent),
      docOverflow: document.documentElement.scrollWidth - window.innerWidth,
    }))()`);
    check(
      "窄视口侧栏隐藏、顶栏 tabs 可用、页面不横向溢出",
      narrowShell.sidebar === "none" &&
        narrowShell.topbar !== "none" &&
        JSON.stringify(narrowShell.tabs) === JSON.stringify(["待办", "看板", "已完成"]) &&
        narrowShell.docOverflow <= 0,
      JSON.stringify(narrowShell),
    );
    await cdp.send("Emulation.clearDeviceMetricsOverride");

    // ---- 10. external create appears through polling while focused ----
    check("外部建卡前打开今日新建卡", (await openCreateCard("今日")) === true);
    await evaluate("document.querySelector('.wb-todo-create-title').focus()");
    const externalCard = await createUpstream("外部轮询卡");
    await setMetadata(externalCard, {
      bucket: "today",
      contract: { input: "", accept: "", ready: false },
    });
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')[0].querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('外部轮询卡'))`,
      7000,
      "外部建卡经 5s 轮询出现",
    );
    const focusHeld = await evaluate(
      "document.activeElement === document.querySelector('.wb-todo-create-title')",
    );
    check("外部建卡 6s 内轮询出现且不抢输入焦点", focusHeld === true);
    await evaluate(
      "[...document.querySelectorAll('.wb-todo-create-btn')].find((node) => node.textContent.trim() === '取消')?.click(); true",
    );
    await waitForEval("!document.querySelector('.wb-todo-create-card')", 3000, "关闭外部建卡用例的新建卡");

    // ---- 11. today/long lists sort by created_at desc -----------------
    const seedRepoCard = async (title, bucket) => {
      const taskId = await createUpstream(title);
      await setMetadata(taskId, {
        bucket,
        contract: { input: "", accept: "", ready: false },
      });
    };
    await seedRepoCard("排序旧今日", "today");
    await seedRepoCard("排序旧长期", "long");
    await new Promise((resolveWait) => setTimeout(resolveWait, 1200));
    await seedRepoCard("排序新今日", "today");
    await seedRepoCard("排序新长期", "long");
    await waitForEval(
      `(() => {
        const titles = (index) => [...document.querySelectorAll('.wb-todo-plane')[index]
          .querySelectorAll('.wb-todo-title')].map((node) => node.textContent);
        return titles(0).includes('排序新今日') && titles(0).includes('排序旧今日') &&
          titles(1).includes('排序新长期') && titles(1).includes('排序旧长期');
      })()`,
      7000,
      "排序卡轮询出现",
    );
    const order = await evaluate(`(() => {
      const titles = (index) => [...document.querySelectorAll('.wb-todo-plane')[index]
        .querySelectorAll('.wb-todo-title')].map((node) => node.textContent);
      return { today: titles(0), long: titles(1) };
    })()`);
    const todayNew = order.today.indexOf("排序新今日");
    const todayOld = order.today.indexOf("排序旧今日");
    const longNew = order.long.indexOf("排序新长期");
    const longOld = order.long.indexOf("排序旧长期");
    check(
      "今日按 created_at 倒序",
      todayNew >= 0 && todayOld >= 0 && todayNew < todayOld,
      JSON.stringify(order.today),
    );
    check(
      "长期按 created_at 倒序",
      longNew >= 0 && longOld >= 0 && longNew < longOld,
      JSON.stringify(order.long),
    );

    // ---- 12. 长期分区右上角新建，落 long bucket -----------------------
    check("长期分区右上角有新建按钮", (await openCreateCard("长期")) === true);
    const createPlacement = await evaluate(`(() => {
      const planes = [...document.querySelectorAll('.wb-todo-plane')];
      return {
        inLong: Boolean(planes[1].querySelector('.wb-todo-create-card')),
        inToday: Boolean(planes[0].querySelector('.wb-todo-create-card')),
        label: document.querySelector('.wb-todo-create-bucket')?.textContent || '',
      };
    })()`);
    check(
      "新建卡开在长期分区",
      createPlacement.inLong === true &&
        createPlacement.inToday === false &&
        createPlacement.label.includes("长期"),
      JSON.stringify(createPlacement),
    );
    await evaluate("window.__wbProbe.create = 0;");
    check("长期新建卡可填标题", (await setCreateField(".wb-todo-create-title", "探针长期卡")) === true);
    check("长期新建卡提交", (await submitCreateCard()) === true);
    await waitForEval("window.__wbProbe.create === 1", 5000, "长期建卡请求");
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-plane')[1].querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('探针长期卡'))`,
      8000,
      "长期新卡渲染",
    );
    const longBoard = (await httpJson("GET", `${base}/api/plugins/kanban/board?include_archived=true`)).data;
    const longTask = longBoard.columns
      .flatMap((column) => column.tasks)
      .find((task) => task.title === "探针长期卡");
    const longMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(longTask.id)}`,
    )).data?.[longTask.id];
    check(
      "长期分区建卡落长期 bucket",
      longTask?.status === "scheduled" && longMeta?.bucket === "long",
      JSON.stringify({ status: longTask?.status, metadata: longMeta }),
    );

    // ---- 13. tab switching keeps the open create card draft ----------
    check("切 tab 前打开今日新建卡", (await openCreateCard("今日")) === true);
    check("切 tab 前填写草稿", (await setCreateField(".wb-todo-create-title", "跨 tab 草稿")) === true);
    const tabLabels = await evaluate(
      "[...document.querySelectorAll('.wb-tab')].map((node) => node.querySelector('.wb-tab-label')?.textContent || node.textContent)",
    );
    check(
      "待办 / 看板 / 已完成三个 tab",
      JSON.stringify(tabLabels) === JSON.stringify(["待办", "看板", "已完成"]),
      JSON.stringify(tabLabels),
    );
    await evaluate("document.querySelectorAll('.wb-tab')[1].click()");
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
    const flowView = await evaluate(`(() => {
      const panes = [...document.querySelectorAll('.wb-pane')];
      return {
        displays: panes.map((node) => getComputedStyle(node).display),
        columns: document.querySelectorAll('.wb-board .wb-col').length,
        names: [...document.querySelectorAll('.wb-board .wb-col .wb-cname')].map(
          (node) => node.textContent,
        ),
      };
    })()`);
    check("看板 8 列正常", flowView.columns === 8, String(flowView.columns));
    check(
      "看板 8 列名称/顺序不变（无 007 的 5 列）",
      JSON.stringify(flowView.names) ===
        JSON.stringify(["待整理", "待执行", "已排期", "就绪", "运行中", "待拍板", "待验收", "已完成"]),
      JSON.stringify(flowView.names),
    );
    const boardVisual = await evaluate(`(() => {
      const col = document.querySelector('.wb-board .wb-col');
      const card = document.querySelector('.wb-board .wb-card');
      return {
        radius: getComputedStyle(col).borderTopLeftRadius,
        colBg: getComputedStyle(col).backgroundColor,
        cardBg: card ? getComputedStyle(card).backgroundColor : null,
      };
    })()`);
    check(
      "看板 007 列/卡片视觉（10px 凹槽列 + 卡片底色）",
      boardVisual.radius === "10px" &&
        boardVisual.colBg !== "rgba(0, 0, 0, 0)" &&
        boardVisual.cardBg !== null &&
        boardVisual.cardBg !== boardVisual.colBg,
      JSON.stringify(boardVisual),
    );
    await waitForEval(
      `[...document.querySelectorAll('.wb-board .wb-card')]
        .some((node) => node.textContent.includes('探针改名卡'))`,
      8000,
      "看板出现 scheduled 卡",
    );
    check(
      "看板拖 scheduled → 待执行",
      (await dragBoardCardToColumn("探针改名卡", "待执行")).ok === true,
    );
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
    const boardGuardToast = await boardToastText();
    const boardGuardStatus = (await httpJson(
      "GET",
      `${base}/api/plugins/kanban/tasks/${encodeURIComponent(createdCard.id)}`,
    )).data?.task?.status;
    check(
      "看板拒绝把仓库卡移回待执行",
      boardGuardToast.includes("仓库卡不能") && boardGuardStatus === "scheduled",
      JSON.stringify({ toast: boardGuardToast, status: boardGuardStatus }),
    );
    check(
      "切到看板只显示看板面板",
      flowView.displays.length === 2 &&
        flowView.displays[0] === "none" &&
        flowView.displays[1] === "flex",
      JSON.stringify(flowView.displays),
    );
    await evaluate("document.querySelectorAll('.wb-tab')[0].click()");
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
    const todoView = await evaluate(`(() => {
      const panes = [...document.querySelectorAll('.wb-pane')];
      return {
        displays: panes.map((node) => getComputedStyle(node).display),
        columns: document.querySelectorAll('.wb-todo-col').length,
        draft: document.querySelector('.wb-todo-create-title')?.value || '',
        createOpen: Boolean(document.querySelector('.wb-todo-create-card')),
      };
    })()`);
    check(
      "切回待办保留草稿",
      todoView.displays.length === 2 &&
        todoView.displays[0] === "flex" &&
        todoView.displays[1] === "none" &&
        todoView.draft === "跨 tab 草稿" &&
        todoView.createOpen === true,
      JSON.stringify(todoView),
    );
    check("待办三列仍在", todoView.columns === 3, String(todoView.columns));
    await evaluate(
      "[...document.querySelectorAll('.wb-todo-create-btn')].find((node) => node.textContent.trim() === '取消')?.click(); true",
    );
    await waitForEval("!document.querySelector('.wb-todo-create-card')", 3000, "关闭跨 tab 草稿");

    // ---- 14. counts still equal rendered cards after all additions ----
    const finalCounts = await evaluate(`(() => {
      return [...document.querySelectorAll('.wb-todo-col')].map((column) => ({
        count: Number(column.querySelector('.wb-todo-count').textContent || 0),
        cards: column.querySelectorAll('.wb-todo-card').length,
        repoCards: column.querySelectorAll('.wb-todo-repo-card').length,
      }));
    })()`);
    check(
      "最终计数仍 = 渲染数",
      finalCounts.every(
        (column) => column.count === (column.repoCards > 0 ? column.repoCards : column.cards),
      ),
      JSON.stringify(finalCounts),
    );

    // ---- 15. Board ready drop = dispatch (snapshot + position) --------
    const boardDispatchCard = await createUpstream("看板派发卡");
    await setMetadata(boardDispatchCard, {
      bucket: "today",
      contract: { accept: "看板派发验收" },
    });
    await evaluate("document.querySelectorAll('.wb-tab')[1].click()");
    await waitForEval(
      `[...document.querySelectorAll('.wb-card')]
        .some((node) => node.textContent.includes('看板派发卡'))`,
      12000,
      "看板派发卡渲染",
    );
    await evaluate(
      "window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null;",
    );
    const boardReadyDrag = await dragBoardCardToColumn("看板派发卡", "就绪");
    check(
      "看板就绪列接受投放",
      boardReadyDrag.ok === true && boardReadyDrag.overPrevented === true,
      JSON.stringify(boardReadyDrag),
    );
    await waitForEval("window.__wbProbe.dispatch === 1", 5000, "看板派发请求");
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      5000,
      "看板派发响应",
    );
    const boardDispatch = await evaluate("window.__wbProbe.lastDispatch");
    check(
      "看板拖到就绪走 dispatch（ready + 位次）",
      boardDispatch.status === "ready" && boardDispatch.position >= 1,
      JSON.stringify(boardDispatch),
    );
    const boardCardStatus = (
      await httpJson("GET", `${base}/api/plugins/kanban/tasks/${boardDispatchCard}`)
    ).data?.task?.status;
    check("看板拖到就绪后上游状态 ready", boardCardStatus === "ready", String(boardCardStatus));
    const boardMeta = (
      await httpJson(
        "GET",
        `${base}/api/workbench/metadata?ids=${encodeURIComponent(boardDispatchCard)}`,
      )
    ).data?.[boardDispatchCard];
    check(
      "看板派发写入 contract_defaults 快照",
      boardMeta?.contract_defaults?.max_minutes === DEFAULTS.budget.max_minutes &&
        boardMeta?.contract_defaults?.max_cost === DEFAULTS.budget.max_cost &&
        JSON.stringify(boardMeta?.contract_defaults?.deny_paths) ===
          JSON.stringify(DEFAULTS.budget.deny_paths),
      JSON.stringify(boardMeta?.contract_defaults),
    );

    const boardBlockedCard = await createUpstream("看板空验收卡");
    await setMetadata(boardBlockedCard, {
      bucket: "today",
      contract: { input: "", accept: "", ready: false },
    });
    await waitForEval(
      `[...document.querySelectorAll('.wb-card')]
        .some((node) => node.textContent.includes('看板空验收卡'))`,
      12000,
      "看板空验收卡渲染",
    );
    const dispatchBeforeBoardReject = await evaluate("window.__wbProbe.dispatch");
    await dragBoardCardToColumn("看板空验收卡", "就绪");
    await waitFor(
      async () => (await boardToastText()) === "先补一句怎样算做完",
      { timeout: 5000, interval: 50, label: "看板闸门中文提示" },
    );
    check("看板闸门提示为中文", true);
    const boardRejected = await evaluate(
      "window.__wbProbe.dispatch",
    );
    const boardBlockedStatus = (
      await httpJson("GET", `${base}/api/plugins/kanban/tasks/${boardBlockedCard}`)
    ).data?.task?.status;
    check(
      "看板空验收被闸门拦住（一次请求、状态不变）",
      boardRejected === dispatchBeforeBoardReject + 1 && boardBlockedStatus === "triage",
      JSON.stringify({ dispatch: boardRejected, status: boardBlockedStatus }),
    );

    // ---- 16. dispatch timeout clears receipt + in-flight guard --------
    await evaluate("document.querySelectorAll('.wb-tab')[0].click()");
    const timeoutCard = await createUpstream("超时卡");
    await setMetadata(timeoutCard, { bucket: "today", contract: { accept: "超时验收" } });
    const afterTimeoutCard = await createUpstream("超时后卡");
    await setMetadata(afterTimeoutCard, {
      bucket: "today",
      contract: { accept: "超时后验收" },
    });
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('超时卡')) &&
       [...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('超时后卡'))`,
      12000,
      "超时测试卡渲染",
    );
    await evaluate(
      "window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null; window.__wbProbe.hangDispatch = true;",
    );
    check("拖拽会挂起的卡", (await dragCardToFlow("超时卡")) === true);
    await waitForEval(
      "(document.querySelector('.wb-todo-receipt-text')?.textContent || '') === '正在派发…'",
      1500,
      "超时卡 pending 回执",
    );
    await waitFor(
      async () =>
        (await evaluate("document.querySelector('.wb-toast')?.textContent || ''")) ===
        "派发超时，请刷新后重试",
      {
        timeout: DISPATCH_TIMEOUT_MS + 3000,
        interval: 100,
        label: "派发超时提示",
      },
    );
    check("dispatch 超时显示错误提示", true);
    const timeoutState = await evaluate(`(() => ({
      receipt: document.querySelector('.wb-todo-receipt-text')?.textContent || '',
      dispatch: window.__wbProbe.dispatch,
      stillRepo: [...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('超时卡')),
    }))()`);
    check("超时后清空 pending 回执", timeoutState.receipt === "", JSON.stringify(timeoutState));
    check("超时只发一次派发请求", timeoutState.dispatch === 1, String(timeoutState.dispatch));
    check("超时卡留在仓库", timeoutState.stillRepo === true);

    await evaluate(
      "window.__wbProbe.hangDispatch = false; window.__wbProbe.dispatch = 0; window.__wbProbe.lastDispatch = null;",
    );
    check("超时后拖拽下一张卡", (await dragCardToFlow("超时后卡")) === true);
    await waitForEval("window.__wbProbe.dispatch === 1", 5000, "超时后派发请求");
    await waitForEval(
      "Boolean(window.__wbProbe.lastDispatch && window.__wbProbe.lastDispatch.ok)",
      6000,
      "超时后派发响应",
    );
    check("超时后 in-flight 串行锁已释放", true);

    // ---- 17. 已完成（只读、按天分组）+ 主题持久化 -------------------
    await cdp.send("Page.reload");
    await waitForEval(
      "Boolean(document.querySelector('.wb-shell')) && document.querySelectorAll('.wb-todo-col').length === 3",
      15000,
      "重载后外壳与待办",
    );
    await evaluate(`(() => {
      const tab = [...document.querySelectorAll('.wb-tab')].find(
        (node) => node.querySelector('.wb-tab-label')?.textContent === '已完成',
      );
      if (!tab) return false;
      tab.click();
      return true;
    })()`);
    await waitForEval("Boolean(document.querySelector('.wb-done-wrap'))", 5000, "已完成视图挂载");
    await waitForEval(
      "document.querySelector('.wb-done-empty')?.textContent.includes('还没有已完成的卡') === true",
      8000,
      "已完成空态",
    );
    const emptyDone = await evaluate("document.querySelector('.wb-done-empty')?.textContent || ''");
    check("已完成空态文案", emptyDone.includes("还没有已完成的卡"), emptyDone);

    const doneTodayCard = await createUpstream("已完成今天卡");
    const doneYesterdayCard = await createUpstream("已完成昨天卡");
    // 锚定本地正午，避免 23:59 跑探针时「今天」跨到明天。
    const noon = new Date();
    noon.setHours(12, 0, 0, 0);
    const noonSeconds = Math.floor(noon.getTime() / 1000);
    // 直接写 throwaway kanban.db：完成列与 completed_at 是上游内核态，
    // 这里只为了让已完成视图拿到两个已知本地日与一条 run 摘要。
    const markDone = (taskId, completedAt, summary) => {
      updateKanban("UPDATE tasks SET status=? WHERE id=?", taskId, "done");
      updateKanban("UPDATE tasks SET completed_at=? WHERE id=?", taskId, completedAt);
      updateKanban(
        "INSERT INTO task_runs (summary, task_id, status, started_at, ended_at) VALUES (?, ?, 'done', 0, 0)",
        taskId,
        summary,
      );
    };
    markDone(doneTodayCard, noonSeconds, "今天的完成摘要");
    markDone(doneYesterdayCard, noonSeconds - 86400, "昨天的完成摘要");

    await waitForEval(
      "document.querySelectorAll('.wb-done-row').length === 2",
      12000,
      "已完成两行渲染",
    );
    const doneState = await evaluate(`(() => ({
      groups: [...document.querySelectorAll('.wb-done-day > span:first-child')].map(
        (node) => node.textContent,
      ),
      titles: [...document.querySelectorAll('.wb-done-title')].map((node) => node.textContent),
      times: [...document.querySelectorAll('.wb-done-time')].map((node) => node.textContent),
      summaries: [...document.querySelectorAll('.wb-done-summary')].map((node) => node.textContent),
      sessions: document.querySelectorAll('.wb-done-session').length,
    }))()`);
    check(
      "已完成按本地日倒序分组（今天 → 昨天）",
      JSON.stringify(doneState.groups) === JSON.stringify(["今天", "昨天"]),
      JSON.stringify(doneState.groups),
    );
    check(
      "已完成行按 completed_at 倒序且展示摘要/时间",
      JSON.stringify(doneState.titles) === JSON.stringify(["已完成今天卡", "已完成昨天卡"]) &&
        doneState.summaries.includes("今天的完成摘要") &&
        doneState.times.every((text) => /^\d{2}:\d{2}$/.test(text)),
      JSON.stringify(doneState),
    );
    check("已完成行有会话入口", doneState.sessions === 2, String(doneState.sessions));

    const doneTextContrast = await contrastMatrix({
      title: ".wb-done-title",
      summary: ".wb-done-summary",
      time: ".wb-done-time",
    });
    check(
      "已完成行文本明暗主题对比度 >= 4.5",
      Object.values(doneTextContrast.light).every((value) => value >= 4.5) &&
        Object.values(doneTextContrast.dark).every((value) => value >= 4.5),
      JSON.stringify(doneTextContrast),
    );

    const openedDoneSession = await evaluate(`(() => {
      const button = [...document.querySelectorAll('.wb-done-session')].find(
        (node) => node.dataset.taskId === ${JSON.stringify(doneTodayCard)},
      );
      if (!button) return false;
      button.click();
      return true;
    })()`);
    check("点击已完成行会话入口", openedDoneSession === true);
    await waitForEval("Boolean(document.querySelector('.wb-session-drawer'))", 8000, "已完成会话抽屉");
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 8000, "会话缺失提示");
    const doneSession = await evaluate(`(() => ({
      title: document.querySelector('.wb-session-title')?.textContent || '',
      id: document.querySelector('.wb-session-sub')?.textContent || '',
      error: document.querySelector('.wb-session-error')?.textContent || '',
    }))()`);
    check(
      "会话缺失由查看器提示（标题/task id/no_session 文案）",
      doneSession.title === "已完成今天卡" &&
        doneSession.id === doneTodayCard &&
        doneSession.error.includes("没有执行会话"),
      JSON.stringify(doneSession),
    );
    await evaluate(`(() => {
      window.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Escape', bubbles: true, cancelable: true,
      }));
      return true;
    })()`);
    await waitForEval(
      "!Boolean(document.querySelector('.wb-session-drawer'))",
      5000,
      "已完成会话抽屉关闭",
    );

    // 对比度矩阵直接改过 dataset；重载让 React 的 theme state 与 DOM 重新对齐。
    await cdp.send("Page.reload");
    await waitForEval(
      "Boolean(document.querySelector('.wb-shell'))",
      15000,
      "主题切换前重载",
    );
    const themeBefore = await evaluate(
      "document.documentElement.getAttribute('data-theme')",
    );
    const expectedTheme = themeBefore === "dark" ? "light" : "dark";
    const expectedColor = expectedTheme === "dark" ? "#070809" : "#ececea";
    await evaluate("document.querySelector('.wb-theme-toggle').click(); true");
    // 可见 root 有 250ms 背景过渡：等过渡结束再断言 canvas 色。
    await new Promise((resolveWait) => setTimeout(resolveWait, 400));
    const themeAfter = await evaluate(`(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      stored: window.localStorage.getItem('wb-theme'),
      meta: document.querySelector('meta[name="theme-color"]')?.getAttribute('content'),
      rootBg: getComputedStyle(document.querySelector('.wb-root')).backgroundColor,
      shellBg: getComputedStyle(document.querySelector('.wb-shell')).backgroundColor,
    }))()`);
    check(
      "主题切换：data-theme / localStorage / theme-color 同步",
      themeAfter.theme === expectedTheme &&
        themeAfter.stored === expectedTheme &&
        themeAfter.meta === expectedColor,
      JSON.stringify({ before: themeBefore, after: themeAfter }),
    );
    check(
      "主题切换后可见 root/shell 背景跟随 token",
      themeAfter.rootBg ===
        (expectedTheme === "dark" ? "rgb(7, 8, 9)" : "rgb(236, 236, 234)") &&
        themeAfter.shellBg ===
          (expectedTheme === "dark" ? "rgb(11, 13, 16)" : "rgb(251, 251, 250)"),
      JSON.stringify({ rootBg: themeAfter.rootBg, shellBg: themeAfter.shellBg }),
    );

    await cdp.send("Page.reload");
    await waitForEval(
      "Boolean(document.querySelector('.wb-shell'))",
      15000,
      "主题持久化重载",
    );
    const persistedTheme = await evaluate(`(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      stored: window.localStorage.getItem('wb-theme'),
      rootBg: getComputedStyle(document.querySelector('.wb-root')).backgroundColor,
      shellBg: getComputedStyle(document.querySelector('.wb-shell')).backgroundColor,
    }))()`);
    check(
      "刷新后主题保持（首屏脚本先于样式）",
      persistedTheme.theme === expectedTheme &&
        persistedTheme.stored === expectedTheme &&
        persistedTheme.rootBg ===
          (expectedTheme === "dark" ? "rgb(7, 8, 9)" : "rgb(236, 236, 234)") &&
        persistedTheme.shellBg ===
          (expectedTheme === "dark" ? "rgb(11, 13, 16)" : "rgb(251, 251, 250)"),
      JSON.stringify(persistedTheme),
    );

    // ---- 18. plugin host: ready drop blocked, other columns work ------
    const reactUmd = readFileSync(
      join(uiRoot, "node_modules/react/umd/react.production.min.js"),
      "utf8",
    );
    const reactDomUmd = readFileSync(
      join(uiRoot, "node_modules/react-dom/umd/react-dom.production.min.js"),
      "utf8",
    );
    const pluginBundle = readFileSync(join(uiRoot, "dist/plugin/index.js"), "utf8");
    const PLUGIN_READONLY_NOTICE = "插件宿主只读；请在 127.0.0.1:8787 独立版派发";

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
    await evaluate(reactUmd + "\n;true");
    await evaluate(reactDomUmd + "\n;true");
    check(
      "插件探针 React UMD 就绪",
      (await evaluate("typeof React + ':' + typeof ReactDOM")) === "object:object",
      await evaluate("typeof React + ':' + typeof ReactDOM"),
    );

    // 8. host detection: SDK without fetchJSON must fail loudly, not render
    // the standalone/silently-broken UI.
    await evaluate(`(() => {
      document.body.innerHTML = '<div id="workbench-root"></div>';
      window.__HERMES_PLUGIN_SDK__ = { React: window.React, ReactDOM: window.ReactDOM };
      return true;
    })()`);
    const unsupportedHost = await evaluate(
      "(() => { try {\n" +
        pluginBundle +
        "\n; return 'no-error'; } catch (error) { return String((error && error.message) || error); } })()",
    );
    check(
      "缺少 fetchJSON 的宿主抛出明确错误",
      typeof unsupportedHost === "string" &&
        unsupportedHost.includes("fetchJSON") &&
        unsupportedHost.includes("不支持的插件宿主"),
      String(unsupportedHost),
    );

    await evaluate(`(() => {
      document.body.innerHTML = '<div id="workbench-root"></div>';
      window.__wbProbe = { dispatch: 0, readyPatch: 0, otherPatch: 0 };
      const original = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        const method = ((init && init.method) || "GET").toUpperCase();
        if (url.includes("/api/workbench/dispatch/")) window.__wbProbe.dispatch += 1;
        if (method === "PATCH" && url.includes("/api/plugins/kanban/tasks/")) {
          const body = String((init && init.body) || "");
          if (body.includes('"ready"')) window.__wbProbe.readyPatch += 1;
          else window.__wbProbe.otherPatch += 1;
        }
        return original(input, init);
      };
      window.__HERMES_PLUGIN_SDK__ = {
        React: window.React,
        ReactDOM: window.ReactDOM,
        // The real dashboard SDK returns parsed JSON and throws on non-2xx;
        // mirror that shape so the plugin sees the same data contract.
        fetchJSON: async (url, init) => {
          const response = await window.fetch(url, init);
          const text = await response.text();
          let data = null;
          try { data = text ? JSON.parse(text) : null; } catch { data = text; }
          if (!response.ok) {
            const detail = data && (data.detail || data.error);
            const error = new Error(
              Array.isArray(detail) ? JSON.stringify(detail) : String(detail || "HTTP " + response.status),
            );
            error.status = response.status;
            if (data && typeof data === "object" && typeof data.error === "string") {
              error.code = data.error;
            }
            throw error;
          }
          return data;
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
    const pluginHostState = await evaluate(`(() => ({
      registered: window.__wbPlugin?.name || '',
      tabs: [...document.querySelectorAll('.wb-tab')].map((node) => node.textContent),
      notice: document.querySelector('.wb-notice')?.textContent || '',
      hint: document.querySelector('.wb-board-wrap .wb-hint')?.textContent || '',
      todoMounted: Boolean(document.querySelector('.wb-todo-wrap')),
      sidebarMounted: Boolean(document.querySelector('.wb-sidebar')),
      shellMounted: Boolean(document.querySelector('.wb-shell')),
      doneMounted: Boolean(document.querySelector('.wb-done-wrap')),
      themeToggle: Boolean(document.querySelector('.wb-theme-toggle')),
    }))()`);
    check(
      "插件宿主只注册看板且无侧栏/待办/已完成",
      pluginHostState.registered === "workbench" &&
        JSON.stringify(pluginHostState.tabs) === JSON.stringify(["看板"]) &&
        pluginHostState.todoMounted === false &&
        pluginHostState.sidebarMounted === false &&
        pluginHostState.shellMounted === false &&
        pluginHostState.doneMounted === false &&
        pluginHostState.themeToggle === false,
      JSON.stringify(pluginHostState),
    );
    check(
      "插件宿主显示只读提示",
      pluginHostState.notice === PLUGIN_READONLY_NOTICE &&
        pluginHostState.hint === PLUGIN_READONLY_NOTICE,
      JSON.stringify(pluginHostState),
    );

    const pluginCard = await createUpstream("插件拖拽卡");
    await waitForEval(
      `[...document.querySelectorAll('.wb-card')]
        .some((node) => node.textContent.includes('插件拖拽卡'))`,
      12000,
      "插件宿主卡片渲染",
    );
    const pluginOverState = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-card')]
        .find((node) => node.textContent.includes('插件拖拽卡'));
      const byLabel = (label) => [...document.querySelectorAll('.wb-col')]
        .find((node) => node.querySelector('.wb-cname')?.textContent === label);
      const dataTransfer = new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
      const readyOver = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer });
      byLabel('就绪').dispatchEvent(readyOver);
      const todoOver = new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer });
      byLabel('待执行').dispatchEvent(todoOver);
      return {
        readyPrevented: readyOver.defaultPrevented,
        todoPrevented: todoOver.defaultPrevented,
      };
    })()`);
    check(
      "插件宿主就绪列不是投放目标，其他列仍可投放",
      pluginOverState.readyPrevented === false && pluginOverState.todoPrevented === true,
      JSON.stringify(pluginOverState),
    );

    await dragBoardCardToColumn("插件拖拽卡", "就绪");
    await waitFor(
      async () => (await boardToastText()) === PLUGIN_READONLY_NOTICE,
      { timeout: 5000, interval: 50, label: "插件宿主只读提示" },
    );
    check("插件宿主就绪拖拽显示只读提示", true);
    const pluginReadyState = await evaluate(
      "window.__wbProbe.dispatch + window.__wbProbe.readyPatch",
    );
    const pluginStatusAfterReady = (
      await httpJson("GET", `${base}/api/plugins/kanban/tasks/${pluginCard}`)
    ).data?.task?.status;
    check(
      "插件宿主就绪拖拽被客户端拦截（零请求、状态不变）",
      pluginReadyState === 0 && pluginStatusAfterReady === "triage",
      JSON.stringify({ requests: pluginReadyState, status: pluginStatusAfterReady }),
    );

    await dragBoardCardToColumn("插件拖拽卡", "待执行");
    await waitForEval("window.__wbProbe.otherPatch === 1", 5000, "插件宿主其他列 PATCH");
    await waitFor(
      async () => {
        const status = (
          await httpJson("GET", `${base}/api/plugins/kanban/tasks/${pluginCard}`)
        ).data?.task?.status;
        return status === "todo";
      },
      { timeout: 8000, interval: 100, label: "插件宿主其他列落 todo" },
    );
    check("插件宿主其他列仍可拖拽（PATCH 生效）", true);

    console.log(`\nPROBE OK — ${checks.length} checks passed`);
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
