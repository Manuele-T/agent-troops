#!/usr/bin/env python3
"""PreToolUse hook: keep named team roles' Read/Grep/Glob inside the project. Fails closed.

Registered in .claude/settings.json by `team-build --fence-reads`:
    read_guard.py --roles rese-researcher,rese-analyst,rese-writer
It objects to nothing unless the hook event's agent_type is one of --roles, so the user's own
sessions in the same repo are untouched. Paths are resolved through symlinks and `..` before the
check. Exit 0 = no objection; exit 2 = block (stderr goes to the model).

Why a hook: Claude Code's `permissions.blockReadsOutsideWorkingDirectories` had no effect in
Claude Code 2.1.233 (tested), and hooks demonstrably block even pre-approved reads.
"""

import json
import os
import sys
import urllib.request

GUARD_NAME = "read_guard"
FILE_TOOLS = ("Read", "Grep", "Glob")
GLOB_CHARS = "*?[{"


BOARD_URL = os.environ.get("TEAMBUILD_BOARD_URL", "http://127.0.0.1:8123/hook")


def notify_board(event, tool, reason):
    """Tell the local board (if one is running) that this call was refused. Never raises,
    never affects the exit code, never writes to stdout."""
    try:
        payload = {
            "hook_event_name": "GuardBlocked", "guard": GUARD_NAME, "tool_name": tool,
            "reason": reason, "agent_type": event.get("agent_type"),
            "agent_id": event.get("agent_id"), "cwd": event.get("cwd"),
        }
        req = urllib.request.Request(
            BOARD_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass


def literal_prefix(pattern):
    """Directory part of a glob pattern before its first wildcard."""
    cut = len(pattern)
    for ch in GLOB_CHARS:
        i = pattern.find(ch)
        if i != -1:
            cut = min(cut, i)
    prefix = pattern[:cut]
    return prefix if prefix.endswith("/") else os.path.dirname(prefix) or "."


def touched_paths(tool, tool_input):
    """Every filesystem location a call would reach, as raw strings."""
    paths = []
    if tool == "Read":
        if tool_input.get("file_path"):
            paths.append(tool_input["file_path"])
    else:
        if tool_input.get("path"):
            paths.append(tool_input["path"])
    if tool == "Glob":
        pattern = tool_input.get("pattern") or ""
        parts = pattern.replace("\\", "/").split("/")
        if pattern.startswith(("/", "~")) or ".." in parts:
            paths.append(literal_prefix(pattern))
    return paths


def inside(path, cwd, allowed_roots):
    expanded = os.path.expanduser(path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    real = os.path.realpath(expanded)
    return any(real == r or real.startswith(r + os.sep) for r in allowed_roots)


def decide(tool, tool_input, cwd, root, extra_roots=()):
    """Return (allowed, reason)."""
    roots = [os.path.realpath(r) for r in (root, *extra_roots) if r]
    to_check = touched_paths(tool, tool_input) or [cwd]  # no explicit path means "the cwd"
    for p in to_check:
        if not inside(p, cwd, roots):
            return False, "%s is outside the project (%s)" % (p, roots[0])
    return True, "inside the project"


def main(argv):
    roles = []
    for i, a in enumerate(argv[:-1]):
        if a == "--roles":
            roles = [r for r in argv[i + 1].split(",") if r]
    try:
        event = json.load(sys.stdin)
        tool = event.get("tool_name")
        if tool not in FILE_TOOLS or event.get("agent_type") not in roles:
            return 0
        cwd = event.get("cwd") or os.getcwd()
        root = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        ok, reason = decide(tool, event.get("tool_input") or {}, cwd, root, (event.get("scratchpad_dir"),))
    except Exception as exc:  # any surprise blocks rather than allows
        sys.stderr.write("read_guard: blocked (internal error: %s)\n" % exc)
        return 2
    if ok:
        return 0
    notify_board(event, tool, reason)
    sys.stderr.write(
        "read_guard: blocked - %s. Team roles may only read files inside the project folder.\n" % reason
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
