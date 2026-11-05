import React, { useState } from "react";
import Board from "./board/Board";
import TodoPage from "./todo/TodoPage";
import { isPluginHost } from "./platform";
import "./styles/board.css";
import "./styles/todo.css";

type TabKey = "todo" | "flow";

// 插件宿主没有 /api/workbench/* 后端；待办依赖它，只在独立版 8787 提供。
const TABS: { key: TabKey; label: string }[] = isPluginHost
  ? [{ key: "flow", label: "看板" }]
  : [
      { key: "todo", label: "待办" },
      { key: "flow", label: "看板" },
    ];

const DEFAULT_TAB: TabKey = isPluginHost ? "flow" : "todo";

export default function App(): React.ReactElement {
  // 独立版待办第一且默认打开；插件宿主只有看板。
  const [tab, setTab] = useState<TabKey>(DEFAULT_TAB);

  return (
    <div className="wb-root">
      <div className="wb-head">
        <div className="wb-tabs">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={"wb-tab" + (tab === t.key ? " on" : "")}
              onClick={() => setTab(t.key)}
            >
              {t.label}
            </button>
          ))}
        </div>
        {isPluginHost ? (
          <span className="wb-notice">待办请用 127.0.0.1:8787 独立版</span>
        ) : null}
      </div>

      {/* 两个面板都保持挂载，切换 tab 不丢草稿/验收编辑状态。 */}
      {!isPluginHost ? (
        <div className={"wb-pane" + (tab === "todo" ? "" : " wb-pane-hidden")}>
          <TodoPage />
        </div>
      ) : null}
      <div className={"wb-pane" + (tab === "flow" ? "" : " wb-pane-hidden")}>
        <Board />
      </div>
    </div>
  );
}