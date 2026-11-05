#!/usr/bin/env node
/**
 * Headless probe for Story 2.3 (session trace entry, hardened).
 *
 * Boots an isolated workbench server with a throwaway state.db, seeds a done
 * card + task-fallback card + review cards (with session / without session /
 * with a missing session), then drives the standalone UI in headless Chrome.
 *
 * Covered:
 *   - finished card "会话" entry + task.session_id "发起会话" fallback
 *   - session metadata (ended/duration/running), origin labels
 *   - id-ordered window (regressed timestamps), hidden/rewound/dedupe filtering
 *   - "显示 X / 共 Y" + truncated hint, 参数已折叠, no tool-argument secrets
 *   - scroll to newest, body scroll lock + inert background
 *   - Esc close/focus restore, Tab trap, light/dark tokens
 *   - 503 session_unavailable + retry (state.db temporarily renamed)
 *   - A/R/S while the drawer is open does not act on the card behind it
 *   - no_session and session_missing drawers (retry only for retryable errors)
 *   - plugin host has no session entry (non-vacuous board check)
 *
 * Usage: node scripts/session-probe.mjs
 *   CHROME_BIN, WORKBENCH_PYTHON override auto-discovery.
 */
import { execFileSync, spawn } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  renameSync,
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
const MESSAGE_LIMIT = 3;
const INSIDE_SECRET = "SECRET_INSIDE_PREFIX";
const AFTER_SECRET = "SECRET_AFTER_240_CHARS";

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
  const tmp = mkdtempSync(join(tmpdir(), "wb-session-probe-"));
  const defaults = join(tmp, "defaults.json");
  writeFileSync(defaults, readFileSync(defaultsPath));
  const stateDb = join(tmp, "state.db");
  const stateDbAside = join(tmp, "state.db.aside");
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

  const runPython = (code, args) =>
    execFileSync(python, ["-I", "-c", code, ...args], { encoding: "utf8" });

  runPython(
    `import sqlite3,sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("""
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, system_prompt TEXT,
  parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL,
  message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0, profile_name TEXT, title TEXT
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_name TEXT,
  timestamp REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
  compacted INTEGER NOT NULL DEFAULT 0, display_order INTEGER,
  display_kind TEXT, display_identity BLOB,
  _compressed_summary INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_session ON messages(session_id, id);
""")
conn.commit()
conn.close()`,
    [stateDb],
  );

  const addSession = (sessionId, messages, { endedAt = 1060, messageCount } = {}) =>
    runPython(
      `import json,sqlite3,sys
path, sid, raw = sys.argv[1], sys.argv[2], sys.argv[3]
ended = None if sys.argv[4] == "" else float(sys.argv[4])
count = int(sys.argv[5]) if sys.argv[5] else len(json.loads(raw))
messages = json.loads(raw)
conn = sqlite3.connect(path)
conn.execute(
  "INSERT INTO sessions (id, source, model, started_at, ended_at, message_count, "
  "input_tokens, output_tokens, profile_name, title) VALUES (?,?,?,?,?,?,?,?,?,?)",
  (sid, "cli", "test-model", 1000.0, ended, count, 120, 45, "probe", "探针会话 " + sid))
for message in messages:
    calls = message.get("tool_calls")
    identity = message.get("display_identity")
    conn.execute(
      "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, timestamp, "
      "active, compacted, display_order, display_kind, display_identity, _compressed_summary) "
      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
      (sid, message.get("role", "user"), message.get("content"),
       json.dumps(calls) if calls else None, message.get("tool_name"),
       message.get("timestamp", 1000.0), message.get("active", 1),
       message.get("compacted", 0), message.get("display_order"),
       message.get("display_kind"), identity,
       message.get("_compressed_summary", 0)))
conn.commit()
conn.close()`,
      [
        stateDb,
        sessionId,
        JSON.stringify(messages),
        endedAt === null ? "" : String(endedAt),
        messageCount === undefined ? "" : String(messageCount),
      ],
    );

  const addRunMetadata = (taskId, metadata, markDone = false) =>
    runPython(
      `import json,sqlite3,sys,time
path, task, done, raw = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4]
conn = sqlite3.connect(path)
now = int(time.time())
cur = conn.execute(
  "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, metadata) "
  "VALUES (?,?,?,?,?,?,?)",
  (task, "probe", "done" if done else "running", now - 60, now if done else None,
   "completed" if done else None, raw))
if done:
    conn.execute("UPDATE tasks SET status='done', completed_at=?, current_run_id=? WHERE id=?",
                 (now, cur.lastrowid, task))
conn.commit()
conn.close()`,
      [kanbanDb, taskId, markDone ? "1" : "0", JSON.stringify(metadata)],
    );

  const setTaskSession = (taskId, sessionId) =>
    runPython(
      `import sqlite3,sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("UPDATE tasks SET session_id=? WHERE id=?", (sys.argv[3], sys.argv[2])).rowcount
conn.commit()
conn.close()
print(rows)`,
      [kanbanDb, taskId, sessionId],
    );

  const setReviewEventAge = (taskId, ageSeconds) => {
    const output = runPython(
      `import sqlite3,sys,time
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='review_requested'",
                    (int(time.time()) - int(sys.argv[3]), sys.argv[2])).rowcount
conn.commit()
conn.close()
print(rows)`,
      [kanbanDb, taskId, String(ageSeconds)],
    );
    const rows = Number(String(output).trim());
    if (rows !== 1) {
      throw new Error(`review_requested 应命中 1 行，实际 ${rows} 行：${taskId}`);
    }
  };

  try {
    server = spawn(
      python,
      ["-I", serverPath, "--host", "127.0.0.1", "--port", String(port)],
      {
        env: {
          ...process.env,
          HERMES_KANBAN_HOME: join(tmp, "kanban-home"),
          HERMES_KANBAN_DB: kanbanDb,
          WORKBENCH_DB: join(tmp, "workbench.db"),
          WORKBENCH_DEFAULTS: defaults,
          WORKBENCH_STATE_DB: stateDb,
          WORKBENCH_SESSION_MESSAGE_LIMIT: String(MESSAGE_LIMIT),
          WORKBENCH_REVIEW_SWEEP_SECONDS: "0",
          WORKBENCH_INBOX_CACHE_SECONDS: "0",
          WORKBENCH_NOTIFY_SWEEP_SECONDS: "0",
          WORKBENCH_NOTIFY_COMMAND: "true",
        },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
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
      "独立版产物包含会话抽屉",
      bundle.includes("wb-session-drawer") && bundle.includes("wb-card-session"),
      "先运行 npm run build",
    );

    // ---- seed cards ---------------------------------------------------
    const createUpstream = async (title) => {
      const result = await httpJson("POST", `${base}/api/plugins/kanban/tasks`, {
        title,
        body: "probe",
        triage: true,
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
    const seedReview = async (title, accept) => {
      const card = await createUpstream(title);
      await setMetadata(card, {
        bucket: "today",
        contract: { accept, input: "probe", ready: false },
      });
      const dispatched = await httpJson(
        "POST",
        `${base}/api/workbench/dispatch/${encodeURIComponent(card)}`,
        {},
      );
      if (dispatched.status !== 200) {
        throw new Error(`派发失败 ${title}: ${dispatched.status} ${JSON.stringify(dispatched.data)}`);
      }
      const reviewed = await httpJson(
        "PATCH",
        `${base}/api/plugins/kanban/tasks/${encodeURIComponent(card)}`,
        { status: "review", summary: `完成：${title}` },
      );
      if (reviewed.status !== 200) {
        throw new Error(`转 review 失败 ${title}: ${reviewed.status} ${JSON.stringify(reviewed.data)}`);
      }
      return card;
    };
    const taskStatus = async (taskId) =>
      (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`))
        .data?.task?.status;

    const doneTitle = "已完成会话卡";
    const doneCard = await createUpstream(doneTitle);
    addRunMetadata(
      doneCard,
      { worker_session_id: "sess-done", executor_session_id: "exec-resume-1" },
      true,
    );
    const longConclusion = "结论：" + "长".repeat(4000);
    const toolArguments = JSON.stringify({
      command: INSIDE_SECRET,
      padding: "x".repeat(600),
      token: AFTER_SECRET,
    });
    addSession("sess-done", [
      { role: "user", content: "第一条：开始", timestamp: 5000 },
      { role: "assistant", content: "第二条：读代码", timestamp: 1000 },
      { role: "assistant", content: "隐藏行", display_kind: "hidden", timestamp: 4000 },
      {
        role: "assistant",
        content: "重复旧稿",
        display_identity: "turn-dup",
        timestamp: 3500,
      },
      { role: "tool", content: "中间完成", tool_name: "kanban_complete", timestamp: 3000 },
      {
        role: "assistant",
        content: "重复终稿",
        display_identity: "turn-dup",
        timestamp: 2500,
      },
      { role: "user", content: "rewound", active: 0, compacted: 0, timestamp: 2200 },
      {
        role: "assistant",
        content: "",
        tool_calls: [
          {
            id: "call_1",
            type: "function",
            function: { name: "kanban_complete", arguments: toolArguments },
          },
        ],
        timestamp: 1000,
      },
      { role: "assistant", content: longConclusion, timestamp: 500 },
    ]);

    const taskTitle = "发起会话完成卡";
    const taskCard = await createUpstream(taskTitle);
    addRunMetadata(taskCard, {}, true);
    const taskSessionRows = setTaskSession(taskCard, "sess-task");
    if (Number(String(taskSessionRows).trim()) !== 1) {
      throw new Error(`tasks.session_id 应命中 1 行：${taskSessionRows}`);
    }
    addSession("sess-task", [{ role: "user", content: "发起会话消息", timestamp: 900 }]);

    const reviewWith = "有会话待验收";
    const reviewWithout = "无会话待验收";
    const reviewMissing = "会话缺失待验收";
    const cardR1 = await seedReview(reviewWith, "甲：跑通");
    addRunMetadata(cardR1, { worker_session_id: "sess-review" }, false);
    addSession(
      "sess-review",
      [
        { role: "user", content: "验收里的会话问题" },
        { role: "assistant", content: "验收里的会话回答" },
      ],
      { endedAt: null },
    );
    const cardR2 = await seedReview(reviewWithout, "乙：人工确认");
    const cardR3 = await seedReview(reviewMissing, "丙：补会话");
    addRunMetadata(cardR3, { worker_session_id: "sess-not-in-db" }, false);
    setReviewEventAge(cardR1, 500);
    setReviewEventAge(cardR2, 300);
    setReviewEventAge(cardR3, 100);

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
          page = targets.find((target) => target.type === "page" && target.url.startsWith(base));
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
    const pressKey = (key, extra = {}) =>
      evaluate(`(() => {
        window.dispatchEvent(new KeyboardEvent('keydown', {
          key: ${JSON.stringify(key)}, bubbles: true, cancelable: true,
          ...${JSON.stringify(extra)},
        }));
        return true;
      })()`);
    const drawerOpen = () => evaluate("Boolean(document.querySelector('.wb-session-drawer'))");
    const overlayOpen = () => evaluate("Boolean(document.querySelector('.wb-verify-overlay'))");
    const openSessionFor = async (taskId) => {
      await evaluate(`(() => {
        const button = document.querySelector(
          '.wb-pane:not(.wb-pane-hidden) .wb-card-session[data-task-id=${JSON.stringify(taskId)}]');
        if (!button) return false;
        button.focus();
        button.click();
        return true;
      })()`);
      await waitForEval("Boolean(document.querySelector('.wb-session-drawer'))", 8000, "会话抽屉");
    };

    // ---- 1. board entries + done session ------------------------------
    await waitForEval("document.querySelectorAll('.wb-tab').length > 0", 20000, "工作台外壳渲染");
    const switched = await evaluate(`(() => {
      const tab = [...document.querySelectorAll('.wb-tab')].find(
        (node) => node.querySelector('.wb-tab-label')?.textContent.trim() === '看板',
      );
      if (!tab) return false;
      tab.click();
      return true;
    })()`);
    check("切换到看板", switched === true);
    await waitForEval(
      `Boolean(document.querySelector('.wb-pane:not(.wb-pane-hidden) .wb-card-session[data-task-id=${JSON.stringify(doneCard)}]'))`,
      15000,
      "已完成卡会话入口",
    );
    check("已完成卡出现「会话」入口", true);

    await openSessionFor(doneCard);
    await waitFor(
      async () =>
        (await evaluate(
          "document.querySelector('.wb-session-id')?.textContent === 'sess-done'",
        )) === true,
      { timeout: 8000, interval: 100, label: "会话元数据加载" },
    );
    await waitForEval("document.querySelectorAll('.wb-session-msg').length === 3", 8000, "消息窗口");

    const drawerState = await evaluate(`(() => {
      const wrap = document.querySelector('.wb-session-messages-wrap');
      return {
        title: document.querySelector('.wb-session-title')?.textContent || '',
        sessionId: document.querySelector('.wb-session-id')?.textContent || '',
        meta: document.querySelector('.wb-session-meta')?.textContent || '',
        kicker: document.querySelector('.wb-session-kicker')?.textContent || '',
        origin: document.querySelector('.wb-session-origin')?.textContent || '',
        count: document.querySelector('.wb-session-count')?.textContent || '',
        truncated: document.querySelector('.wb-session-truncated')?.textContent || '',
        messages: [...document.querySelectorAll('.wb-session-msg')].map((node) => node.textContent),
        toolSummary: document.querySelector('.wb-session-tools summary')?.textContent || '',
        toolRows: [...document.querySelectorAll('.wb-session-tools li')].map((node) => node.textContent),
        executor: document.querySelector('.wb-session-executor')?.textContent || '',
        scrollTop: wrap?.scrollTop ?? 0,
        scrollHeight: wrap?.scrollHeight ?? 0,
        clientHeight: wrap?.clientHeight ?? 0,
        bodyOverflow: document.body.style.overflow,
        headInert: document.querySelector('.wb-root > .wb-shell')?.inert === true,
        headAria: document.querySelector('.wb-root > .wb-shell')?.getAttribute('aria-hidden'),
        text: document.querySelector('.wb-session-drawer')?.textContent || '',
      };
    })()`);
    check(
      "抽屉展示会话元数据（已结束/时长）",
      drawerState.title === doneTitle &&
        drawerState.sessionId === "sess-done" &&
        drawerState.meta.includes("test-model") &&
        drawerState.meta.includes("结束") &&
        drawerState.meta.includes("时长") &&
        drawerState.kicker.includes("已结束") &&
        drawerState.origin.includes("worker 会话") &&
        drawerState.origin.includes("最新一轮"),
      JSON.stringify(drawerState),
    );
    check(
      "显示 X / 共 Y、truncated 与过滤后的 3 条",
      drawerState.count.includes("显示 3 / 共 6") &&
        drawerState.truncated.includes("已截断") &&
        drawerState.messages.length === 3,
      JSON.stringify({ count: drawerState.count, truncated: drawerState.truncated }),
    );
    const messageOrder = drawerState.messages.join("\n");
    check(
      "窗口按 id 正序（时间戳回退也不乱）且隐藏/重写行被过滤",
      messageOrder.indexOf("重复终稿") < messageOrder.indexOf("参数已折叠") &&
        messageOrder.indexOf("参数已折叠") < messageOrder.indexOf("结论：") &&
        !messageOrder.includes("隐藏行") &&
        !messageOrder.includes("rewound") &&
        !messageOrder.includes("重复旧稿"),
      JSON.stringify(drawerState.messages.map((text) => text.slice(0, 40))),
    );
    check(
      "tool calls 只显示名称 + 参数已折叠，参数密钥不外泄",
      drawerState.toolSummary.includes("kanban_complete") &&
        drawerState.toolRows.join(" ").includes("参数已折叠") &&
        !drawerState.text.includes(INSIDE_SECRET) &&
        !drawerState.text.includes(AFTER_SECRET),
      JSON.stringify({ summary: drawerState.toolSummary, rows: drawerState.toolRows }),
    );
    check(
      "executor_session_id 只在已解析 run 上展示",
      drawerState.executor.includes("exec-resume-1") &&
        drawerState.executor.includes("续跑属 Epic 3"),
      drawerState.executor,
    );
    check(
      "消息列表滚动到最新（底部）",
      drawerState.scrollTop > 0 &&
        drawerState.scrollTop + drawerState.clientHeight >= drawerState.scrollHeight - 3,
      JSON.stringify({
        top: drawerState.scrollTop,
        height: drawerState.scrollHeight,
        client: drawerState.clientHeight,
      }),
    );
    check(
      "抽屉真模态：锁 body 滚动 + 背景 inert/aria-hidden",
      drawerState.bodyOverflow === "hidden" &&
        drawerState.headInert === true &&
        drawerState.headAria === "true",
      JSON.stringify({ overflow: drawerState.bodyOverflow, inert: drawerState.headInert }),
    );

    const themeState = await evaluate(`(async () => {
      const root = document.documentElement;
      const drawer = document.querySelector('.wb-session-drawer');
      root.dataset.theme = 'light';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const light = getComputedStyle(drawer).backgroundColor;
      root.dataset.theme = 'dark';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      return { light, dark: getComputedStyle(drawer).backgroundColor };
    })()`);
    check("会话抽屉明暗主题走 token", themeState.light !== themeState.dark, JSON.stringify(themeState));

    const trapped = await evaluate(`(() => {
      const drawer = document.querySelector('.wb-session-drawer');
      drawer.querySelector('.wb-session-close').focus();
      window.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Tab', shiftKey: true, bubbles: true, cancelable: true,
      }));
      return Boolean(document.activeElement?.closest('.wb-session-drawer'));
    })()`);
    check("Tab 焦点被限制在抽屉内", trapped === true);

    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "Esc 关闭抽屉");
    const focusRestored = await evaluate(
      `document.activeElement?.getAttribute('data-task-id') === ${JSON.stringify(doneCard)}`,
    );
    check("Esc 关闭后焦点回到入口", focusRestored === true);
    check(
      "关闭后恢复 body 滚动与背景属性",
      (await evaluate("document.body.style.overflow")) === "" &&
        (await evaluate("document.querySelector('.wb-root > .wb-shell')?.inert === false")) === true,
    );

    // ---- 2. task.session_id fallback label ----------------------------
    await openSessionFor(taskCard);
    await waitForEval(
      "document.querySelector('.wb-session-id')?.textContent === 'sess-task'",
      8000,
      "任务兜底会话加载",
    );
    const fallbackOrigin = await evaluate(
      "document.querySelector('.wb-session-origin')?.textContent || ''",
    );
    check(
      "tasks.session_id 回退标注为「发起会话」",
      fallbackOrigin.includes("发起会话") && !fallbackOrigin.includes("最新一轮"),
      fallbackOrigin,
    );
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "兜底抽屉关闭");

    // ---- 3. session_unavailable + retry -------------------------------
    renameSync(stateDb, stateDbAside);
    await openSessionFor(doneCard);
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 10000, "503 错误");
    const unavailable = await evaluate(`(() => ({
      code: document.querySelector('.wb-session-error-code')?.textContent || '',
      text: document.querySelector('.wb-session-error')?.textContent || '',
      retry: Boolean(document.querySelector('.wb-session-error .wb-btn')),
      messages: document.querySelectorAll('.wb-session-msg').length,
    }))()`);
    check(
      "state.db 不可用 → session_unavailable + 重试",
      unavailable.code === "session_unavailable" &&
        unavailable.text.includes("会话库暂时不可读") &&
        unavailable.retry === true &&
        unavailable.messages === 0,
      JSON.stringify(unavailable),
    );
    await evaluate("document.querySelector('.wb-session-error .wb-btn').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 8000, "重试仍 503");
    check("重试在 state.db 恢复前保持报错", (await drawerOpen()) === true);
    renameSync(stateDbAside, stateDb);
    await evaluate("document.querySelector('.wb-session-error .wb-btn').click(); true");
    await waitForEval(
      "document.querySelector('.wb-session-id')?.textContent === 'sess-done'",
      8000,
      "恢复后重试成功",
    );
    check("state.db 恢复后点击重试加载成功", true);
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "503 抽屉关闭");

    // ---- 4. verify page: A/R/S isolation + running + missing ----------
    await evaluate("document.querySelector('.wb-review-entry').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-verify-overlay'))", 8000, "验收覆盖层");
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(reviewWith)}`,
      10000,
      "验收展示有会话卡",
    );
    await evaluate("document.querySelector('.wb-verify-session-open').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-session-drawer'))", 8000, "验收内会话抽屉");
    await waitForEval(
      "document.querySelector('.wb-session-id')?.textContent === 'sess-review'",
      8000,
      "验收内会话加载",
    );
    const runningState = await evaluate(`(() => ({
      kicker: document.querySelector('.wb-session-kicker')?.textContent || '',
      meta: document.querySelector('.wb-session-meta')?.textContent || '',
    }))()`);
    check(
      "运行中的会话显示「进行中」",
      runningState.kicker.includes("进行中") && runningState.meta.includes("进行中"),
      JSON.stringify(runningState),
    );

    await pressKey("a");
    await pressKey("r");
    await pressKey("s");
    await new Promise((resolveWait) => setTimeout(resolveWait, 500));
    const behind = await evaluate(`(() => ({
      overlay: Boolean(document.querySelector('.wb-verify-overlay')),
      title: document.querySelector('.wb-verify-title')?.textContent || '',
      drawer: Boolean(document.querySelector('.wb-session-drawer')),
    }))()`);
    check(
      "抽屉打开时 A/R/S 不作用于背后的验收卡",
      behind.overlay === true &&
        behind.drawer === true &&
        behind.title === reviewWith &&
        (await taskStatus(cardR1)) === "review",
      JSON.stringify(behind),
    );
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "抽屉 Esc");
    check("Esc 只关会话抽屉，验收页仍在", (await overlayOpen()) === true);

    await pressKey("a");
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(reviewWithout)}`,
      10000,
      "验收前进到无会话卡",
    );
    await evaluate("document.querySelector('.wb-verify-session-open').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 8000, "no_session 提示");
    const noSession = await evaluate(`(() => ({
      text: document.querySelector('.wb-session-error')?.textContent || '',
      code: document.querySelector('.wb-session-error-code')?.textContent || '',
      retry: Boolean(document.querySelector('.wb-session-error .wb-btn')),
      messages: document.querySelectorAll('.wb-session-msg').length,
    }))()`);
    check(
      "无会话卡提示 no_session 且不显示空查看器",
      noSession.text.includes("没有执行会话") &&
        noSession.code === "no_session" &&
        noSession.retry === false &&
        noSession.messages === 0,
      JSON.stringify(noSession),
    );
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "无会话抽屉关闭");

    await pressKey("a");
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(reviewMissing)}`,
      10000,
      "验收前进到缺失会话卡",
    );
    await evaluate("document.querySelector('.wb-verify-session-open').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 8000, "session_missing 提示");
    const missing = await evaluate(`(() => ({
      text: document.querySelector('.wb-session-error')?.textContent || '',
      code: document.querySelector('.wb-session-error-code')?.textContent || '',
      retry: Boolean(document.querySelector('.wb-session-error .wb-btn')),
    }))()`);
    check(
      "session_missing 专用提示 + 重试",
      missing.code === "session_missing" &&
        missing.text.includes("state.db 中不存在") &&
        missing.retry === true,
      JSON.stringify(missing),
    );
    await evaluate("document.querySelector('.wb-session-error .wb-btn').click(); true");
    await waitForEval("Boolean(document.querySelector('.wb-session-error'))", 8000, "missing 重试");
    check("session_missing 重试仍保持提示", true);
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-session-drawer'))", 5000, "missing 抽屉关闭");
    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-verify-overlay'))", 5000, "退出验收");
    check("缺失会话不影响验收退出", true);

    // ---- 5. plugin host: no session entry (non-vacuous) ---------------
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
      window.__HERMES_PLUGIN_SDK__ = {
        React: window.React,
        ReactDOM: window.ReactDOM,
        fetchJSON: async (url, init) => (await fetch(url, init)).json(),
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
    await waitForEval(
      `document.body.textContent.includes(${JSON.stringify(doneTitle)})`,
      15000,
      "插件宿主完成卡渲染",
    );
    const pluginState = await evaluate(`(() => ({
      registered: window.__wbPlugin?.name || '',
      hasDoneCard: document.body.textContent.includes(${JSON.stringify(doneTitle)}),
      cardSession: Boolean(document.querySelector('.wb-card-session')),
      verifySession: Boolean(document.querySelector('.wb-verify-session-open')),
      drawer: Boolean(document.querySelector('.wb-session-drawer')),
      todoMounted: Boolean(document.querySelector('.wb-todo-wrap')),
    }))()`);
    check(
      "插件宿主无会话入口（看板与完成卡确已渲染）",
      pluginState.registered === "workbench" &&
        pluginState.hasDoneCard === true &&
        pluginState.cardSession === false &&
        pluginState.verifySession === false &&
        pluginState.drawer === false &&
        pluginState.todoMounted === false,
      JSON.stringify(pluginState),
    );

    console.log(`\nSESSION PROBE OK — ${checks.length} checks passed`);
  } finally {
    if (cdp) cdp.close();
    if (chrome && chrome.exitCode === null) chrome.kill("SIGKILL");
    if (server && server.exitCode === null) server.kill("SIGTERM");
    await new Promise((resolveWait) => setTimeout(resolveWait, 200));
    if (existsSync(stateDbAside)) {
      try {
        renameSync(stateDbAside, stateDb);
      } catch {
        // best effort
      }
    }
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