import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { isPluginHost } from "./platform";
import "./styles/base.css";

const sdk = window.__HERMES_PLUGIN_SDK__;

if (isPluginHost) {
  // 插件宿主：由 dashboard 负责挂载，我们只交出组件
  const plugins = window.__HERMES_PLUGINS__;
  if (!plugins) throw new Error("Hermes 插件宿主缺少 window.__HERMES_PLUGINS__");
  plugins.register("workbench", App);
} else if (sdk) {
  // 与 platform.ts 使用同一谓词（sdk && sdk.fetchJSON）。只有 SDK 没有
  // fetchJSON 时无法连 kanban API，不能降级成独立版（会渲染错 UI 并 404）。
  throw new Error(
    "不支持的插件宿主：window.__HERMES_PLUGIN_SDK__ 缺少 fetchJSON，无法连接 kanban API",
  );
} else {
  // 独立版：自己接管 #workbench-root
  const el = document.getElementById("workbench-root");
  if (!el) throw new Error("独立版缺少 #workbench-root 挂载点");
  createRoot(el).render(<App />);
}