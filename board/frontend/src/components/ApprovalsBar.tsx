import { useState } from "react";
import type { DataSource, Decision, PendingApproval } from "../datasource";

const VERB: Record<string, string> = {
  WebFetch: "open a web page",
  WebSearch: "search the web",
  Bash: "run a command",
};

// Outward calls waiting for a person. Each one is held by .claude/hooks/board_gate.py until a button
// here is clicked; no answer within a few minutes counts as Deny. Calls with warnings (signs of data
// being carried out) are always asked, never covered by an allow list or "for this job".
export default function ApprovalsBar({ approvals, source }: { approvals: PendingApproval[]; source: DataSource }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (approvals.length === 0) return null;

  async function answer(id: string, decision: Decision) {
    setBusy(id);
    setError(null);
    try {
      await source.decide(id, decision);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="approvals" aria-label="Waiting for your approval">
      <h2>Waiting for your approval ({approvals.length})</h2>
      {approvals.map((a) => (
        <div key={a.id} className={`approval-card${a.warnings.length ? " approval-flagged" : ""}`}>
          <div className="approval-body">
            <p className="approval-what">
              <strong>{a.agent}</strong> wants to {VERB[a.tool] ?? `use ${a.tool}`}:
            </p>
            <code className="approval-detail">{a.detail || "(no details)"}</code>
            {a.warnings.length > 0 && (
              <ul className="approval-warnings">
                {a.warnings.map((w) => (
                  <li key={w}>⚠ {w}</li>
                ))}
              </ul>
            )}
          </div>
          <div className="approval-buttons">
            <button type="button" className="btn-approve" disabled={busy !== null} onClick={() => answer(a.id, "allow")}>
              Allow
            </button>
            {a.job_allowable && (
              <button type="button" className="btn-approve-job" disabled={busy !== null} onClick={() => answer(a.id, "allow-job")}>
                {a.tool === "WebSearch" ? "Allow searches for this job" : `Allow ${a.site} for this job`}
              </button>
            )}
            <button type="button" className="btn-danger" disabled={busy !== null} onClick={() => answer(a.id, "deny")}>
              Deny
            </button>
          </div>
        </div>
      ))}
      {error && <p className="action-result err">✗ {error}</p>}
    </section>
  );
}
