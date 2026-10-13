import { API_BASE } from "../config";
import type {
  Agent,
  AgentEvent,
  DataSource,
  Decision,
  DecisionRecord,
  PendingApproval,
  RunView,
  Unsubscribe,
  WorkerActivity,
} from "../datasource";

// The per-launch token the board server writes into the page; only approval decisions need it.
function boardToken(): string {
  return document.querySelector('meta[name="board-token"]')?.getAttribute("content") ?? "";
}

// Concrete adapter: the ONLY place that knows the transport (HTTP + SSE). UI
// components never see this — they depend on the DataSource interface only.
export class HttpDataSource implements DataSource {
  async getAgents(): Promise<Agent[]> {
    const r = await fetch(`${API_BASE}/agents`);
    if (!r.ok) throw new Error(`GET /agents -> ${r.status}`);
    return r.json();
  }

  subscribe(onEvent: (event: AgentEvent) => void): Unsubscribe {
    const es = new EventSource(`${API_BASE}/events`);
    // Connection state is surfaced through the same channel as synthetic events
    // (kind "__open"/"__error") so we don't have to widen the interface.
    es.onopen = () => onEvent({ kind: "__open" });
    es.onerror = () => onEvent({ kind: "__error" }); // EventSource auto-reconnects
    es.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data) as AgentEvent);
      } catch {
        // keepalive comments never reach onmessage; ignore any stray parse error
      }
    };
    return () => es.close();
  }

  async getWorkerActivity(agentId: string): Promise<WorkerActivity> {
    const r = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/activity`);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(
        (body as { detail?: string }).detail ?? `GET /agents/${agentId}/activity -> ${r.status}`,
      );
    }
    return r.json();
  }

  async getPendingApprovals(): Promise<PendingApproval[]> {
    const r = await fetch(`${API_BASE}/gate`);
    if (!r.ok) throw new Error(`GET /gate -> ${r.status}`);
    return ((await r.json()) as { pending: PendingApproval[] }).pending;
  }

  async getRun(): Promise<RunView> {
    const r = await fetch(`${API_BASE}/run`);
    if (!r.ok) throw new Error(`GET /run -> ${r.status}`);
    return r.json();
  }

  async getDecisions(): Promise<DecisionRecord[]> {
    const r = await fetch(`${API_BASE}/approvals`);
    if (!r.ok) throw new Error(`GET /approvals -> ${r.status}`);
    return ((await r.json()) as { decisions: DecisionRecord[] }).decisions;
  }

  async decide(id: string, decision: Decision): Promise<void> {
    const r = await fetch(`${API_BASE}/gate/${encodeURIComponent(id)}/${decision}`, {
      method: "POST",
      headers: { "X-Board-Token": boardToken() },
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error((body as { detail?: string }).detail ?? `answer failed (${r.status})`);
    }
  }
}
