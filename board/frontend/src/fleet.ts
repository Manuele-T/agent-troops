// Fleet state model. Each robot is one team role. Its coarse state comes from the hook-derived
// event TRANSITIONS the backend pushes (claimed/heartbeat/completed/blocked), never from status
// snapshots. A finished role shows a brief `done` flash and only falls to `idle` after a
// cooldown, so fast steps don't look like nothing happened. `fineState`/`activity` mirror the
// backend's synthetic "activity" frames (which tool family the role is using right now).
import { useCallback, useEffect, useRef, useState } from "react";
import type { Agent, AgentEvent, DataSource, DecisionRecord, PendingApproval, RunView } from "./datasource";

export type VisibleState = "idle" | "working" | "done" | "blocked";

// What a role is allowed to do, computed server-side from its agent file (teambuild.audit).
// legs: the "lethal trifecta" legs its tools grant — private_data, untrusted_content, exfiltration.
export interface Boundaries {
  tools: string[] | null; // null = no `tools:` line, inherits everything
  legs: string[];
  gated: boolean;
  guarded: boolean;
  verdict: string;
}

function parseBoundaries(raw: unknown): Boundaries | null {
  const b = raw as Partial<Boundaries> | null | undefined;
  if (!b || !Array.isArray(b.legs)) return null;
  return {
    tools: Array.isArray(b.tools) ? b.tools.map(String) : null,
    legs: b.legs.map(String),
    gated: Boolean(b.gated),
    guarded: Boolean(b.guarded),
    verdict: String(b.verdict ?? ""),
  };
}

export interface AgentView {
  id: string;
  name: string;
  working: boolean; // a tool call has started and the role hasn't finished or been blocked since
  lastTerminalKind: string | null; // "completed" | "blocked"
  lastTerminalAt: number | null; // ms
  fineState: string | null; // "working" | null
  activity: string | null; // tool family (coding/writing/researching/reading/acting/delegating)
  boundaries: Boundaries | null;
}

export const DONE_COOLDOWN_MS = 6000;
const WORKING_KINDS = new Set(["claimed", "heartbeat"]);

export function visibleState(a: AgentView, now: number): VisibleState {
  if (a.working) return "working";
  if (a.lastTerminalKind === "blocked") return "blocked";
  if (a.lastTerminalKind === "completed" && a.lastTerminalAt && now - a.lastTerminalAt < DONE_COOLDOWN_MS) {
    return "done";
  }
  return "idle";
}

// The human-readable fine-grained label for the side panel. Only meaningful while the coarse
// state is "working" — outside that window the badge (idle/blocked/done) says it all.
export function activityLabel(agent: AgentView, coarse: VisibleState): string | null {
  if (coarse !== "working" || agent.fineState !== "working") return null;
  return agent.activity;
}

function freshAgent(id: string): AgentView {
  return {
    id,
    name: id,
    working: false,
    lastTerminalKind: null,
    lastTerminalAt: null,
    fineState: null,
    activity: null,
    boundaries: null,
  };
}

function seedAgent(ag: Agent): AgentView {
  const a = freshAgent(String(ag.id));
  a.name = String(ag.name ?? ag.id);
  a.boundaries = parseBoundaries(ag.boundaries);
  const endedMs = ag.last_ended_at ? Number(ag.last_ended_at) * 1000 : null;
  if (ag.state === "working") a.working = true;
  else if (ag.state === "done" || ag.state === "blocked") {
    a.lastTerminalKind = ag.state === "done" ? "completed" : "blocked";
    a.lastTerminalAt = endedMs;
  }
  return a;
}

export interface Fleet {
  agents: AgentView[];
  connected: boolean;
  approvals: PendingApproval[];
  run: RunView | null;
  decisions: DecisionRecord[];
}

export function useFleet(source: DataSource): Fleet {
  const [agents, setAgents] = useState<Record<string, AgentView>>({});
  const [connected, setConnected] = useState(false);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [run, setRun] = useState<RunView | null>(null);
  const [decisions, setDecisions] = useState<DecisionRecord[]>([]);
  const [, setTick] = useState(0); // forces cooldown re-evaluation each second

  const agentsRef = useRef(agents);
  agentsRef.current = agents;
  const lastMissingRef = useRef(0);
  const boundaryTriesRef = useRef(0);

  const refreshBoundaries = useCallback(async () => {
    try {
      const list = await source.getAgents();
      setAgents((prev) => {
        const next = { ...prev };
        for (const ag of list) {
          const b = parseBoundaries(ag.boundaries);
          if (b && next[ag.id] && !next[ag.id].boundaries) next[ag.id] = { ...next[ag.id], boundaries: b };
        }
        return next;
      });
    } catch {
      /* boundaries are decoration; ignore */
    }
  }, [source]);

  const handleEvent = useCallback((ev: AgentEvent) => {
    if (ev.kind === "__open") return setConnected(true);
    if (ev.kind === "__error") return setConnected(false);
    if (ev.kind === "decision") {
      if (ev.payload && typeof ev.payload === "object") {
        const d = ev.payload as DecisionRecord;
        setDecisions((prev) => [...prev, d].slice(-50));
      }
      return;
    }
    if (ev.kind === "run") {
      if (ev.payload && typeof ev.payload === "object") setRun(ev.payload as RunView);
      return;
    }
    if (ev.kind === "gate") {
      const list = (ev.payload as { pending?: PendingApproval[] } | null | undefined)?.pending;
      if (Array.isArray(list)) setApprovals(list);
      return;
    }

    const p = ev.payload as
      | { assignee?: string; fine_state?: string | null; activity?: string | null }
      | null
      | undefined;
    const assignee = p?.assignee;
    if (!assignee) return;

    setAgents((prev) => {
      const a: AgentView = prev[assignee] ? { ...prev[assignee] } : freshAgent(assignee);
      if (ev.kind === "activity") {
        a.fineState = p.fine_state ?? null;
        a.activity = p.activity ?? null;
      } else if (WORKING_KINDS.has(ev.kind)) {
        a.working = true;
        a.lastTerminalKind = null;
        a.lastTerminalAt = null;
      } else if (ev.kind === "completed" || ev.kind === "blocked") {
        a.working = false;
        a.lastTerminalKind = ev.kind;
        a.lastTerminalAt = Date.now();
      }
      return { ...prev, [assignee]: a };
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const ags = await source.getAgents();
        if (cancelled) return;
        const amap: Record<string, AgentView> = {};
        for (const ag of ags) amap[ag.id] = seedAgent(ag);
        setAgents(amap);
        const [pending, current, past] = await Promise.all([
          source.getPendingApprovals(), source.getRun(), source.getDecisions(),
        ]);
        if (!cancelled) {
          setApprovals(pending);
          setRun(current);
          setDecisions(past);
        }
      } catch {
        /* start empty; live events will populate */
      }
    })();

    const unsub = source.subscribe(handleEvent);
    const ticker = window.setInterval(() => {
      setTick((t) => t + 1);
      // Roles that first appear after page load only get boundaries from a re-fetch. A few
      // tries per newly-seen role, then stop: baseline sessions have no repo to read.
      const missing = Object.values(agentsRef.current).filter((a) => !a.boundaries).length;
      if (missing !== lastMissingRef.current) {
        lastMissingRef.current = missing;
        boundaryTriesRef.current = 0;
      }
      if (missing > 0 && boundaryTriesRef.current < 6) {
        boundaryTriesRef.current += 1;
        void refreshBoundaries();
      }
    }, 1000);
    return () => {
      cancelled = true;
      unsub();
      window.clearInterval(ticker);
    };
  }, [source, handleEvent, refreshBoundaries]);

  return {
    agents: Object.values(agents).sort((a, b) => a.id.localeCompare(b.id)),
    connected,
    approvals,
    run,
    decisions,
  };
}
