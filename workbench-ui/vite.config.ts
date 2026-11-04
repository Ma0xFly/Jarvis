import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

/**
 * 两套 build 共用一份源码：
 *   --mode plugin      React external，产物挂到 window.__HERMES_PLUGIN_SDK__
 *   --mode standalone  React 打包进产物，自己 createRoot(#workbench-root)
 *
 * 插件宿主用经典 <script> 加载，所以格式必须是 iife。
 */
const entry = fileURLToPath(new URL("./src/main.tsx", import.meta.url));

/** 插件包里 react 直接指向宿主注入的全局对象 */
const PLUGIN_GLOBALS: Record<string, string> = {
  react: "React",
  "react-dom": "ReactDOM",
  "react-dom/client": "ReactDOM",
};

export default defineConfig(({ mode }) => {
  const isPlugin = mode === "plugin";

  return {
    define: { "process.env.NODE_ENV": JSON.stringify("production") },
    // 不用 @vitejs/plugin-react：它默认走 automatic JSX runtime，会把 react/jsx-runtime 打进插件包
    esbuild: {
      jsx: "transform",
      jsxFactory: "React.createElement",
      jsxFragment: "React.Fragment",
    },
    build: {
      outDir: isPlugin ? "dist/plugin" : "dist/standalone",
      emptyOutDir: true,
      target: "es2019",
      cssCodeSplit: false,
      lib: {
        entry,
        name: "Workbench",
        formats: ["iife"],
        fileName: () => "index.js",
      },
      rollupOptions: {
        external: isPlugin ? ["react", "react-dom", "react-dom/client"] : [],
        output: {
          // 宿主已经把 React 注入到 __HERMES_PLUGIN_SDK__，插件包不能再打包一份。
          // 用 var 声明：宿主没给 ReactDOM 时也只是 undefined，不会 ReferenceError。
          banner: isPlugin
            ? "var React = window.__HERMES_PLUGIN_SDK__.React; var ReactDOM = window.__HERMES_PLUGIN_SDK__.ReactDOM;"
            : "",
          globals: isPlugin ? PLUGIN_GLOBALS : {},
          // 外部依赖直接当全局对象用（interop "default"：模块本身即默认导出）
          interop: isPlugin ? "default" : "auto",
          assetFileNames: (info: any) => {
            const name = info.name || (info.names && info.names[0]) || "asset";
            return name.endsWith(".css") ? "style.css" : name;
          },
        } as any,
      },
    },
  };
});