import React, { useState } from "react";
import Board from "./board/Board";
import "./styles/board.css";

type TabKey = "flow";

const TABS: { key: TabKey; label: string }[] = [{ key: "flow", label: "看板" }];

export default function App(): React.ReactElement {
  const [tab] = useState<TabKey>("flow");

  return (
    <div className="wb-root">
      <div className="wb-head">
        <div className="wb-tabs">
          {TABS.map((t) => (
            <button key={t.key} className={"wb-tab" + (tab === t.key ? " on" : "")}>
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {tab === "flow" ? <Board /> : null}
    </div>
  );
}