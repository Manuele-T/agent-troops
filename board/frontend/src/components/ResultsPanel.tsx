import type { DecisionRecord, RunView } from "../datasource";
import Markdown from "./Markdown";

function clock(sec: number): string {
  return new Date(sec * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// The job and what came out of it, as readable text: the team's final answer first, then each
// member's own report (newest first, folded). Everything here arrives through hooks; the board
// never reads transcript files.
function Decisions({ decisions }: { decisions: DecisionRecord[] }) {
  if (decisions.length === 0) return null;
  const list = [...decisions].reverse();
  const allowed = decisions.filter((d) => d.decision === "allow").length;
  return (
    <details className="results-decisions">
      <summary>
        <strong>Approval decisions</strong>{" "}
        <span className="muted">· {allowed} allowed, {decisions.length - allowed} denied (kept in your approval log)</span>
      </summary>
      <ul className="decision-list">
        {list.map((d, i) => (
          <li key={`${d.ts}-${i}`} className={`decision decision-${d.decision}`}>
            <span className="decision-mark">{d.decision === "allow" ? "✓" : "✗"}</span>
            <span className="muted">{clock(d.ts)}</span> <strong>{d.agent}</strong> {d.tool}{" "}
            <code>{d.detail.length > 120 ? `${d.detail.slice(0, 119)}…` : d.detail}</code>{" "}
            <span className="muted">— {d.how}</span>
            {d.warnings.length > 0 && <span className="decision-warn"> ⚠ {d.warnings.length} warning(s)</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

export default function ResultsPanel({ run, decisions }: { run: RunView | null; decisions: DecisionRecord[] }) {
  if (!run || (!run.job && !run.final && run.reports.length === 0)) {
    return (
      <section className="results results-empty">
        <p className="muted">The job you give the team, each member&apos;s report and the final answer appear here.</p>
        <Decisions decisions={decisions} />
      </section>
    );
  }
  const reports = [...run.reports].reverse();
  return (
    <section className="results" aria-label="Results">
      {run.job && (
        <div className="results-job">
          <h2>Job</h2>
          <p>{run.job}</p>
        </div>
      )}
      <div className="results-final">
        <h2>Answer {run.final_at ? <span className="muted">· {clock(run.final_at)}</span> : null}</h2>
        {run.final ? <Markdown text={run.final} /> : <p className="muted">The team is still working…</p>}
      </div>
      {reports.length > 0 && (
        <div className="results-reports">
          <h2>Team members&apos; reports</h2>
          {reports.map((r, i) => (
            <details key={`${r.agent}-${r.ts}`} open={i === 0 && !run.final}>
              <summary>
                <strong>{r.agent}</strong> <span className="muted">· {clock(r.ts)}</span>
              </summary>
              <Markdown text={r.text} />
            </details>
          ))}
        </div>
      )}
      <Decisions decisions={decisions} />
    </section>
  );
}
