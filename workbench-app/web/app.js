/**
 * Workbench — Hermes dashboard plugin
 *
 * 待办（今日 / 仓库双栏）+ 看板（7 列流程）+ 已完成（历史）
 * 数据源：内置 kanban 插件的 REST API（/api/plugins/kanban/*），
 * 与 CLI / gateway 共用同一 kanban_db，不直接读写数据库。
 *
 * 「今日」是人的安排标记，kanban 本身没有该字段：
 * 存放在本插件 localStorage（wb.today.v1），按任务 id 关联。
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useMemo, useCallback, useRef } = SDK.hooks;

  const API = "/api/plugins/kanban";
  const STATUS_ORDER = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"];
  const STATUS = {
    triage: { label: "待整理", color: "var(--wb-muted)" },
    todo: { label: "待执行", color: "var(--wb-warn)" },
    scheduled: { label: "已排期", color: "var(--wb-warn)" },
    ready: { label: "就绪", color: "var(--wb-accent)" },
    running: { label: "运行中", color: "var(--wb-ok)" },
    blocked: { label: "待拍板", color: "var(--wb-danger)" },
    review: { label: "待验收", color: "#8b5cf6" },
    done: { label: "已完成", color: "var(--wb-muted)" }
  };

  /* ---------- 内核裁决口径（不要在插件里放宽内核的 WHERE 子句） ---------- */
  // complete_task: UPDATE ... WHERE status IN ('running','ready','blocked','review')
  // → 待整理/待执行/已排期 直接 PATCH done 必然 409。勾选完成前先补一次「就绪」。
  const COMPLETABLE = ["running", "ready", "blocked", "review"];
  // 看板落列的合法来源（与 kanban_db 的落库/入口条件一致），越权落列在客户端就拦住并给中文提示。
  const ENTERABLE = {
    done: ["running", "ready", "blocked", "review"],
    review: ["running", "ready"],
    blocked: ["running", "ready"],
    scheduled: ["todo", "ready", "running", "blocked"]
  };
  const ENTER_HINT = {
    done: "先在待办页勾选完成，或先拖到「就绪」再拖进来",
    review: "「待验收」只能从「就绪 / 运行中」进入",
    blocked: "「待拍板」只能从「就绪 / 运行中」进入",
    scheduled: "「已排期」不能从「待整理」直接进入，先拖到「待执行」或「就绪」"
  };
  function statusLabel(s) { return (STATUS[s] || {}).label || s; }

  /* ---------- storage ---------- */
  const LS_TODAY = "wb.today.v1";
  const LS_REPO = "wb.repoOrder.v1";
  function readLS(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      const val = raw ? JSON.parse(raw) : null;
      return Array.isArray(val) ? val : fallback;
    } catch (e) { return fallback; }
  }
  function writeLS(key, val) {
    try { localStorage.setItem(key, JSON.stringify(val)); } catch (e) { /* ignore */ }
  }

  /* ---------- time ---------- */
  function dayKey(ts) {
    if (!ts) return "earlier";
    const d = new Date(ts * 1000);
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yest = new Date(start.getTime() - 86400000);
    if (d >= start) return "today";
    if (d >= yest) return "yesterday";
    return "earlier";
  }
  function timeLabel(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (dayKey(ts) === "earlier") {
      return String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") + " " + hm;
    }
    return hm;
  }

  /* ---------- api ---------- */
  async function apiBoard() {
    return SDK.fetchJSON(API + "/board");
  }
  async function apiPatch(id, body) {
    return SDK.fetchJSON(API + "/tasks/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }
  async function apiCreate(body) {
    return SDK.fetchJSON(API + "/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  /* ---------- component ---------- */
  function Workbench() {
    const [board, setBoard] = useState(null);
    const [tab, setTab] = useState("plan");
    const [todayIds, setTodayIds] = useState(function () { return readLS(LS_TODAY, []); });
    const [repoOrder, setRepoOrder] = useState(function () { return readLS(LS_REPO, []); });
    const [toast, setToast] = useState(null);
    const [modal, setModal] = useState(false);
    const [form, setForm] = useState({ title: "", prio: 0, note: "", today: false });
    const [cap, setCap] = useState("");
    const [q, setQ] = useState("");
    const [over, setOver] = useState(null); // {id, before}
    const [dropPanel, setDropPanel] = useState(null);
    const [dropCol, setDropCol] = useState(null);
    const [busy, setBusy] = useState(false);
    const [err, setErr] = useState("");
    const dragRef = useRef(null);
    const inFlight = useRef({});   // 勾选完成的去重（同一张卡两步 PATCH 期间连点）
    const toastTimer = useRef(null);

    useEffect(function () { writeLS(LS_TODAY, todayIds); }, [todayIds]);
    useEffect(function () { writeLS(LS_REPO, repoOrder); }, [repoOrder]);

    const showToast = useCallback(function (msg) {
      setToast(msg);
      if (toastTimer.current) clearTimeout(toastTimer.current);
      toastTimer.current = setTimeout(function () { setToast(null); }, 2600);
    }, []);

    const load = useCallback(async function () {
      try {
        const d = await apiBoard();
        setBoard(d);
        setErr("");
      } catch (e) {
        setErr("读取看板失败：" + (e && e.message ? e.message : String(e)));
      }
    }, []);

    useEffect(function () {
      load();
      const t = setInterval(load, 5000);
      return function () { clearInterval(t); };
    }, [load]);

    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") setModal(false); }
      document.addEventListener("keydown", onKey);
      return function () { document.removeEventListener("keydown", onKey); };
    }, []);

    const tasks = useMemo(function () {
      const out = [];
      const cols = (board && board.columns) || [];
      cols.forEach(function (c) {
        (c.tasks || []).forEach(function (t) { out.push(t); });
      });
      return out;
    }, [board]);

    const byId = useMemo(function () {
      const m = {};
      tasks.forEach(function (t) { m[t.id] = t; });
      return m;
    }, [tasks]);

    const openTasks = tasks.filter(function (t) { return t.status !== "done" && t.status !== "archived"; });
    const doneTasks = tasks.filter(function (t) { return t.status === "done"; });
    const runningCount = tasks.filter(function (t) { return t.status === "running"; }).length;

    const todayList = todayIds.map(function (id) { return byId[id]; }).filter(function (t) {
      return t && t.status !== "done" && t.status !== "archived";
    });
    const todaySet = {};
    todayIds.forEach(function (id) { todaySet[id] = true; });
    const repoIndex = {};
    repoOrder.forEach(function (id, i) { repoIndex[id] = i; });
    const repoList = openTasks.filter(function (t) {
      if (todaySet[t.id]) return false;
      if (q && (t.title || "").toLowerCase().indexOf(q.toLowerCase()) < 0) return false;
      return true;
    }).sort(function (a, b) {
      const ia = repoIndex[a.id] === undefined ? 1e9 : repoIndex[a.id];
      const ib = repoIndex[b.id] === undefined ? 1e9 : repoIndex[b.id];
      return ia - ib;
    });

    /* ---------- actions ---------- */
    const patch = useCallback(async function (id, body) {
      setBusy(true);
      try {
        await apiPatch(id, body);
        await load();
        return true;
      } catch (e) {
        showToast("操作失败：" + (e && e.message ? e.message : String(e)));
        return false;
      } finally {
        setBusy(false);
      }
    }, [load, showToast]);

    const markDone = useCallback(async function (id) {
      const t = byId[id];
      if (!t || t.status === "done" || t.status === "archived") return;   // 已完成的不重复触发
      if (inFlight.current[id]) return;                                    // 连点只算一次
      inFlight.current[id] = true;
      try {
        // 待整理/待执行/已排期 不在内核可完成集合里：先入「就绪」，再完成。
        if (COMPLETABLE.indexOf(t.status) < 0) {
          const okReady = await patch(id, { status: "ready" });
          if (!okReady) return;
        }
        const ok = await patch(id, { status: "done", summary: "从工作台标记完成" });
        if (ok) showToast("已完成，归档到「已完成」");
      } finally {
        delete inFlight.current[id];
      }
    }, [byId, patch, showToast]);

    const restore = useCallback(function (id) {
      patch(id, { status: "todo" }).then(function (ok) {
        if (ok) showToast("已恢复到原池子");
      });
    }, [patch, showToast]);

    const flowDrop = useCallback(function (id, status) {
      if (!id) return;
      if (status === "running") { showToast("运行中由调度器接管，不能手动设置"); return; }
      const t = byId[id];
      const cur = t ? t.status : null;
      if (cur === status) return;                                          // 拖回原列不算操作
      const from = ENTERABLE[status];
      if (cur && from && from.indexOf(cur) < 0) {
        showToast("不能从「" + statusLabel(cur) + "」移到「" + statusLabel(status) + "」：" + ENTER_HINT[status]);
        return;
      }
      const body = { status: status };
      if (status === "done") body.summary = "从工作台完成";
      patch(id, body).then(function (ok) { if (ok) showToast("已移到「" + STATUS[status].label + "」"); });
    }, [byId, patch, showToast]);

    const moveToPanel = useCallback(function (id, toToday, beforeId, before) {
      if (!id) return;
      setTodayIds(function (prev) {
        const next = prev.filter(function (x) { return x !== id; });
        if (toToday) {
          let idx = beforeId ? next.indexOf(beforeId) : next.length;
          if (idx < 0) idx = next.length;
          if (beforeId && !before) idx += 1;
          next.splice(idx, 0, id);
        }
        return next;
      });
      setRepoOrder(function (prev) {
        const next = prev.filter(function (x) { return x !== id; });
        if (!toToday) {
          let idx = beforeId ? next.indexOf(beforeId) : next.length;
          if (idx < 0) idx = next.length;
          if (beforeId && !before) idx += 1;
          next.splice(idx, 0, id);
        }
        return next;
      });
      showToast(toToday ? "已加入今日待办" : "已放回待办仓库");
    }, [showToast]);

    const quickAdd = useCallback(async function () {
      const title = cap.trim();
      if (!title) return;
      setBusy(true);
      try {
        const res = await apiCreate({ title: title, triage: true, priority: 0 });
        const id = res && res.task && res.task.id;
        if (id) setTodayIds(function (prev) { return [id].concat(prev); });
        setCap("");
        await load();
        showToast("已加入今日待办");
      } catch (e) {
        showToast("创建失败：" + (e && e.message ? e.message : String(e)));
      } finally { setBusy(false); }
    }, [cap, load, showToast]);

    const createTask = useCallback(async function () {
      const title = form.title.trim();
      if (!title) return;
      setBusy(true);
      try {
        const res = await apiCreate({
          title: title,
          body: form.note || null,
          priority: Number(form.prio) || 0,
          triage: true
        });
        const id = res && res.task && res.task.id;
        if (form.today && id) setTodayIds(function (prev) { return [id].concat(prev); });
        setModal(false);
        setForm({ title: "", prio: 0, note: "", today: false });
        await load();
        showToast("已创建" + (form.today ? "并加入今天" : "，进入待办仓库"));
      } catch (e) {
        showToast("创建失败：" + (e && e.message ? e.message : String(e)));
      } finally { setBusy(false); }
    }, [form, load, showToast]);

    /* ---------- render helpers ---------- */
    const tabBtn = function (key, label, badge) {
      return h("button", {
        key: key,
        className: "wb-tab" + (tab === key ? " on" : ""),
        onClick: function () { setTab(key); }
      }, label, badge != null ? h("span", { className: "n" }, badge) : null);
    };

    const row = function (t) {
      const cls = "wb-row" +
        (over && over.id === t.id ? (over.before ? " over-top" : " over-bottom") : "");
      return h("div", {
        key: t.id,
        className: cls,
        draggable: true,
        onDragStart: function (e) {
          dragRef.current = t.id;
          e.dataTransfer.setData("text/plain", t.id);
          e.dataTransfer.effectAllowed = "move";
        },
        onDragEnd: function () { dragRef.current = null; setOver(null); setDropPanel(null); },
        onDragOver: function (e) {
          e.preventDefault();
          e.stopPropagation();
          const r = e.currentTarget.getBoundingClientRect();
          const before = (e.clientY - r.top) < r.height / 2;
          setOver({ id: t.id, before: before });
        },
        onDragLeave: function () { setOver(null); },
        onDrop: function (e) {
          e.preventDefault();
          e.stopPropagation();
          const info = over && over.id === t.id ? over : { id: t.id, before: true };
          const inToday = e.currentTarget.closest(".wb-panel[data-panel=today]") != null;
          setOver(null); setDropPanel(null);
          moveToPanel(dragRef.current, inToday, t.id, info.before);
        }
      },
        h("button", { className: "wb-check", title: "标记完成", onClick: function () { markDone(t.id); } }),
        h("span", { className: "wb-dot", style: { background: (STATUS[t.status] || {}).color || "var(--wb-muted)" } }),
        h("span", { className: "wb-title" }, t.title),
        h("span", { className: "wb-meta" },
          t.priority >= 2 ? h("span", { className: "wb-tag" }, "高") : null,
          h("span", { className: "wb-tag" }, (STATUS[t.status] || {}).label || t.status),
          h("span", { className: "wb-who" }, t.assignee || "未指派")
        )
      );
    };

    const panel = function (key, title, hint, list, headExtra) {
      return h("section", {
        key: key,
        className: "wb-panel" + (dropPanel === key ? " drop" : ""),
        "data-panel": key,
        onDragOver: function (e) { e.preventDefault(); setDropPanel(key); },
        onDragLeave: function (e) {
          if (!e.currentTarget.contains(e.relatedTarget)) setDropPanel(null);
        },
        onDrop: function (e) {
          e.preventDefault();
          setDropPanel(null);
          moveToPanel(dragRef.current, key === "today", null, true);
        }
      },
        h("div", { className: "wb-phead" },
          h("div", { className: "wb-prow" },
            h("div", { className: "wb-ptitle" }, title, h("span", { className: "n" }, list.length)),
            h("div", { className: "wb-phint" }, hint)
          ),
          headExtra || null
        ),
        h("div", { className: "wb-list" },
          list.length ? list.map(row) : h("div", { className: "wb-empty" }, key === "today" ? "把右侧仓库的待办拖过来，或在上方直接输入" : "仓库是空的")
        )
      );
    };

    const renderPlan = function () {
      const todayPanel = panel("today", "今日待办", "今天要做 · 拖回右侧可放回仓库", todayList,
        h("div", { className: "wb-capture" },
          h("input", {
            className: "wb-input",
            placeholder: "今天要做的事，回车加入",
            value: cap,
            onChange: function (e) { setCap(e.target.value); },
            onKeyDown: function (e) { if (e.key === "Enter") quickAdd(); }
          }),
          h("button", { className: "wb-btn", onClick: quickAdd, disabled: busy }, "加入今天")
        )
      );
      const repoPanel = panel("repo", "待办仓库", "长期 · 拖到左侧 ← 加入今天", repoList,
        h("div", { className: "wb-capture" },
          h("input", {
            className: "wb-input",
            placeholder: "搜索仓库…",
            value: q,
            onChange: function (e) { setQ(e.target.value); }
          })
        )
      );
      return h("div", { className: "wb-plan" }, todayPanel, repoPanel);
    };

    const renderFlow = function () {
      const cols = STATUS_ORDER.map(function (k) {
        const list = tasks.filter(function (t) { return t.status === k; }).sort(function (a, b) {
          const ia = typeof a.order === "number" ? a.order : 0;
          const ib = typeof b.order === "number" ? b.order : 0;
          return ia - ib;
        });
        return h("div", {
          key: k,
          className: "wb-col" + (dropCol === k ? " drop" : ""),
          onDragOver: function (e) { e.preventDefault(); setDropCol(k); },
          onDragLeave: function () { setDropCol(null); },
          onDrop: function (e) {
            e.preventDefault();
            setDropCol(null);
            flowDrop(dragRef.current, k);
          }
        },
          h("div", { className: "wb-chead" },
            h("span", null, h("span", { className: "wb-dot", style: { background: STATUS[k].color, display: "inline-block", marginRight: 5, verticalAlign: "1px" } }), STATUS[k].label),
            h("span", { className: "n" }, list.length)
          ),
          list.map(function (t) {
            return h("div", {
              key: t.id,
              className: "wb-card" + (k === "done" ? " done" : ""),
              draggable: true,
              onDragStart: function (e) {
                dragRef.current = t.id;
                e.dataTransfer.setData("text/plain", t.id);
                e.dataTransfer.effectAllowed = "move";
              },
              onDragEnd: function () { dragRef.current = null; }
            },
              h("div", { className: "ct" }, t.title),
              h("div", { className: "cm" },
                h("span", null, t.id),
                h("span", { className: "wb-tag" }, t.assignee || "未指派"),
                h("span", { className: "who" }, t.priority >= 2 ? "高优先" : "")
              )
            );
          }),
          list.length ? null : h("div", { className: "wb-dropempty" }, "拖到这里")
        );
      });
      return h("div", null,
        h("div", { className: "wb-stats" },
          h("span", null, "运行中 ", h("b", null, runningCount)),
          h("span", null, "今日完成 ", h("b", null, doneTasks.filter(function (t) { return dayKey(t.completed_at) === "today"; }).length)),
          h("span", null, "待拍板 ", h("b", null, tasks.filter(function (t) { return t.status === "blocked"; }).length)),
          h("span", null, "待启动 ", h("b", null, tasks.filter(function (t) { return ["triage", "todo", "ready"].indexOf(t.status) >= 0; }).length)),
          h("span", { style: { marginLeft: "auto", opacity: 0.75 } }, "看板是流程视图：拖拽卡片换列 = 改状态")
        ),
        h("div", { className: "wb-board" }, cols)
      );
    };

    const renderDone = function () {
      const groups = [
        { key: "today", label: "今天" },
        { key: "yesterday", label: "昨天" },
        { key: "earlier", label: "更早" }
      ];
      const sorted = doneTasks.slice().sort(function (a, b) {
        return (b.completed_at || 0) - (a.completed_at || 0);
      });
      const body = groups.map(function (g) {
        const list = sorted.filter(function (t) { return dayKey(t.completed_at) === g.key; });
        if (!list.length) return null;
        return h("div", { key: g.key, className: "wb-dgroup" },
          h("div", { className: "wb-dgh" }, g.label, h("span", { className: "n" }, list.length)),
          list.map(function (t) {
            return h("div", { key: t.id, className: "wb-drow" },
              h("span", { className: "wb-dcheck" }, "✓"),
              h("span", { className: "wb-dtitle" }, t.title),
              h("span", { className: "wb-dmeta" },
                h("span", { className: "wb-tag" }, t.assignee || "未指派"),
                h("span", null, timeLabel(t.completed_at))
              ),
              h("button", { className: "wb-btn sm ghost wb-restore", onClick: function () { restore(t.id); } }, "恢复")
            );
          })
        );
      }).filter(Boolean);
      return h("div", { className: "wb-done" },
        h("div", { className: "wb-dstats" },
          h("span", null, "共 ", h("b", null, doneTasks.length), " 件已完成"),
          h("span", null, "今天 ", h("b", null, doneTasks.filter(function (t) { return dayKey(t.completed_at) === "today"; }).length), " 件"),
          h("span", { style: { marginLeft: "auto", opacity: 0.75 } }, "悬停可「恢复」回原来的池子")
        ),
        body.length ? body : h("div", { className: "wb-empty" }, "还没有完成的任务")
      );
    };

    const renderModal = function () {
      return h("div", { className: "wb-overlay", onClick: function (e) { if (e.target === e.currentTarget) setModal(false); } },
        h("div", { className: "wb-dialog" },
          h("h3", null, "新建待办"),
          h("div", { className: "wb-field" },
            h("label", null, "标题"),
            h("input", {
              type: "text",
              autoFocus: true,
              placeholder: "例：把 SentinelDrive 的告警接入飞书",
              value: form.title,
              onChange: function (e) { setForm(Object.assign({}, form, { title: e.target.value })); }
            })
          ),
          h("div", { className: "wb-frow" },
            h("div", { className: "wb-field" },
              h("label", null, "优先级"),
              h("select", {
                value: form.prio,
                onChange: function (e) { setForm(Object.assign({}, form, { prio: e.target.value })); }
              },
                h("option", { value: 0 }, "普通"),
                h("option", { value: 2 }, "高"),
                h("option", { value: 1 }, "低")
              )
            )
          ),
          h("div", { className: "wb-field" },
            h("label", null, "备注（可选）"),
            h("textarea", {
              placeholder: "补充背景、链接或验收要求…",
              value: form.note,
              onChange: function (e) { setForm(Object.assign({}, form, { note: e.target.value })); }
            })
          ),
          h("label", { className: "wb-checkline" },
            h("input", {
              type: "checkbox",
              checked: form.today,
              onChange: function (e) { setForm(Object.assign({}, form, { today: e.target.checked })); }
            }),
            "同时加入今日待办"
          ),
          h("div", { className: "wb-dact" },
            h("button", { className: "wb-btn", onClick: function () { setModal(false); } }, "取消"),
            h("button", { className: "wb-btn primary", onClick: createTask, disabled: busy }, "创建")
          )
        )
      );
    };

    return h("div", { className: "wb-root" },
      h("div", { className: "wb-head" },
        h("div", { className: "wb-tabs" },
          tabBtn("plan", "待办", openTasks.length),
          tabBtn("flow", "看板", runningCount + " 运行"),
          tabBtn("done", "已完成", doneTasks.length)
        ),
        h("div", { className: "wb-actions" },
          h("button", { className: "wb-btn", onClick: load, title: "刷新" }, "刷新"),
          h("button", { className: "wb-btn primary", onClick: function () { setModal(true); } }, "＋ 新建待办")
        )
      ),
      err ? h("div", { className: "wb-empty" }, err) : null,
      tab === "plan" ? renderPlan() : tab === "flow" ? renderFlow() : renderDone(),
      modal ? renderModal() : null,
      toast ? h("div", { className: "wb-toast" }, toast) : null
    );
  }

  PLUGINS.register("workbench", Workbench);
})();