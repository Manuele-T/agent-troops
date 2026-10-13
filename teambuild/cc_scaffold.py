"""Roster JSON -> .claude/ files. Supports --dry-run and --destroy.

Writes, per roster:
    .claude/agents/<role-id>.md         (identity, boundaries, pipeline — from agent_md.py)
    .claude/hooks/emit.py               (copied verbatim, once, shared across teams)
    .claude/hooks/bash_guard.py         (same, only when a role's Bash is allowlisted; wired in
                                          settings.json per role, keyed on the hook's agent_type)
    .claude/hooks/read_guard.py         (same, only with --fence-reads: team roles' Read/Grep/Glob
                                          stay inside the project)
    .claude/hooks/flow_guard.py         (same, whenever the team has a web member and a file-reading
                                          member: once private data is read, no more web)
    .claude/hooks/board_gate.py         (same, whenever a role can reach outside the computer: its
                                          web or shell calls wait for Allow/Deny on the board)
    .claude/settings.json               (deep-merged: deny rules + hook wiring)
    .claude/.teambuild-manifest.json    (every file + settings key this tool added,
                                          per team prefix — what makes destroy safe)

Multiple teams can be scaffolded into the same target repo: the manifest tracks each
team's own file list and settings additions, and destroy() only removes an addition if
no other team's entry still needs it.

Deliberately no .claude/skills/<role-id>/SKILL.md. Earlier this project scaffolded a
Skill alongside each Agent, giving both the same name/description. A live Phase 4 test
showed Claude Code's top-level thread can resolve a role via that unrestricted Skill
path instead of the tool-scoped Agent path — Skills run in the invoking context with no
isolation, so this silently bypassed every tools: allowlist in the roster. See
docs/shakeout-log.md. The pipeline content that used to live in SKILL.md is now a
section of the agent .md itself (agent_md.py) — one file, one properly-scoped path.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

from teambuild.templates.agent_md import render_agent_md

MANIFEST_NAME = ".teambuild-manifest.json"
EMIT_TEMPLATE_PATH = Path(__file__).parent / "templates" / "emit.py"
GUARD_TEMPLATE_PATH = Path(__file__).parent / "templates" / "bash_guard.py"
READ_GUARD_TEMPLATE_PATH = Path(__file__).parent / "templates" / "read_guard.py"
FLOW_GUARD_TEMPLATE_PATH = Path(__file__).parent / "templates" / "flow_guard.py"
BOARD_GATE_TEMPLATE_PATH = Path(__file__).parent / "templates" / "board_gate.py"
BOARD_GATED_TOOLS = ("WebFetch", "WebSearch", "Bash")     # the tools that can send data off the machine
BOARD_GATE_TIMEOUT = 600      # seconds Claude Code waits for the hook; board_gate.py gives up at 570
DEFAULT_BLUEPRINTS_PATH = Path(__file__).parent / "blueprints.json"

# The interpreter named in hook commands. Windows installs usually have `python` (or `py`), not `python3`.
# The hook scripts are stdlib-only, so any Python 3 works. Not yet run on Windows: see the README, "What has been verified".
PYTHON = "python" if os.name == "nt" else "python3"
HOOK_COMMAND = f"{PYTHON} .claude/hooks/emit.py"
HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop", "Stop")


# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

def _agent_rel(role_id: str) -> str:
    return f".claude/agents/{role_id}.md"


def _hook_rel() -> str:
    return ".claude/hooks/emit.py"


def _guard_rel() -> str:
    return ".claude/hooks/bash_guard.py"


def _read_guard_rel() -> str:
    return ".claude/hooks/read_guard.py"


def _flow_guard_rel() -> str:
    return ".claude/hooks/flow_guard.py"


def _settings_path(target_dir: Path) -> Path:
    return target_dir / ".claude" / "settings.json"


def _manifest_path(target_dir: Path) -> Path:
    return target_dir / ".claude" / MANIFEST_NAME


# --------------------------------------------------------------------------- #
# Data loading                                                                 #
# --------------------------------------------------------------------------- #

def _load_role_primitives(blueprints_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(blueprints_path) if blueprints_path else DEFAULT_BLUEPRINTS_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["role_primitives"]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# settings.json merge                                                          #
# --------------------------------------------------------------------------- #

def guard_command(role_id: str, bash_allow: list[str]) -> str:
    """Settings-level PreToolUse command that limits one role's Bash to bash_allow. Runs for
    every Bash call in the repo but only objects when the event's agent_type is role_id."""
    for entry in bash_allow:
        if any(c in entry for c in "'\"\\$`"):
            raise ValueError(f"bash_allow entry not safe to embed: {entry!r}")
    allow_args = " ".join(f"--allow '{entry}'" for entry in bash_allow)
    return (
        f'{PYTHON} "$CLAUDE_PROJECT_DIR/.claude/hooks/bash_guard.py" --role {role_id} {allow_args}'
    )


def read_guard_command(role_ids: str) -> str:
    """Settings-level PreToolUse command that keeps the named team roles' Read/Grep/Glob inside
    the project. Objects to nothing unless the event's agent_type is one of role_ids."""
    return f'{PYTHON} "$CLAUDE_PROJECT_DIR/.claude/hooks/read_guard.py" --roles {role_ids}'


def flow_guard_command(orchestrator: str, private: list[str], outbound: list[str], mode: str) -> str:
    """Settings-level PreToolUse command for the one-way-flow guard. Judges only the orchestrator's
    delegations (agent_type), so the user's own sessions in the repo are untouched."""
    if mode not in ("block", "ask"):
        raise ValueError(f"flow guard mode must be block or ask, not {mode!r}")
    return (
        f'{PYTHON} "$CLAUDE_PROJECT_DIR/.claude/hooks/flow_guard.py" --orchestrator {orchestrator} '
        f'--private {",".join(private)} --outbound {",".join(outbound)} --mode {mode}'
    )


def _board_gate_rel() -> str:
    return ".claude/hooks/board_gate.py"


def board_gate_command(role_ids: list[str]) -> str:
    """Settings-level PreToolUse command that sends the named roles' outward calls to the board."""
    return f'{PYTHON} "$CLAUDE_PROJECT_DIR/.claude/hooks/board_gate.py" --roles {",".join(role_ids)}'


def board_gated_roles(roster: dict) -> list[str]:
    """Team roles holding a tool that can reach outside the computer (never the orchestrator)."""
    return [r["id"] for r in roster["roles"]
            if r.get("primitive") != "orchestrator" and set(r.get("tools", [])) & set(BOARD_GATED_TOOLS)]


def team_flow(roster: dict) -> tuple[str, list[str], list[str], str] | None:
    """(orchestrator, private, outbound, mode) if this team needs the flow guard, else None."""
    from teambuild.architect import Architect      # local: the scaffolder stays importable on its own

    mode = roster.get("flow_guard", "block")
    if mode == "off":
        return None
    private, outbound = Architect(DEFAULT_BLUEPRINTS_PATH).flow_roles(roster)
    orchestrator = next((r["id"] for r in roster["roles"] if r.get("primitive") == "orchestrator"), None)
    if not (private and outbound and orchestrator):
        return None
    return orchestrator, private, outbound, mode


def _guarded_roles(roster: dict, primitives: dict[str, Any]) -> list[tuple[str, list[str]]]:
    out = []
    for role in roster["roles"]:
        allow = primitives.get(role["primitive"], {}).get("bash_allow")
        if allow and "Bash" in role["tools"]:
            out.append((role["id"], list(allow)))
    return out


def _build_settings_patch(
    roster: dict, primitives: dict[str, Any] | None = None, fence_reads: bool = False
) -> dict:
    deny = list(roster.get("deny_rules", []))
    hooks = {}
    for event in HOOK_EVENTS:
        entry = {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}
        if event in ("PreToolUse", "PostToolUse"):          # the only events here that take a matcher
            entry = {"matcher": "*", **entry}
        hooks[event] = [entry]
    for role_id, allow in _guarded_roles(roster, primitives or {}):
        hooks["PreToolUse"].append({
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": guard_command(role_id, allow)}],
        })
    if fence_reads:
        role_ids = ",".join(r["id"] for r in roster["roles"])
        hooks["PreToolUse"].append({
            "matcher": "Read|Grep|Glob",
            "hooks": [{"type": "command", "command": read_guard_command(role_ids)}],
        })
    flow = team_flow(roster)
    if flow:
        hooks["PreToolUse"].append({
            "matcher": "Agent|Task|SendMessage",
            "hooks": [{"type": "command", "command": flow_guard_command(*flow)}],
        })
    gated = board_gated_roles(roster)
    if gated:
        hooks["PreToolUse"].append({
            "matcher": "|".join(BOARD_GATED_TOOLS),
            "hooks": [{"type": "command", "command": board_gate_command(gated), "timeout": BOARD_GATE_TIMEOUT}],
        })
    return {"permissions": {"deny": deny}, "hooks": hooks}


def _deep_merge_settings(existing: dict, patch: dict) -> tuple[dict, list[str]]:
    """Deep-merge patch into existing without clobbering existing keys.

    Returns (merged_settings, settings_additions) where settings_additions is the list
    of "kind:value" strings this call newly added — recorded in the manifest so destroy
    can remove exactly these, and nothing the user (or another team) already had.
    """
    merged = copy.deepcopy(existing)
    added: list[str] = []

    merged.setdefault("permissions", {})
    deny = list(merged["permissions"].get("deny", []))
    for rule in patch.get("permissions", {}).get("deny", []):
        if rule not in deny:
            deny.append(rule)
            added.append(f"permissions.deny:{rule}")
    merged["permissions"]["deny"] = deny

    merged.setdefault("hooks", {})
    for event, entries in patch.get("hooks", {}).items():
        existing_entries = merged["hooks"].setdefault(event, [])
        for entry in entries:
            command = entry["hooks"][0]["command"]
            already_present = any(
                h.get("command") == command
                for existing_entry in existing_entries
                for h in existing_entry.get("hooks", [])
            )
            if not already_present:
                existing_entries.append(copy.deepcopy(entry))
                added.append(f"hooks.{event}:{command}")

    return merged, added


def _required_settings_keys(patch: dict) -> list[str]:
    """The settings entries a team's own patch requires to exist, independent of
    whether another team already put them there first. This — not the merge-time
    diff — is what the manifest should record, so destroy can tell a shared
    requirement from one only this team needed.
    """
    keys = [f"permissions.deny:{rule}" for rule in patch.get("permissions", {}).get("deny", [])]
    for event, entries in patch.get("hooks", {}).items():
        for entry in entries:
            for h in entry.get("hooks", []):
                keys.append(f"hooks.{event}:{h['command']}")
    return keys


def _remove_settings_additions(settings: dict, additions: list[str]) -> dict:
    for item in additions:
        kind, _, value = item.partition(":")
        if kind == "permissions.deny":
            deny = settings.get("permissions", {}).get("deny", [])
            if value in deny:
                deny.remove(value)
        elif kind.startswith("hooks."):
            event = kind.split(".", 1)[1]
            entries = settings.get("hooks", {}).get(event, [])
            settings.setdefault("hooks", {})[event] = [
                e for e in entries
                if not any(h.get("command") == value for h in e.get("hooks", []))
            ]
    return settings


# --------------------------------------------------------------------------- #
# Scaffold                                                                     #
# --------------------------------------------------------------------------- #

def scaffold(
    roster: dict,
    target_dir: str | Path,
    dry_run: bool = False,
    primitives: dict[str, Any] | None = None,
    fence_reads: bool = False,
) -> list[str]:
    target_dir = Path(target_dir)
    prefix = roster["team_prefix"]
    if primitives is None:
        primitives = _load_role_primitives()

    actions: list[str] = []
    files_written: list[str] = []

    all_role_ids = [r["id"] for r in roster["roles"]]
    needs_guard = bool(_guarded_roles(roster, primitives))
    flow = team_flow(roster)
    for role in roster["roles"]:
        role_id = role["id"]
        boundary_text = primitives[role["primitive"]]["boundary_text"]
        sibling_ids = [rid for rid in all_role_ids if rid != role_id]
        agent_md = render_agent_md(role, boundary_text, sibling_ids, roster.get("guardrail"),
                                   (flow[1], flow[2]) if flow else None)

        agent_rel = _agent_rel(role_id)

        if dry_run:
            actions.append(f"[dry-run] write {agent_rel} ({len(agent_md)} chars)")
        else:
            agent_path = target_dir / agent_rel
            agent_path.parent.mkdir(parents=True, exist_ok=True)
            agent_path.write_text(agent_md, encoding="utf-8")

            actions.append(f"wrote {agent_rel}")
        files_written.append(agent_rel)

    # hooks/emit.py — copied verbatim, once. Shared across teams in the same repo.
    hook_rel = _hook_rel()
    hook_path = target_dir / hook_rel
    if not hook_path.exists():
        if dry_run:
            actions.append(f"[dry-run] write {hook_rel}")
        else:
            hook_path.parent.mkdir(parents=True, exist_ok=True)
            hook_path.write_text(
                EMIT_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            actions.append(f"wrote {hook_rel}")
    files_written.append(hook_rel)

    # hooks/bash_guard.py — only when some role's Bash is allowlisted. Shared, like emit.py.
    if needs_guard:
        guard_rel = _guard_rel()
        guard_path = target_dir / guard_rel
        if not guard_path.exists():
            if dry_run:
                actions.append(f"[dry-run] write {guard_rel}")
            else:
                guard_path.parent.mkdir(parents=True, exist_ok=True)
                guard_path.write_text(
                    GUARD_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8"
                )
                actions.append(f"wrote {guard_rel}")
        files_written.append(guard_rel)

    # hooks/read_guard.py — only with --fence-reads. Shared, like the other hook scripts.
    if fence_reads:
        rg_rel = _read_guard_rel()
        rg_path = target_dir / rg_rel
        if not rg_path.exists():
            if dry_run:
                actions.append(f"[dry-run] write {rg_rel}")
            else:
                rg_path.parent.mkdir(parents=True, exist_ok=True)
                rg_path.write_text(READ_GUARD_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                actions.append(f"wrote {rg_rel}")
        files_written.append(rg_rel)

    # hooks/flow_guard.py — whenever the team has a web member and a file-reading member. Shared.
    if flow:
        fg_rel = _flow_guard_rel()
        fg_path = target_dir / fg_rel
        if not fg_path.exists():
            if dry_run:
                actions.append(f"[dry-run] write {fg_rel}")
            else:
                fg_path.parent.mkdir(parents=True, exist_ok=True)
                fg_path.write_text(FLOW_GUARD_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                actions.append(f"wrote {fg_rel}")
        files_written.append(fg_rel)

    # hooks/board_gate.py — whenever a role can reach outside the computer. Shared.
    if board_gated_roles(roster):
        bg_rel = _board_gate_rel()
        bg_path = target_dir / bg_rel
        if not bg_path.exists():
            if dry_run:
                actions.append(f"[dry-run] write {bg_rel}")
            else:
                bg_path.parent.mkdir(parents=True, exist_ok=True)
                bg_path.write_text(BOARD_GATE_TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                actions.append(f"wrote {bg_rel}")
        files_written.append(bg_rel)

    # settings.json — deep-merge, never clobber existing keys.
    settings_path = _settings_path(target_dir)
    settings_file_created = not settings_path.exists()
    existing_settings = _read_json(settings_path, {})
    patch = _build_settings_patch(roster, primitives, fence_reads)
    merged_settings, newly_added = _deep_merge_settings(existing_settings, patch)
    # what this team's presence requires — not just what was novel at merge time,
    # so a rule/hook shared with an earlier team is still tracked as this team's too.
    settings_required = _required_settings_keys(patch)

    if dry_run:
        actions.append(
            f"[dry-run] settings.json += {newly_added}"
            if newly_added
            else "[dry-run] settings.json unchanged"
        )
    else:
        _write_json(settings_path, merged_settings)
        actions.append(f"merged settings.json (+{len(newly_added)} keys)")

    # manifest — the single source of truth destroy() reads.
    manifest_path = _manifest_path(target_dir)
    if dry_run:
        actions.append(f"[dry-run] update manifest .claude/{MANIFEST_NAME}")
    else:
        manifest = _read_json(manifest_path, {"teams": {}})
        manifest.setdefault("teams", {})[prefix] = {
            "blueprint_id": roster.get("blueprint_id"),
            "files": files_written,
            "settings_additions": settings_required,
            "settings_file_created": settings_file_created,
        }
        _write_json(manifest_path, manifest)
        actions.append(f"wrote manifest .claude/{MANIFEST_NAME}")

    return actions


# --------------------------------------------------------------------------- #
# Destroy                                                                      #
# --------------------------------------------------------------------------- #

def destroy(prefix: str, target_dir: str | Path, dry_run: bool = False) -> list[str]:
    target_dir = Path(target_dir)
    actions: list[str] = []

    manifest_path = _manifest_path(target_dir)
    manifest = _read_json(manifest_path, None)
    if manifest is None:
        return actions

    teams = manifest.get("teams", {})
    team = teams.pop(prefix, None)
    if team is None:
        return actions

    # anything still referenced by another team must survive
    remaining_files = {f for t in teams.values() for f in t.get("files", [])}
    remaining_additions = {a for t in teams.values() for a in t.get("settings_additions", [])}

    for rel in team.get("files", []):
        if rel in remaining_files:
            continue
        path = target_dir / rel
        if dry_run:
            actions.append(f"[dry-run] remove {rel}")
            continue
        if not path.exists():
            continue
        path.unlink()
        actions.append(f"removed {rel}")
        parent = path.parent
        if parent.name not in ("agents", "hooks") and parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    additions_to_remove = [a for a in team.get("settings_additions", []) if a not in remaining_additions]
    # only delete settings.json outright if THIS team is the one that created it and
    # no other team remains to need it — otherwise selectively drop just its keys,
    # preserving anything the user (or another team) had in there.
    drop_whole_file = team.get("settings_file_created", False) and not teams
    if drop_whole_file or additions_to_remove:
        settings_path = _settings_path(target_dir)
        if dry_run:
            if drop_whole_file:
                actions.append("[dry-run] remove .claude/settings.json")
            else:
                actions.append(f"[dry-run] settings.json -= {additions_to_remove}")
        elif drop_whole_file:
            if settings_path.exists():
                settings_path.unlink()
                actions.append("removed .claude/settings.json")
        else:
            settings = _read_json(settings_path, None)
            if settings is not None:
                settings = _remove_settings_additions(settings, additions_to_remove)
                _write_json(settings_path, settings)
                actions.append(f"updated settings.json (-{len(additions_to_remove)} keys)")

    if dry_run:
        actions.append(f"[dry-run] update manifest .claude/{MANIFEST_NAME}")
        return actions

    if teams:
        manifest["teams"] = teams
        _write_json(manifest_path, manifest)
        actions.append(f"updated manifest .claude/{MANIFEST_NAME}")
    else:
        manifest_path.unlink()
        actions.append(f"removed manifest .claude/{MANIFEST_NAME}")
        claude_dir = target_dir / ".claude"
        for sub in ("agents", "hooks"):
            subdir = claude_dir / sub
            if subdir.exists() and not any(subdir.iterdir()):
                subdir.rmdir()

    return actions


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if args and args[0] == "--destroy":
        if len(args) < 3:
            sys.exit("usage: python -m teambuild.cc_scaffold --destroy <prefix> <target_dir> [--dry-run]")
        for line in destroy(args[1], args[2], dry_run=dry):
            print(line)
        return

    if len(args) < 2:
        sys.exit("usage: python -m teambuild.cc_scaffold roster.json target_dir [--dry-run]")

    roster = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    for line in scaffold(roster, args[1], dry_run=dry):
        print(line)


if __name__ == "__main__":
    main()
