import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/base.css";

const sdk = window.__HERMES_PLUGIN_SDK__;

if (sdk) {
  // 插件宿主：由 dashboard 负责挂载，我们只交出组件
  const plugins = window.__HERMES_PLUGINS__;
  if (!plugins) throw new Error("Hermes 插件宿主缺少 window.__HERMES_PLUGINS__");
  plugins.register("workbench", App);
} else {
  // 独立版：自己接管 #workbench-root
  const el = document.getElementById("workbench-root");
  if (!el) throw new Error("独立版缺少 #workbench-root 挂载点");
  createRoot(el).render(<App />);
}