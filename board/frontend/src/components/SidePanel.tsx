import { useEffect, useRef, useState } from "react";
import type { DataSource, RunView, WorkerActivity, WorkerActivityItem } from "../datasource";
import Markdown from "./Markdown";
import { activityLabel, visibleState, type AgentView } from "../fleet";

// Activity item timestamps are Unix seconds.
function agoFromUnixSeconds(sec: number): string {
  const s = Math.max(0, Math.round(Date.now() / 1000 - sec));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

const ACTIVITY_POLL_MS = 1500;

// Polls source.getWorkerActivity(agentId) — the ONLY way this component talks
// to the backend (no fetch() here). Enabled only
// while the caller says so (agent selected + working/done); cleanly stops
// (interval cleared, in-flight guard reset) on agentId/enabled change and on
// unmount. A `cancelled` flag + `inFlightRef` reset in cleanup together mean:
// a straggling response from a PREVIOUS agent/disabled period can never land
// on top of the current one, and a slow poll can never overlap the next tick.
function useWorkerActivity(
  source: DataSource,
  agentId: string | null,
  enabled: boolean,
): WorkerActivity | null {
  const [activity, setActivity] = useState<WorkerActivity | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    setActivity(null); // never show a stale/previous agent's feed while switching
    if (!agentId || !enabled) return;
    let cancelled = false;

    const poll = async () => {
      if (inFlightRef.current) return; // skip this tick — previous call still in flight
      inFlightRef.current = true;
      try {
        const result = await source.getWorkerActivity(agentId);
        if (!cancelled) setActivity(result);
      } catch {
        // Failed/aborted poll: keep the last frame on screen, no error state.
      } finally {
        inFlightRef.current = false;
      }
    };

    void poll(); // first poll immediately on selection, don't wait a full interval
    const interval = window.setInterval(() => {
      void poll();
    }, ACTIVITY_POLL_MS);

    return () => {
      cancelled = true;
      inFlightRef.current = false;
      window.clearInterval(interval);
    };
  }, [source, agentId, enabled]);

  return activity;
}

// "→ calling <tool>" for tool_call ALWAYS (never shows raw args — a clean
// one-liner regardless of what's in `text`); tool_result shows a short
// preview when there's text, else just the tool name; assistant shows its
// text as-is. Never returns a string that would render an empty/placeholder
// row — callers filter out any blank result.
function activityItemLine(item: WorkerActivityItem): string {
  if (item.kind === "blocked") return `⛔ refused ${item.label ?? "call"}: ${item.text ?? ""}`;
  if (item.kind === "tool_call") return `→ calling ${item.label ?? "tool"}`;
  if (item.kind === "tool_result") {
    const label = item.label ?? "tool";
    if (!item.text) return label;
    const preview = item.text.length > 100 ? `${item.text.slice(0, 100)}…` : item.text;
    return `${label}: ${preview}`;
  }
  return item.text ?? ""; // assistant
}

const LEG_INFO: { key: string; letter: string; title: string; hint: string }[] = [
  { key: "private_data", letter: "P", title: "Private data", hint: "can read your files" },
  { key: "untrusted_content", letter: "U", title: "Untrusted content", hint: "reads text an attacker could control" },
  { key: "exfiltration", letter: "E", title: "Exfiltration", hint: "can send data off this machine" },
];

// The "walls" of a role: which of the three trifecta legs its tools grant. A role holding all
// three can be steered by injected text into leaking; the team is built so no unsupervised one does.
function BoundariesBlock({ b }: { b: NonNullable<AgentView["boundaries"]> }) {
  const all = LEG_INFO.every((l) => b.legs.includes(l.key));
  return (
    <div className="boundaries">
      <h3>Boundaries</h3>
      <ul className="boundary-legs">
        {LEG_INFO.map((l) => (
          <li key={l.key} className={b.legs.includes(l.key) ? "leg-held" : "leg-free"}>
            <span className={`leg-chip leg-${l.letter}`}>{l.letter}</span>
            <span>
              {l.title}: <strong>{b.legs.includes(l.key) ? "yes" : "no"}</strong>{" "}
              <span className="muted">— {l.hint}</span>
            </span>
          </li>
        ))}
      </ul>
      <p className="muted boundary-tools">
        tools: {b.tools ? b.tools.join(", ") || "none" : "inherits everything"}
        {b.gated ? " · needs human approval" : ""}
        {b.guarded ? " · shell limited to an allowlist" : ""}
      </p>
      {all && <p className="boundary-warn">Holds all three legs{b.gated ? " — relies on human approval" : ""}.</p>}
    </div>
  );
}

export default function SidePanel({
  agent,
  run,
  source,
  onClose,
}: {
  agent: AgentView | null;
  run: RunView | null;
  source: DataSource;
  onClose: () => void;
}) {
  const activityFeedRef = useRef<HTMLUListElement | null>(null);

  // Computed before the early `!agent` return so the hook is always called (Rules of Hooks)
  // and is a no-op while nothing is selected. Polls while working, and through the brief
  // "done"/"blocked" display so the last frame still lands.
  const gate = agent ? visibleState(agent, Date.now()) : null;
  const activityEnabled = gate === "working" || gate === "done" || gate === "blocked";
  const workerActivity = useWorkerActivity(source, agent?.id ?? null, activityEnabled);

  useEffect(() => {
    const el = activityFeedRef.current;
    if (el) el.scrollTop = el.scrollHeight; // auto-scroll to the newest line
  }, [workerActivity]);

  if (!agent) {
    return (
      <aside className="panel panel-empty">
        <p>Click a robot to see what that team member may do and what it is doing.</p>
      </aside>
    );
  }

  const state = visibleState(agent, Date.now());
  const activity = activityLabel(agent, state);
  const asked = run?.asked[agent.id] ?? null;
  const report = run ? [...run.reports].reverse().find((r) => r.agent === agent.id) ?? null : null;
  const items = (workerActivity?.available ? workerActivity.items : [])
    .map((item) => ({ item, line: activityItemLine(item) }))
    .filter(({ line }) => line.trim() !== "");

  return (
    <aside className="panel">
      <div className="panel-head">
        <h2>{agent.name}</h2>
        <button type="button" className="close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <div className={`badge state-${state}`}>{state}</div>
      {agent.boundaries && <BoundariesBlock b={agent.boundaries} />}
      {activity && <p className="muted activity-label">{activity}</p>}

      {asked && (
        <>
          <h3>Asked to</h3>
          <p className="asked">{asked}</p>
        </>
      )}

      <h3>Live activity</h3>
      <p className="muted activity-subtitle">
        The latest tool calls and results, ~1-2s behind. Never the model&apos;s hidden reasoning.
      </p>
      {items.length === 0 ? (
        <p className="muted">No live activity — agent idle</p>
      ) : (
        <ul className="activity-feed" ref={activityFeedRef}>
          {items.map(({ item, line }, i) => (
            <li key={`${item.ts}-${i}`} className={`activity-item activity-item-${item.kind}`}>
              <span className="activity-text">{line}</span>
              <span className="muted activity-ts">{agoFromUnixSeconds(item.ts)}</span>
            </li>
          ))}
        </ul>
      )}

      {report && (
        <>
          <h3>Its report</h3>
          <div className="report">
            <Markdown text={report.text} />
          </div>
        </>
      )}
    </aside>
  );
}
