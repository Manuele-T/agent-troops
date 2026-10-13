// The ONLY contract the UI knows about. No fetch details leak in here. The concrete adapter
// (adapters/httpDataSource.ts) implements this interface; swapping it never needs a UI change.

export type Agent = { id: string; name: string; state: string; [k: string]: unknown };
export type AgentEvent = {
  id?: number;
  kind: string;
  taskId?: string;
  payload?: unknown;
  [k: string]: unknown;
};
// Live "what is this agent doing right now" feed: newest item last, capped ~20, every text
// field already truncated server-side. `label` is the tool name for tool_call/tool_result
// items. `text` can be null.
export type WorkerActivityItem = {
  ts: number;
  kind: "assistant" | "tool_call" | "tool_result" | "blocked";
  label: string | null;
  text: string | null;
};
export type WorkerActivity = {
  available: boolean;
  updatedAt: number | null;
  items: WorkerActivityItem[];
};
// One outward call waiting for a person (POST /gate from .claude/hooks/board_gate.py).
export type PendingApproval = {
  id: string;
  agent: string;
  tool: string;
  detail: string;
  created: number; // Unix seconds
  site: string | null; // the web site, for a page fetch
  warnings: string[]; // signs the call may carry data out; never allowed without a click
  job_allowable: boolean; // "allow for this job" is offered
};
// The current job (GET /run, then "run" events): what was asked, what the orchestrator asked each
// member, each member's final report, and the team's final answer. Times are Unix seconds.
export type RunView = {
  job: string | null;
  started: number;
  asked: Record<string, string>;
  reports: { agent: string; text: string; ts: number }[];
  final: string | null;
  final_at: number | null;
};
export type Decision = "allow" | "deny" | "allow-job";
// One approval outcome (GET /approvals, then "decision" events). Secrets in `detail` are redacted.
export type DecisionRecord = {
  ts: number; // Unix seconds
  decision: "allow" | "deny";
  how: string; // "clicked Allow", "site on your allow list", "no answer in time", ...
  agent: string;
  tool: string;
  detail: string;
  warnings: string[];
};
export type Unsubscribe = () => void;

export interface DataSource {
  getAgents(): Promise<Agent[]>;
  subscribe(onEvent: (event: AgentEvent) => void): Unsubscribe;
  // Live activity feed for one agent. Plain fetch (not push): callers poll it. Never rejects
  // for "nothing to show" (idle agent): resolves to `{available: false}`.
  getWorkerActivity(agentId: string): Promise<WorkerActivity>;
  // Approvals: the current list (later changes arrive as "gate" events), and the answer to one.
  getPendingApprovals(): Promise<PendingApproval[]>;
  getRun(): Promise<RunView>;
  getDecisions(): Promise<DecisionRecord[]>;
  decide(id: string, decision: Decision): Promise<void>;
}
