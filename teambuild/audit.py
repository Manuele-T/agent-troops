"""`team-build --audit [dir]`: check any repo's existing .claude/agents/*.md for the lethal trifecta,
and the rest of its Claude Code setup (hooks, MCP servers, skills, commands: setup_audit.py).

Offline, no LLM, no key. Works on agents this tool never generated.

Honest limits, repeated in the report: it reads configuration, not behaviour; "gated" is
self-declared in an agent's own text (Claude Code's approval prompt is the real gate); tools it
cannot classify (most MCP tools) are flagged, not guessed.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from teambuild.architect import TRIFECTA
from teambuild.setup_audit import setup_notes

BLUEPRINTS_PATH = Path(__file__).parent / "blueprints.json"

# Built-in tools a subagent inherits when its frontmatter has no `tools:` line.
INHERITED_BUILTINS = ["Read", "Grep", "Glob", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "Task"]
# Built-ins with no trifecta leg of their own (they write, delegate or track state).
NO_LEG_TOOLS = {"NotebookEdit", "Agent", "Task", "TodoWrite", "Skill", "SendMessage", "ExitPlanMode"}
SECRET_DENY = ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"]
BROAD_ALLOW = {"Read", "Bash", "Bash(*)", "WebFetch", "Write", "Edit"}
APPROVAL_RE = re.compile(r"approval", re.I)
HUMAN_RE = re.compile(r"human|explicit", re.I)


@dataclass
class RoleAudit:
    id: str
    path: str
    tools: list[str] | None          # None = no tools: line, inherits everything
    legs: set[str] = field(default_factory=set)
    gated: bool = False
    guarded: bool = False
    verdict: str = "PASS"            # PASS | WARN | FAIL
    notes: list[str] = field(default_factory=list)

    def bump(self, verdict: str) -> None:
        order = {"PASS": 0, "WARN": 1, "FAIL": 2}
        if order[verdict] > order[self.verdict]:
            self.verdict = verdict


@dataclass
class AuditResult:
    target: str
    roles: list[RoleAudit]
    repo_notes: list[tuple[str, str]]   # (level, text)
    skipped: list[str]

    @property
    def failed(self) -> bool:
        return any(r.verdict == "FAIL" for r in self.roles) or any(l == "FAIL" for l, _ in self.repo_notes)

    @property
    def overall(self) -> str:
        if not self.roles:
            if self.failed:
                return "FAIL"
            return "WARN" if any(l == "WARN" for l, _ in self.repo_notes) else "NO AGENTS"
        return "FAIL" if self.failed else ("WARN" if any(
            r.verdict == "WARN" for r in self.roles) or any(l == "WARN" for l, _ in self.repo_notes) else "PASS")


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

def _split_top_level(value: str) -> list[str]:
    items, depth, cur = [], 0, ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip())
    return items


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    """Flat key/value frontmatter, with YAML list and flow-list forms. None if there is none."""
    if not text.startswith("---"):
        return None
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?(.*)", text, re.S)
    if not m:
        return None
    fm_text, body = m.group(1), m.group(2)
    data: dict[str, Any] = {}
    lines = fm_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.startswith((" ", "\t", "-", "#")):
            i += 1
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value == "":
            items = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("- "):
                items.append(lines[j].lstrip()[2:].strip().strip("'\""))
                j += 1
            data[key] = items if items else ""
            i = j
            continue
        if value.startswith("[") and value.endswith("]"):
            data[key] = [x.strip().strip("'\"") for x in _split_top_level(value[1:-1])]
        else:
            data[key] = value.strip("'\"")
        i += 1
    data["_raw"] = fm_text
    return data, body


def _parse_tools(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [v for v in value if v]
    return _split_top_level(str(value))


def _base(tool: str) -> str:
    return tool.split("(", 1)[0].strip()


# --------------------------------------------------------------------------- #
# Repo-level facts                                                             #
# --------------------------------------------------------------------------- #

def _merge(a: Any, b: Any) -> Any:
    """Claude Code combines its settings files: objects merge key by key, lists (hook entries,
    permission rules) add up, and for anything else the later file wins."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {**a, **{k: _merge(a[k], v) if k in a else v for k, v in b.items()}}
    if isinstance(a, list) and isinstance(b, list):
        return a + [x for x in b if x not in a]
    return b


def _load_settings(claude_dir: Path) -> dict:
    merged: dict = {}
    for name in ("settings.json", "settings.local.json"):
        p = claude_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                merged = _merge(merged, data)
    return merged


def _guards(settings: dict, claude_dir: Path) -> dict[str, dict[str, Any]]:
    """role id -> {"allow": [...], "script_present": bool} from bash_guard.py hook entries."""
    out: dict[str, dict[str, Any]] = {}
    script_present = (claude_dir / "hooks" / "bash_guard.py").exists()
    for entry in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if "bash_guard.py" not in cmd:
                continue
            try:
                argv = shlex.split(cmd)
            except ValueError:
                continue
            role = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--role"), None)
            allow = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--allow"]
            if role:
                out[role] = {"allow": allow, "script_present": script_present}
    return out


def _read_fenced_roles(settings: dict, claude_dir: Path) -> set[str]:
    if not (claude_dir / "hooks" / "read_guard.py").exists():
        return set()
    roles: set[str] = set()
    for entry in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if "read_guard.py" in cmd:
                try:
                    argv = shlex.split(cmd)
                except ValueError:
                    continue
                for i, a in enumerate(argv[:-1]):
                    if a == "--roles":
                        roles |= {r for r in argv[i + 1].split(",") if r}
    return roles


def _board_gated_roles(settings: dict) -> set[str]:
    """Roles named by board_gate.py hook entries: their outward calls wait for Allow/Deny on the board."""
    roles: set[str] = set()
    for entry in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if "board_gate.py" not in cmd:
                continue
            try:
                argv = shlex.split(cmd)
            except ValueError:
                continue
            for i, a in enumerate(argv[:-1]):
                if a == "--roles":
                    roles |= {r for r in argv[i + 1].split(",") if r}
    return roles


def _flow_guards(settings: dict, claude_dir: Path) -> dict[str, bool]:
    """orchestrator id -> script present, from flow_guard.py hook entries."""
    present = (claude_dir / "hooks" / "flow_guard.py").exists()
    out: dict[str, bool] = {}
    for entry in (settings.get("hooks") or {}).get("PreToolUse", []) or []:
        for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
            cmd = h.get("command", "") if isinstance(h, dict) else ""
            if "flow_guard.py" not in cmd:
                continue
            try:
                argv = shlex.split(cmd)
            except ValueError:
                continue
            orch = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--orchestrator"), None)
            if orch:
                out[orch] = present
    return out


def team_relay_notes(roles: list[RoleAudit], flow_guards: dict[str, bool]) -> list[tuple[str, str]]:
    """Team-level check: no single agent may hold the trifecta, but a team can, with the orchestrator (or
    whatever session delegates) carrying text from a file-reading agent to a web agent. Groups agents by
    `<prefix>-orchestrator`; agents in no team form one group."""
    teams: dict[str, list[RoleAudit]] = {}
    orchestrators = [r.id for r in roles if r.id.endswith("-orchestrator")]
    for r in roles:
        owner = next((o for o in orchestrators if r.id.startswith(o[: -len("orchestrator")])), None)
        teams.setdefault(owner or "", []).append(r)
    notes: list[tuple[str, str]] = []
    for orch, members in sorted(teams.items()):
        private = [m.id for m in members if "private_data" in m.legs and m.id != orch]
        outbound = [m.id for m in members if not m.gated and {"untrusted_content", "exfiltration"} <= m.legs
                    and m.id != orch]
        if not (private and outbound):
            continue
        name = f"team {orch[: -len('-orchestrator')]}" if orch else "agents outside any team"
        if orch and orch in flow_guards:
            if flow_guards[orch]:
                notes.append(("INFO", f"{name}: one-way flow on (flow_guard.py): after {', '.join(private)} "
                                      f"has been used, {', '.join(outbound)} gets no more work in that session"))
            else:
                notes.append(("FAIL", f"{name}: a flow_guard hook is configured but .claude/hooks/flow_guard.py "
                                      "is missing, so the hook fails open"))
        else:
            notes.append(("WARN", f"{name}: relay open. No single agent holds the trifecta, but "
                                  f"{', '.join(outbound)} can reach the web and {', '.join(private)} can read "
                                  "files, and nothing stops a session passing data from one to the other"))
    return notes


def _claude_version() -> str | None:
    try:
        p = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #

def audit(target: str | Path, blueprints_path: str | Path | None = None) -> AuditResult:
    target = Path(target)
    data = json.loads(Path(blueprints_path or BLUEPRINTS_PATH).read_text(encoding="utf-8"))
    tool_legs = {k: v for k, v in data["tool_legs"].items() if not k.startswith("_")}
    guarded_legs = {k: v for k, v in data.get("guarded_tool_legs", {}).items() if not k.startswith("_")}

    claude_dir = target / ".claude"
    settings = _load_settings(claude_dir)
    guards = _guards(settings, claude_dir)

    roles: list[RoleAudit] = []
    skipped: list[str] = []
    agents_dir = claude_dir / "agents"
    for path in sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []:
        parsed = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        rel = str(path.relative_to(target))
        if parsed is None:
            skipped.append(f"{rel}: no frontmatter, Claude Code will not load it as an agent")
            continue
        fm, body = parsed
        role = RoleAudit(id=str(fm.get("name") or path.stem), path=rel, tools=_parse_tools(fm.get("tools")))
        text = f"{fm.get('description', '')}\n{body}"
        role.gated = bool(APPROVAL_RE.search(text) and HUMAN_RE.search(text))

        # -- which tools does it hold? ----------------------------------------- #
        if role.tools is None:
            disallowed = {_base(t) for t in (_parse_tools(fm.get("disallowedTools")) or [])}
            held = [t for t in INHERITED_BUILTINS if t not in disallowed]
            role.notes.append(
                "no `tools:` line, so it inherits every tool (Bash, WebFetch, Write, MCP tools...)"
                + (f" minus disallowedTools {sorted(disallowed)}" if disallowed else "")
            )
        else:
            held = [_base(t) for t in role.tools]

        guard = guards.get(role.id)
        role.guarded = bool(guard)
        unclassified = []
        for t in held:
            if t == "Bash" and guard and guard["script_present"] and guarded_legs.get("Bash"):
                role.legs |= set(guarded_legs["Bash"])
            elif t in tool_legs:
                role.legs |= set(tool_legs[t])
            elif t in NO_LEG_TOOLS:
                pass
            else:
                unclassified.append(t)
        if unclassified:
            role.bump("WARN")
            role.notes.append(
                "tool(s) this audit cannot classify: " + ", ".join(sorted(set(unclassified)))
                + " (usually MCP tools). Their legs are NOT counted, so check them by hand"
            )
        if role.tools is None:
            role.notes.append("also inherits every MCP tool the session has, which this audit cannot see")

        # -- bash guard state --------------------------------------------------- #
        if "Bash" in held and guard and not guard["script_present"]:
            role.bump("FAIL")
            role.notes.append(
                "a bash_guard hook is configured but .claude/hooks/bash_guard.py is missing, "
                "so the hook fails open"
            )
        if any(_base(t) == "Bash" and "(" in t for t in (role.tools or [])):
            role.notes.append(
                "a `Bash(...)` specifier in `tools:` does not narrow Bash: Claude Code treats it as the whole tool"
            )

        # -- verdict ------------------------------------------------------------ #
        holds_all = role.legs >= set(TRIFECTA)
        if holds_all:
            if role.gated:
                role.bump("WARN")
                role.notes.append(
                    "holds all three trifecta legs; relies on human approval, which the agent "
                    "declares in its own text and this tool cannot verify"
                )
            else:
                role.bump("FAIL")
                role.notes.append("holds all three trifecta legs (private data, untrusted content, exfiltration) with no approval gate")
        mode = str(fm.get("permissionMode", "")).strip()
        if mode == "bypassPermissions":
            if holds_all or role.gated or "Bash" in held:
                role.bump("FAIL")
                role.notes.append("permissionMode: bypassPermissions removes the approval prompt any gate depends on")
            else:
                role.bump("WARN")
                role.notes.append("permissionMode: bypassPermissions disables approval prompts")
        if any(_base(t) in ("Agent", "Task") and "(" in t for t in (role.tools or [])):
            role.notes.append("`Agent(...)` allowlist is enforced only when this agent is launched with `claude --agent`")
        roles.append(role)

    # -- repo-level checks ----------------------------------------------------- #
    repo_notes: list[tuple[str, str]] = []
    if roles:
        deny = (settings.get("permissions") or {}).get("deny", []) or []
        missing = [r for r in SECRET_DENY if r not in deny]
        if missing:
            repo_notes.append(("WARN", f"no deny rule for {', '.join(missing)}"))
        allow = (settings.get("permissions") or {}).get("allow", []) or []
        broad = [a for a in allow if a in BROAD_ALLOW]
        if broad:
            repo_notes.append(("WARN", f"permissions.allow pre-approves {', '.join(broad)}, which removes the prompt that guards every role"))
        fenced = _read_fenced_roles(settings, claude_dir)
        if fenced:
            repo_notes.append(("INFO", f"read fence on for: {', '.join(sorted(fenced))} (read_guard.py)"))
        else:
            repo_notes.append((
                "INFO",
                "read fence off: Claude Code asks before reading outside the project unless Read is "
                "pre-approved. `team-build --fence-reads` blocks it for the team's roles",
            ))
        repo_notes += team_relay_notes(roles, _flow_guards(settings, claude_dir))
        board_gated = _board_gated_roles(settings)
        if board_gated:
            if (claude_dir / "hooks" / "board_gate.py").exists():
                repo_notes.append(("INFO", f"board approvals on for: {', '.join(sorted(board_gated))} "
                                           "(board_gate.py): their web and shell calls wait for Allow/Deny on the board "
                                           "when it is running, and for Claude Code's own prompt when it is not"))
            else:
                repo_notes.append(("WARN", "a board_gate hook is configured but .claude/hooks/board_gate.py is missing: "
                                           "those roles' web and shell calls will fail"))
        for rid in guards:
            if rid not in {r.id for r in roles}:
                repo_notes.append(("WARN", f"bash_guard hook names role '{rid}', but no agent file has that name"))
    repo_notes += setup_notes(target)
    return AuditResult(str(target), roles, repo_notes, skipped)


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #

def _legs_str(legs: set[str]) -> str:
    return ", ".join(l for l in TRIFECTA if l in legs) or "none"


def _tools_str(role: RoleAudit) -> str:
    return "(inherits all)" if role.tools is None else (", ".join(role.tools) or "none")


def format_text(result: AuditResult) -> str:
    if not result.roles:
        lines = [f"No agents found in {result.target}/.claude/agents/"
                 + ("." if result.repo_notes else " - nothing to audit.")]
        lines += [f"  [{level}] repo: {text}" for level, text in result.repo_notes]
        lines += [f"  skipped: {s}" for s in result.skipped]
        if result.repo_notes:
            lines += ["", f"overall: {result.overall}"]
        return "\n".join(lines)
    out = [f"agent audit: {result.target}", ""]
    for r in result.roles:
        flags = ("gated " if r.gated else "") + ("guarded" if r.guarded else "")
        out.append(f"  [{r.verdict}] {r.id}")
        out.append(f"        tools : {_tools_str(r)}")
        out.append(f"        legs  : {_legs_str(r.legs)}{'   (' + flags.strip() + ')' if flags.strip() else ''}")
        out += [f"        - {n}" for n in r.notes]
    for level, text in result.repo_notes:
        out.append(f"  [{level}] repo: {text}")
    for s in result.skipped:
        out.append(f"  [skip] {s}")
    out += ["", f"overall: {result.overall}"]
    return "\n".join(out)


def render_report(result: AuditResult, claude_version: str | None = None,
                  approvals: list[dict] | None = None) -> str:
    """The Markdown report. `approvals`: decisions from the approval log for this folder, if any were
    looked up (the caller reads the log; this function stays pure)."""
    v = claude_version if claude_version is not None else _claude_version()
    lines = [
        "# Agent permission audit",
        "",
        f"- **Date:** {date.today().isoformat()}",
        f"- **Target:** `{result.target}`",
        f"- **Claude Code:** {v or 'not detected'}",
        f"- **Tool:** `team-build --audit` (offline, deterministic, no model involved)",
        f"- **Overall result:** **{result.overall}**",
        "",
        "## What was checked",
        "",
        "For each agent in `.claude/agents/`, the tools it can use are mapped to the three legs of the "
        "\"lethal trifecta\": **private data** (can read your files or inbox), **untrusted content** "
        "(ingests text an attacker can control) and **exfiltration** (can send data off the machine). "
        "An agent holding all three can be steered by injected text into leaking. `FAIL` = all three "
        "with no approval gate; `WARN` = all three but self-declared human approval, or something "
        "this audit could not classify.",
        "",
        "The rest of the project's Claude Code setup is checked too: hooks (one that can change Claude Code's "
        "settings or permissions, answers prompts with \"allow\", downloads and runs code or contacts another "
        "computer), MCP servers in `.mcp.json` (remote, unpinned packages, secrets written into the file), and "
        "skills, commands and agent files (hidden comments, invisible characters, instruction-override wording, "
        "commands that run as soon as they are used).",
        "",
        "## Agents",
        "",
        "| Agent | Tools | Legs held | Gated | Bash guarded | Verdict |",
        "|---|---|---|---|---|---|",
    ]
    for r in result.roles:
        lines.append(
            f"| `{r.id}` | {_tools_str(r)} | {_legs_str(r.legs)} | {'yes' if r.gated else 'no'} | "
            f"{'yes' if r.guarded else 'no'} | **{r.verdict}** |"
        )
    findings = [(r.id, n) for r in result.roles for n in r.notes]
    if findings or result.repo_notes:
        lines += ["", "## Findings", ""]
        lines += [f"- `{rid}`: {n}" for rid, n in findings]
        lines += [f"- **repo** ({lvl}): {txt}" for lvl, txt in result.repo_notes]
    if result.skipped:
        lines += ["", "## Skipped files", ""] + [f"- {s}" for s in result.skipped]
    if approvals is not None:
        from teambuild.approval_log import render_markdown
        lines += ["", render_markdown(approvals, Path(result.target)).rstrip()]
    lines += [
        "",
        "## Limits of this audit",
        "",
        "- It reads configuration, not behaviour. It does not observe what an agent actually does.",
        "- \"Gated\" is detected from the agent's own text mentioning human approval. Claude Code's "
        "permission prompt is the real gate, and it is removed by `bypassPermissions` or a broad "
        "`permissions.allow` rule.",
        "- Tools it cannot classify (most MCP tools) are listed but their legs are not counted.",
        "- The hook, MCP, skill and command checks are pattern checks: they catch the obvious cases and can miss "
        "a well-disguised one. Plugins and user-level settings (`~/.claude`) are outside this audit.",
        "- Text can flow between agents through whatever delegates to them. The team-level check flags a "
        "team whose agents together hold all three legs; `flow_guard.py` closes that path only for the "
        "team it names, and only when the team is started as its orchestrator (`claude --agent`). "
        "Anything private in the orchestrator's own starting context (the request, `CLAUDE.md`) is outside it.",
        "",
    ]
    return "\n".join(lines)
