#!/usr/bin/env node
/**
 * Headless probe for Story 2.1 (full-screen verification, hardened).
 *
 * Boots an isolated workbench server (throwaway HERMES_KANBAN_HOME /
 * HERMES_KANBAN_DB / WORKBENCH_DB), seeds review cards through the public REST
 * surfaces, then drives the standalone UI in headless Chrome over CDP.
 *
 * Covered:
 *   - count-only header, full-screen open, focus/trap, 1/N, countdown tokens
 *   - text + image attachments, result/summary/run info, 2.3 slot
 *   - key repeat ignored; A accept; R reason rules (Esc, typing a/r/s,
 *     Ctrl+Enter); S skip with a remaining card; 5s poll does not re-add skip
 *   - batch stats, ~3s auto-exit, appended card during stats/poll
 *   - row entry, no-acceptance warning, external not_in_review refresh
 *   - plugin host has no verify entry
 *
 * Usage: node scripts/verify-probe.mjs
 *   CHROME_BIN, WORKBENCH_PYTHON override auto-discovery.
 */
import { execFileSync, spawn } from "node:child_process";
import {
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

/** 1x1 transparent PNG. */
const PNG_PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZlS8AAAAASUVORK5CYII=",
  "base64",
);

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
  const tmp = mkdtempSync(join(tmpdir(), "wb-verify-probe-"));
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
  };
  const setReviewEventAge = (taskId, ageSeconds) =>
    updateKanban(
      "UPDATE task_events SET created_at=? WHERE task_id=? AND kind='review_requested'",
      taskId,
      Math.floor(Date.now() / 1000) - ageSeconds,
    );

  try {
    server = spawn(python, ["-I", serverPath, "--host", "127.0.0.1", "--port", String(port)], {
      env: {
        ...process.env,
        HERMES_KANBAN_HOME: join(tmp, "kanban-home"),
        HERMES_KANBAN_DB: join(tmp, "kanban-home", "kanban.db"),
        WORKBENCH_DB: join(tmp, "workbench.db"),
        WORKBENCH_DEFAULTS: defaults,
        // 48h/backoff behavior is covered by test_verify.py; keep the probe
        // deterministic and free of background writes.
        WORKBENCH_REVIEW_SWEEP_SECONDS: "0",
        WORKBENCH_INBOX_CACHE_SECONDS: "0",
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
      "独立版产物包含验收界面",
      bundle.includes("wb-verify-overlay") && bundle.includes("wb-review-entry"),
      "先运行 npm run build",
    );

    // ---- seed review cards through public REST ------------------------
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
    const uploadAttachment = async (taskId, filename, content, contentType) => {
      const form = new FormData();
      form.append("file", new Blob([content], { type: contentType }), filename);
      const response = await fetch(
        `${base}/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/attachments`,
        { method: "POST", body: form },
      );
      if (response.status !== 200) {
        throw new Error(`上传附件失败 ${taskId}: ${response.status} ${await response.text()}`);
      }
    };
    const seedReview = async (title, accept, input) => {
      const card = await createUpstream(title);
      await setMetadata(card, {
        bucket: "today",
        contract: { accept, input, ready: false },
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

    const titleA = "验收探针甲";
    const titleB = "验收探针乙";
    const titleC = "验收探针丙";
    const titleD = "验收探针丁";
    const cardA = await seedReview(titleA, "甲的标准：跑通测试", "甲的输入");
    await uploadAttachment(cardA, "probe-note.txt", "probe attachment", "text/plain");
    await uploadAttachment(cardA, "pixel.png", PNG_PIXEL, "image/png");
    updateKanban("UPDATE tasks SET result=? WHERE id=?", cardA, "实际产出文本");
    const cardB = await seedReview(titleB, "乙的标准：交付文档", "乙的输入");
    const cardC = await seedReview(titleC, "丙的标准：人工确认", "丙的输入");
    const cardD = await seedReview(titleD, "丁的标准：抽查", "丁的输入");
    // Deterministic oldest-first order: 甲 > 乙 > 丙 > 丁.
    setReviewEventAge(cardA, 400);
    setReviewEventAge(cardB, 300);
    setReviewEventAge(cardC, 200);
    setReviewEventAge(cardD, 100);

    const taskStatus = async (taskId) =>
      (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`))
        .data?.task?.status;
    const taskDetail = async (taskId) =>
      (await httpJson("GET", `${base}/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}`))
        .data;

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
    const click = (selector) =>
      evaluate(`(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return false;
        node.click();
        return true;
      })()`);
    const pressKey = (key, extra = {}) =>
      evaluate(`(() => {
        window.dispatchEvent(new KeyboardEvent('keydown', {
          key: ${JSON.stringify(key)}, bubbles: true, cancelable: true,
          ...${JSON.stringify(extra)},
        }));
        return true;
      })()`);
    const entryCount = () =>
      evaluate("document.querySelector('.wb-review-entry-count')?.textContent || ''");
    const verifyCount = () =>
      evaluate("document.querySelector('.wb-verify-count')?.textContent || ''");
    const verifyTitle = () =>
      evaluate("document.querySelector('.wb-verify-title')?.textContent || ''");
    const overlayOpen = () =>
      evaluate("Boolean(document.querySelector('.wb-verify-overlay'))");
    const typeReason = (text) =>
      evaluate(`(() => {
        const area = document.querySelector('#wb-verify-reason-input');
        if (!area) return false;
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value'
        ).set;
        setter.call(area, ${JSON.stringify(text)});
        area.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()`);

    // ---- 1. header count + open + focus/layout -----------------------
    await waitFor(
      async () => (await entryCount()) === "4",
      { timeout: 12000, interval: 150, label: "右上角验收计数 4" },
    );
    check("右上角验收计数来自 count 端点（4）", true);

    check("点击验收计数打开覆盖层", await click(".wb-review-entry"));
    await waitForEval("Boolean(document.querySelector('.wb-verify-overlay'))", 8000, "验收覆盖层");
    await waitForEval(
      "Boolean(document.querySelector('.wb-verify-title')) && Boolean(document.querySelector('.wb-verify-figure img'))",
      10000,
      "验收详情加载（含图片附件）",
    );
    await waitFor(
      async () =>
        await evaluate(
          "document.activeElement?.classList.contains('wb-verify-overlay') === true",
        ),
      { timeout: 5000, interval: 100, label: "验收对话框初始焦点" },
    );
    check("打开时焦点落在对话框", true);

    const tabState = await evaluate(`(() => {
      const overlay = document.querySelector('.wb-verify-overlay');
      overlay.focus();
      window.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Tab', bubbles: true, cancelable: true,
      }));
      return {
        inside: Boolean(document.activeElement?.closest('.wb-verify-overlay')),
        tag: document.activeElement?.tagName || '',
      };
    })()`);
    check("Tab 焦点被限制在对话框内", tabState.inside === true, JSON.stringify(tabState));

    const cardState = await evaluate(`(() => ({
      count: document.querySelector('.wb-verify-count')?.textContent || '',
      title: document.querySelector('.wb-verify-title')?.textContent || '',
      standard: document.querySelector('.wb-verify-standard')?.textContent || '',
      texts: [...document.querySelectorAll('.wb-verify-text')].map((node) => node.textContent),
      file: document.querySelector('.wb-verify-file-name')?.textContent || '',
      img: document.querySelector('.wb-verify-figure img')?.getAttribute('src') || '',
      run: Boolean(document.querySelector('.wb-verify-run')),
      runSummary: document.querySelector('.wb-verify-run')?.textContent || '',
      session: document.querySelector('.wb-verify-session-slot')?.textContent || '',
      countdown: document.querySelector('.wb-verify-countdown')?.textContent || '',
      keys: [...document.querySelectorAll('.wb-verify-key')].map((node) => node.textContent.trim()),
    }))()`);
    check("一张一屏：1/N 与最旧卡", cardState.count === "1/4" && cardState.title === titleA, JSON.stringify(cardState));
    check("展示验收标准原文", cardState.standard === "甲的标准：跑通测试", cardState.standard);
    check(
      "展示自述、result、文本附件与图片附件",
      cardState.texts.join("\n").includes(`完成：${titleA}`) &&
        cardState.texts.join("\n").includes("实际产出文本") &&
        cardState.file === "probe-note.txt" &&
        cardState.img.includes("/attachments/"),
      JSON.stringify(cardState),
    );
    check(
      "展示运行信息、自述、倒计时与 2.3 占位",
      cardState.run && cardState.runSummary.includes("完成") &&
        cardState.session.includes("2.3") &&
        cardState.countdown.includes("自动收") &&
        !cardState.countdown.includes("48h"),
      JSON.stringify(cardState),
    );
    check(
      "底部键位 A/R/S",
      JSON.stringify(cardState.keys) === JSON.stringify(["A 收", "R 不收", "S 跳过"]),
      JSON.stringify(cardState.keys),
    );

    const verifyTheme = await evaluate(`(async () => {
      const root = document.documentElement;
      const overlay = document.querySelector('.wb-verify-overlay');
      const title = document.querySelector('.wb-verify-title');
      root.dataset.theme = 'light';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      const light = getComputedStyle(overlay).backgroundColor;
      const lightFg = getComputedStyle(title).color;
      root.dataset.theme = 'dark';
      await new Promise((resolveWait) => setTimeout(resolveWait, 50));
      return {
        light,
        dark: getComputedStyle(overlay).backgroundColor,
        lightFg,
        darkFg: getComputedStyle(title).color,
      };
    })()`);
    check(
      "验收页明暗主题走 token",
      verifyTheme.light !== verifyTheme.dark && verifyTheme.lightFg !== verifyTheme.darkFg,
      JSON.stringify(verifyTheme),
    );

    // Key repeat must not accept twice / at all.
    await pressKey("a", { repeat: true });
    await new Promise((resolveWait) => setTimeout(resolveWait, 350));
    check(
      "重复按键不触发验收",
      (await verifyTitle()) === titleA && (await verifyCount()) === "1/4",
      JSON.stringify({ title: await verifyTitle(), count: await verifyCount() }),
    );

    // ---- 2. A accept --------------------------------------------------
    await pressKey("a");
    await waitFor(
      async () => (await verifyCount()) === "2/4",
      { timeout: 8000, interval: 100, label: "A 收后队列前进" },
    );
    const acceptedDetail = await taskDetail(cardA);
    check(
      "A 收 → done + summary 验收通过",
      acceptedDetail?.task?.status === "done" &&
        acceptedDetail?.task?.latest_summary === "验收通过",
      JSON.stringify({ status: acceptedDetail?.task?.status, summary: acceptedDetail?.task?.latest_summary }),
    );

    // ---- 3. R reason panel: Esc, typing a/r/s, Ctrl+Enter -------------
    await pressKey("r");
    await waitForEval("Boolean(document.querySelector('#wb-verify-reason-input'))", 5000, "理由输入");
    const emptyDisabled = await evaluate(
      "document.querySelector('.wb-verify-reason-actions .wb-verify-primary')?.disabled",
    );
    check("空理由时不能提交", emptyDisabled === true, String(emptyDisabled));

    await typeReason("ars");
    await new Promise((resolveWait) => setTimeout(resolveWait, 250));
    const typingState = await evaluate(`(() => ({
      value: document.querySelector('#wb-verify-reason-input')?.value || '',
      count: document.querySelector('.wb-verify-count')?.textContent || '',
      title: document.querySelector('.wb-verify-title')?.textContent || '',
    }))()`);
    check(
      "在理由框里输入 a/r/s 不触发验收动作",
      typingState.value === "ars" &&
        typingState.count === "2/4" &&
        typingState.title === titleB &&
        (await taskStatus(cardB)) === "review",
      JSON.stringify(typingState),
    );

    await pressKey("Escape");
    await waitForEval("!Boolean(document.querySelector('.wb-verify-overlay'))", 5000, "理由面板 Esc 退出");
    await waitFor(
      async () => (await entryCount()) === "3",
      { timeout: 8000, interval: 150, label: "退出后计数 3" },
    );
    check("理由面板按 Esc 退出验收", true);

    await click(".wb-review-entry");
    await waitForEval("Boolean(document.querySelector('.wb-verify-overlay'))", 8000, "重新打开覆盖层");
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(titleB)}`,
      10000,
      "重新打开后回到最旧卡 B",
    );
    check("重新打开回到最旧卡", (await verifyCount()) === "1/3");

    await pressKey("r");
    await waitForEval("Boolean(document.querySelector('#wb-verify-reason-input'))", 5000, "理由输入 B");
    const reason = "请补测试再交";
    await typeReason(reason);
    await evaluate(`(() => {
      const area = document.querySelector('#wb-verify-reason-input');
      area.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true,
      }));
      return true;
    })()`);
    await waitFor(
      async () => (await verifyCount()) === "2/3",
      { timeout: 8000, interval: 100, label: "Ctrl+Enter 提交后队列前进" },
    );
    const rejectedDetail = await taskDetail(cardB);
    const rejectedComment = (rejectedDetail?.comments || []).slice(-1)[0];
    check(
      "Ctrl+Enter 提交不收 → comment + triage",
      rejectedDetail?.task?.status === "triage" &&
        rejectedComment?.author === "workbench" &&
        rejectedComment?.body === reason,
      JSON.stringify({ status: rejectedDetail?.task?.status, comment: rejectedComment?.body }),
    );

    // ---- 4. S skip with a remaining card + 5s poll -------------------
    await pressKey("s");
    await waitFor(
      async () => (await verifyCount()) === "3/3",
      { timeout: 8000, interval: 100, label: "S 跳过后队列前进" },
    );
    check("S 跳过后下一张仍在前台", (await verifyTitle()) === titleD);
    await new Promise((resolveWait) => setTimeout(resolveWait, 5500));
    const afterPoll = await evaluate(`(() => ({
      count: document.querySelector('.wb-verify-count')?.textContent || '',
      title: document.querySelector('.wb-verify-title')?.textContent || '',
      cards: [...document.querySelectorAll('.wb-verify-title')].length,
    }))()`);
    check(
      "5s 轮询不把跳过的卡重新加回",
      afterPoll.count === "3/3" && afterPoll.title === titleD,
      JSON.stringify(afterPoll),
    );
    check(
      "跳过的卡上游仍是 review",
      (await taskStatus(cardC)) === "review",
    );

    // ---- 5. last card accepted → stats + auto-exit -------------------
    await pressKey("a");
    await waitForEval("Boolean(document.querySelector('.wb-verify-finish-title'))", 8000, "本批统计");
    const stats = await evaluate(
      "[...document.querySelectorAll('.wb-verify-stat b')].map((node) => node.textContent)",
    );
    check(
      "本批统计 accepted/rejected/skipped = 1/1/1",
      JSON.stringify(stats) === JSON.stringify(["1", "1", "1"]),
      JSON.stringify(stats),
    );
    await waitFor(
      async () => !(await overlayOpen()),
      { timeout: 6000, interval: 150, label: "统计页 3s 自动退出" },
    );
    check("统计页约 3s 后自动退出", true);
    await waitFor(
      async () => (await entryCount()) === "1",
      { timeout: 8000, interval: 150, label: "自动退出后计数 1" },
    );
    check("自动退出后待收计数刷新为 1", true);

    // ---- 6. row entry + no-acceptance warning + appended card --------
    await setMetadata(cardC, { contract: { accept: "" } });
    await waitForEval(
      `[...document.querySelectorAll('.wb-todo-flow-card.clickable')]
        .some((node) => node.textContent.includes(${JSON.stringify(titleC)}))`,
      12000,
      "流水线待收行渲染",
    );
    const rowOpened = await evaluate(`(() => {
      const card = [...document.querySelectorAll('.wb-todo-flow-card.clickable')]
        .find((node) => node.textContent.includes(${JSON.stringify(titleC)}));
      if (!card) return false;
      card.click();
      return true;
    })()`);
    check("点击待收行进入验收", rowOpened === true);
    await waitForEval("Boolean(document.querySelector('.wb-verify-overlay'))", 8000, "行入口覆盖层");
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(titleC)}`,
      10000,
      "行入口展示该卡",
    );
    await waitForEval(
      "Boolean([...document.querySelectorAll('.wb-verify-note.warn')].find((n) => n.textContent.includes('没有写验收标准')))",
      5000,
      "无验收标准警示",
    );
    check("无验收标准时显示警示", (await verifyCount()) === "1/1");

    // A new review card appears while the overlay is open: the 5s poll must
    // append it to the queue and keep the countdown working.
    const titleE = "追加验收卡";
    const cardE = await seedReview(titleE, "追加标准", "追加输入");
    await waitFor(
      async () => (await verifyCount()) === "1/2",
      { timeout: 10000, interval: 200, label: "轮询追加新卡" },
    );
    const appended = await evaluate(`(() => ({
      count: document.querySelector('.wb-verify-count')?.textContent || '',
      countdown: document.querySelector('.wb-verify-countdown')?.textContent || '',
      title: document.querySelector('.wb-verify-title')?.textContent || '',
    }))()`);
    check(
      "轮询追加新卡且倒计时仍来自后端 TTL",
      appended.count === "1/2" &&
        appended.title === titleC &&
        appended.countdown.includes("自动收") &&
        !appended.countdown.includes("48h"),
      JSON.stringify(appended),
    );

    // External status change while the card is on screen: verify must 409,
    // remove it locally, and surface the next card. Pause page intervals so
    // the 5s sync cannot remove the card before the A keydown is handled.
    await evaluate("for (let i = 1; i < 20000; i += 1) { clearInterval(i); } true");
    const triageState = await evaluate(`(async () => {
      const response = await fetch(${JSON.stringify(
        `${base}/api/plugins/kanban/tasks/${encodeURIComponent(cardC)}`,
      )}, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'triage' }),
      });
      const titleNow = document.querySelector('.wb-verify-title')?.textContent || '';
      if (titleNow !== ${JSON.stringify(titleC)}) {
        return { dispatched: false, titleNow, status: response.status };
      }
      window.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'a', bubbles: true, cancelable: true,
      }));
      return { dispatched: true, titleNow, status: response.status };
    })()`);
    await waitForEval(
      `document.querySelector('.wb-verify-title')?.textContent === ${JSON.stringify(titleE)}`,
      10000,
      "外部状态变化后前进到下一张",
    );
    check(
      "外部改状态后 A 收 409 并移到下一张",
      triageState.dispatched === true && (await taskStatus(cardC)) === "triage",
      JSON.stringify(triageState),
    );

    // ---- 7. accept the appended card → stats + auto-exit -------------
    await pressKey("a");
    await waitForEval("Boolean(document.querySelector('.wb-verify-finish-title'))", 8000, "追加卡统计");
    const finalStats = await evaluate(
      "[...document.querySelectorAll('.wb-verify-stat b')].map((node) => node.textContent)",
    );
    check(
      "追加卡验收统计 accepted=1",
      JSON.stringify(finalStats) === JSON.stringify(["1", "0", "0"]),
      JSON.stringify(finalStats),
    );
    await waitFor(
      async () => !(await overlayOpen()),
      { timeout: 6000, interval: 150, label: "第二批自动退出" },
    );
    await waitFor(
      async () => (await entryCount()) === "0",
      { timeout: 10000, interval: 150, label: "清空后计数 0" },
    );
    check("第二批清空并自动退出", (await taskStatus(cardE)) === "done");

    // ---- 8. plugin host: board only, no verify entry ------------------
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
    const pluginState = await evaluate(`(() => ({
      registered: window.__wbPlugin?.name || '',
      tabs: [...document.querySelectorAll('.wb-tab')].map((node) => node.textContent),
      reviewEntry: Boolean(document.querySelector('.wb-review-entry')),
      verifyOverlay: Boolean(document.querySelector('.wb-verify-overlay')),
      todoMounted: Boolean(document.querySelector('.wb-todo-wrap')),
    }))()`);
    check(
      "插件宿主只有看板，无验收入口",
      pluginState.registered === "workbench" &&
        JSON.stringify(pluginState.tabs) === JSON.stringify(["看板"]) &&
        pluginState.reviewEntry === false &&
        pluginState.verifyOverlay === false &&
        pluginState.todoMounted === false,
      JSON.stringify(pluginState),
    );

    console.log(`\nVERIFY PROBE OK — ${checks.length} checks passed`);
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