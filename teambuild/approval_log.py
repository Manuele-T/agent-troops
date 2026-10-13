"""The approval log: every decision on an outward call, kept as evidence (maker-checker).

The board appends one JSON line per decision to $XDG_STATE_HOME/agent-troops/approvals.jsonl
(default ~/.local/state/...): a person's click, an automatic allow or block from the site lists or
"allow for this job", or a timeout. The folder is private (0700) and the file 0600; it lives outside
every project, so it is never committed by accident. Secrets in the call are redacted before they
are written (teambuild/board/policy.py redact()). The file is rotated to approvals.jsonl.1 at 5 MB.

`team-build --approvals [DIR] [--report FILE]` reads it back; `team-build --audit DIR --report FILE`
includes the decisions recorded for that folder. Stdlib only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROTATE_BYTES = 5 * 1024 * 1024
FIELDS = ("ts", "decision", "how", "agent", "tool", "detail", "site", "warnings", "cwd", "policy")


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "agent-troops" / "approvals.jsonl"


def append(entry: dict, path: Path | None = None) -> None:
    """Add one decision. Never raises: the log must not break an approval."""
    path = path or log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists() and path.stat().st_size > ROTATE_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        line = json.dumps({k: entry.get(k) for k in FIELDS}, ensure_ascii=False) + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _within(cwd: object, folder: Path) -> bool:
    if not isinstance(cwd, str) or not cwd:
        return False
    try:
        c = Path(cwd).resolve()
    except (OSError, RuntimeError):
        return False
    return c == folder or c.is_relative_to(folder)


def read(folder: Path | None = None, limit: int | None = None, path: Path | None = None) -> list[dict]:
    """Decisions, oldest first; only those made for `folder` (or inside it) when given."""
    path = path or log_path()
    folder = folder.resolve() if folder else None
    out: list[dict] = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict) and (folder is None or _within(e.get("cwd"), folder)):
                out.append(e)
    out.sort(key=lambda e: e.get("ts") or 0)
    return out[-limit:] if limit else out


def _when(ts: object) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if isinstance(ts, (int, float)) else "?"


def _cell(value: object, n: int = 160) -> str:
    text = " ".join(str(value or "").split()).replace("|", "\\|")
    return text if len(text) <= n else text[: n - 1] + "…"


def summary(entries: list[dict]) -> str:
    allowed = sum(1 for e in entries if e.get("decision") == "allow")
    by_person = sum(1 for e in entries if str(e.get("how", "")).startswith("clicked"))
    return (f"{len(entries)} decision(s): {allowed} allowed, {len(entries) - allowed} denied; "
            f"{by_person} by a person, {len(entries) - by_person} by a rule or timeout")


def format_text(entries: list[dict]) -> str:
    if not entries:
        return "No approval decisions recorded."
    lines = [summary(entries)]
    for e in entries:
        flag = " [warnings]" if e.get("warnings") else ""
        lines.append(f"  {_when(e.get('ts'))}  {str(e.get('decision', '?')).upper():5}  {e.get('agent')}  "
                     f"{e.get('tool')}  {_cell(e.get('detail'), 90)}  ({e.get('how')}){flag}")
    return "\n".join(lines)


def render_markdown(entries: list[dict], folder: Path | None = None) -> str:
    where = f" for `{folder}`" if folder else ""
    out = [f"## Approval decisions{where}", "",
           "Every outward call a team member made (web search, web fetch, publish command) and who or what decided it. "
           "Secrets in the calls are redacted. Source: the board's approval log "
           f"(`{log_path()}`).", ""]
    if not entries:
        return "\n".join(out + ["No decisions recorded.", ""])
    out += [summary(entries) + ".", "",
            "| When | Decision | Decided by | Member | Tool | Call | Warnings | Repository policy |",
            "|---|---|---|---|---|---|---|---|"]
    for e in entries:
        out.append(f"| {_when(e.get('ts'))} | {_cell(e.get('decision'))} | {_cell(e.get('how'))} | {_cell(e.get('agent'))} | "
                   f"{_cell(e.get('tool'))} | {_cell(e.get('detail'))} | {_cell('; '.join(e.get('warnings') or []))} | "
                   f"{_cell(e.get('policy') or 'none')} |")
    return "\n".join(out + [""])
