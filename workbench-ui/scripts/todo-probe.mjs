#!/usr/bin/env node
/**
 * Headless probe for Story 1.3 (todo three-zone).
 *
 * Boots an isolated workbench server (throwaway HERMES_KANBAN_HOME /
 * WORKBENCH_DB), seeds cards through the public REST surfaces, then drives
 * the standalone UI in headless Chrome over CDP. No npm dependencies: Node 22
 * ships a global WebSocket and fetch.
 *
 * Covered matrix rows:
 *   - three column order / counts == rendered cards
 *   - repository card shows only title + acceptance, no status decorations
 *   - empty acceptance drop: column highlight, 300ms shake, red hint, 0 request
 *   - acceptance inline edit writes through to workbench.db
 *   - dispatch with acceptance leaves the repository and reaches ready
 *   - new card lands in today, triage, empty acceptance, not auto-dispatched
 *   - 409 parent: toast「已转为 todo，等 parent 完成」and card leaves repository
 *
 * Usage: node scripts/todo-probe.mjs
 *   CHROME_BIN, WORKBENCH_PYTHON override auto-discovery.
 */
import { spawn } from "node:child_process";
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

  try {
    server = spawn(python, ["-I", serverPath, "--host", "127.0.0.1", "--port", String(port)], {
      env: {
        ...process.env,
        HERMES_KANBAN_HOME: join(tmp, "kanban-home"),
        WORKBENCH_DB: join(tmp, "workbench.db"),
        WORKBENCH_DEFAULTS: defaults,
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
      "独立版产物包含待办三区",
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

    await waitForEval(
      "document.querySelectorAll('.wb-todo-col').length === 3 && document.querySelectorAll('.wb-todo-repo-card').length === 4",
      15000,
      "待办三列与仓库卡渲染",
    );

    // Instrument the workbench calls made after this point.
    await evaluate(`(() => {
      window.__wbProbe = { dispatch: 0, create: 0, metadata: 0 };
      const original = window.fetch.bind(window);
      window.fetch = (input, init) => {
        const url = typeof input === "string" ? input : (input && input.url) || "";
        if (url.includes("/api/workbench/dispatch/")) window.__wbProbe.dispatch += 1;
        else if (/\\/api\\/workbench\\/metadata\\/[^?]+$/.test(url)) window.__wbProbe.metadata += 1;
        else if (url.endsWith("/api/workbench/tasks")) window.__wbProbe.create += 1;
        return original(input, init);
      };
      return true;
    })()`);

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
      "[...document.querySelectorAll('.wb-todo-seghead span:first-child')].map((node) => node.textContent)",
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

    // ---- 5. dispatch with acceptance ---------------------------------
    await evaluate(`(async () => {
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('空验收卡'));
      const flow = document.querySelector('.wb-todo-flow');
      const dataTransfer = new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
      flow.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      flow.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
      return true;
    })()`);
    await waitForEval("window.__wbProbe.dispatch === 1", 5000, "dispatch 请求");
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
    check("派发成功 toast「已派发」", dispatched.toast === "已派发", dispatched.toast);
    const emptyStatus = (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${emptyCard}`)).data?.task?.status;
    check("派发后上游状态 ready", emptyStatus === "ready", String(emptyStatus));

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

    // ---- 8. 409 parent: toast + card leaves repository ---------------
    await evaluate(`(async () => {
      window.__wbProbe.dispatch = 0;
      const card = [...document.querySelectorAll('.wb-todo-repo-card')]
        .find((node) => node.textContent.includes('等 parent 卡'));
      const flow = document.querySelector('.wb-todo-flow');
      const dataTransfer = new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer }));
      flow.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer }));
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      flow.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer }));
      return true;
    })()`);
    await waitForEval("window.__wbProbe.dispatch === 1", 5000, "parent 派发请求");
    await waitForEval(
      `(document.querySelector('.wb-toast')?.textContent || '') === '已转为 todo，等 parent 完成'`,
      8000,
      "409 toast",
    );
    await waitForEval(
      `![...document.querySelectorAll('.wb-todo-repo-card')]
        .some((node) => node.textContent.includes('等 parent 卡'))`,
      8000,
      "停卡离开仓库",
    );
    const blockedStatus = (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${blockedChild}`)).data?.task?.status;
    check("有未完成 parent 的卡落 todo", blockedStatus === "todo", String(blockedStatus));

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