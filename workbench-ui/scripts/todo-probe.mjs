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
 *   - new card lands in today, triage, empty acceptance, not auto-dispatched
 *   - 409 parent: toast「已转为 todo，等 parent 完成」and card leaves repository
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
          lastDispatch: null,
          holdMs: 0,
        };
        const original = window.fetch.bind(window);
        window.fetch = (input, init) => {
          const url = typeof input === "string" ? input : (input && input.url) || "";
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
        repoCard.children[0] === "wb-todo-title" &&
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
      `(document.querySelector('.wb-toast')?.textContent || '') === '已转为 todo，等 parent 完成'`,
      8000,
      "409 toast",
    );
    await evaluate("window.__wbProbe.holdMs = 0;");
    await waitForEval(
      `![...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('等 parent 卡'))`,
      8000,
      "409 卡离开仓库",
    );
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
      blockedState.toast === "已转为 todo，等 parent 完成",
      blockedState.toast,
    );
    const blockedStatus = (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${blockedChild}`)).data?.task?.status;
    check("有未完成 parent 的卡落 todo", blockedStatus === "todo", String(blockedStatus));

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

    // ---- 6. create card lands in today, not dispatched ---------------
    const emptyTitle = await evaluate(`(async () => {
      window.__wbProbe.create = 0;
      const input = document.querySelector('.wb-todo-composer input');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '   ');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 30));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 150));
      return window.__wbProbe.create;
    })()`);
    check("空标题不建卡", emptyTitle === 0, String(emptyTitle));

    await evaluate(`(async () => {
      window.__wbProbe.create = 0;
      window.__wbProbe.dispatch = 0;
      const input = document.querySelector('.wb-todo-composer input');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '探针新建卡');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 30));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      return true;
    })()`);
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
        acceptEmpty: card.querySelector('.wb-todo-accept').classList.contains('empty'),
        dispatch: window.__wbProbe.dispatch,
      };
    })()`);
    check("新建卡落今日", createdInfo.inToday === true && createdInfo.inLong === false);
    check("新建卡验收空警示", createdInfo.acceptEmpty === true);
    check("新建卡不自动派发", createdInfo.dispatch === 0, String(createdInfo.dispatch));

    const boardAfterCreate = (await httpJson("GET", `${base}/api/plugins/kanban/board?include_archived=true`)).data;
    const createdCard = boardAfterCreate.columns
      .flatMap((column) => column.tasks)
      .find((task) => task.title === "探针新建卡");
    check("新建卡上游状态 triage", createdCard?.status === "triage", String(createdCard?.status));
    const createdMeta = (await httpJson(
      "GET",
      `${base}/api/workbench/metadata?ids=${encodeURIComponent(createdCard.id)}`,
    )).data?.[createdCard.id];
    check("新建卡 metadata bucket=today", createdMeta?.bucket === "today", JSON.stringify(createdMeta));

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

    // ---- 8. 409 result re-check (dispatch + live receipt done in 5c) --
    check(
      "409 卡仍不在仓库",
      (await evaluate(
        "[...document.querySelectorAll('.wb-todo-repo-card')].some((node) => node.textContent.includes('等 parent 卡'))",
      )) === false,
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
      const before = getComputedStyle(flow).backgroundColor;
      root.dataset.theme = 'light';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const after = getComputedStyle(flow).backgroundColor;
      root.dataset.theme = 'dark';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      return { before, after };
    })()`);
    check("明暗主题走 base.css token", theme.before !== theme.after, JSON.stringify(theme));

    // Badge is 10.5px text and the receipt is 12px: require WCAG AA 4.5:1.
    const themeContrast = await evaluate(`(() => {
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
      const contrastFor = (selector) => {
        const badge = document.querySelector(selector);
        if (!badge) return null;
        const style = getComputedStyle(badge);
        const text = parse(style.color);
        const layer = parse(style.backgroundColor);
        let base = parse(getComputedStyle(badge.closest('.wb-todo-card')).backgroundColor);
        const page = parse(getComputedStyle(document.body).backgroundColor);
        if (!text || !layer || !base || !page) return null;
        if (base.a < 1) base = over(base, page);
        return ratio(text, over(layer, base));
      };
      const contrastOnPage = (selector) => {
        const node = document.querySelector(selector);
        if (!node) return null;
        const style = getComputedStyle(node);
        const text = parse(style.color);
        const layer = parse(style.backgroundColor);
        const page = parse(getComputedStyle(document.body).backgroundColor);
        if (!text || !layer || !page) return null;
        return ratio(text, over(layer, page));
      };
      const root = document.documentElement;
      const result = {};
      for (const theme of ['light', 'dark']) {
        root.dataset.theme = theme;
        result[theme] = {
          ready: contrastFor('.wb-todo-badge:not(.running)'),
          running: contrastFor('.wb-todo-badge.running'),
          receipt: contrastOnPage('.wb-todo-receipt'),
        };
      }
      root.dataset.theme = 'dark';
      return result;
    })()`);
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
    await cdp.send("Emulation.clearDeviceMetricsOverride");

    // ---- 10. external create appears through polling while focused ----
    await evaluate("document.querySelector('.wb-todo-composer input').focus()");
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
      "document.activeElement === document.querySelector('.wb-todo-composer input')",
    );
    check("外部建卡 6s 内轮询出现且不抢输入焦点", focusHeld === true);

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

    // ---- 12. composer bucket toggle reaches 长期 ----------------------
    await evaluate(`(async () => {
      window.__wbProbe.create = 0;
      const buttons = [...document.querySelectorAll('.wb-todo-bucket')];
      buttons.find((node) => node.textContent.trim() === '长期').click();
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const input = document.querySelector('.wb-todo-composer input');
      input.focus();
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '探针长期卡');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 30));
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
      return true;
    })()`);
    const longToggleOn = await evaluate(
      `[...document.querySelectorAll('.wb-todo-bucket')].find((node) => node.textContent.trim() === '长期').classList.contains('on')`,
    );
    check("切到长期并高亮", longToggleOn === true);
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
      "composer 建卡落长期 bucket",
      longTask?.status === "triage" && longMeta?.bucket === "long",
      JSON.stringify({ status: longTask?.status, metadata: longMeta }),
    );

    // ---- 13. tab switching keeps TodoPage draft state ----------------
    await evaluate(`(() => {
      const input = document.querySelector('.wb-todo-composer input');
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, '跨 tab 草稿');
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`);
    const tabLabels = await evaluate(
      "[...document.querySelectorAll('.wb-tab')].map((node) => node.textContent)",
    );
    check(
      "待办 / 看板两个 tab",
      JSON.stringify(tabLabels) === JSON.stringify(["待办", "看板"]),
      JSON.stringify(tabLabels),
    );
    await evaluate("document.querySelectorAll('.wb-tab')[1].click()");
    await new Promise((resolveWait) => setTimeout(resolveWait, 150));
    const flowView = await evaluate(`(() => {
      const panes = [...document.querySelectorAll('.wb-pane')];
      return {
        displays: panes.map((node) => getComputedStyle(node).display),
        columns: document.querySelectorAll('.wb-board .wb-col').length,
      };
    })()`);
    check("看板 8 列正常", flowView.columns === 8, String(flowView.columns));
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
        draft: document.querySelector('.wb-todo-composer input').value,
      };
    })()`);
    check(
      "切回待办保留草稿",
      todoView.displays.length === 2 &&
        todoView.displays[0] === "flex" &&
        todoView.displays[1] === "none" &&
        todoView.draft === "跨 tab 草稿",
      JSON.stringify(todoView),
    );
    check("待办三列仍在", todoView.columns === 3, String(todoView.columns));

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

    // ---- 17. plugin host: ready drop blocked, other columns work ------
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
    }))()`);
    check(
      "插件宿主只注册看板且无待办",
      pluginHostState.registered === "workbench" &&
        JSON.stringify(pluginHostState.tabs) === JSON.stringify(["看板"]) &&
        pluginHostState.todoMounted === false,
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
