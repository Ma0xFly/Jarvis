(function () {
  "use strict";

  // ---- theme -------------------------------------------------------------
  var root = document.documentElement;
  var saved = null;
  try { saved = localStorage.getItem("wb.theme"); } catch (e) {}
  var theme = saved === "light" || saved === "dark" ? saved : "dark";
  root.setAttribute("data-theme", theme);
  var btn = document.getElementById("themeBtn");
  function paintBtn() { btn.textContent = theme === "dark" ? "☾" : "☀"; }
  paintBtn();
  btn.addEventListener("click", function () {
    theme = theme === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem("wb.theme", theme); } catch (e) {}
    paintBtn();
  });

  // ---- render ------------------------------------------------------------
  var app = document.getElementById("app");
  var Comp = window.__WORKBENCH_COMPONENT__;
  if (!Comp) {
    app.innerHTML = '<div style="padding:32px;font-size:13px;opacity:.8">工作台组件加载失败：检查 /static/app.js 是否存在。</div>';
    return;
  }
  ReactDOM.createRoot(app).render(React.createElement(Comp));

  // ---- connection badge --------------------------------------------------
  var badge = document.getElementById("conn");
  var text = document.getElementById("connText");
  async function ping() {
    try {
      var res = await fetch("/api/plugins/kanban/board", { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error("HTTP " + res.status);
      badge.className = "conn on";
      text.textContent = "已连接 kanban";
    } catch (e) {
      badge.className = "conn off";
      text.textContent = "kanban 不可达";
    }
  }
  ping();
  setInterval(ping, 30000);
})();