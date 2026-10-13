import { useState } from "react";
import type { DataSource } from "./datasource";
import { useFleet } from "./fleet";
import Room from "./components/Room";
import SidePanel from "./components/SidePanel";
import ApprovalsBar from "./components/ApprovalsBar";
import ResultsPanel from "./components/ResultsPanel";

// App depends only on the DataSource interface; the concrete adapter is injected
// from main.tsx (the composition root).
export default function App({ source }: { source: DataSource }) {
  const { agents, connected, approvals, run, decisions } = useFleet(source);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = agents.find((a) => a.id === selectedId) ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <h1>agent-troops Board</h1>
        <span className={`conn ${connected ? "live" : "down"}`}>
          {connected ? "live" : "connecting…"}
        </span>
        <span className="count">{agents.length} agents</span>
      </header>
      <ApprovalsBar approvals={approvals} source={source} />
      <div className="layout">
        <Room agents={agents} selectedId={selectedId} onSelect={setSelectedId} />
        <SidePanel agent={selected} run={run} source={source} onClose={() => setSelectedId(null)} />
      </div>
      <ResultsPanel run={run} decisions={decisions} />
    </div>
  );
}
