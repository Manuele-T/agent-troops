#!/usr/bin/env python3
"""PreToolUse hook: one-way flow. Once private data has entered a session, no more web. Fails closed.

Registered in .claude/settings.json by the scaffolder, for teams that have both a web member and a
member that reads files:
    flow_guard.py --orchestrator rese-orchestrator --private rese-analyst,rese-writer \
                  --outbound rese-researcher --mode block

Why: no single member holds the lethal trifecta, but the team does, and the
orchestrator carries text between members. Injected text from the web could make it ask a file-reading
member for private data and then ask the web member to "look up" a URL carrying it. This hook watches
the orchestrator's delegations and breaks that chain in code:

  * delegating to a PRIVATE member marks the session tainted (before that member runs);
  * delegating to an OUTBOUND member in a tainted session is refused (--mode block, exit 2, the model
    sees the reason) or handed to a human (--mode ask, permissionDecision "ask": Claude Code's own
    prompt in a terminal; in a headless run nobody can answer, so it is refused).

A delegation whose target it can't identify counts as both kinds. Only events whose agent_type is the
orchestrator are judged; every other session in the repo is untouched. State is one small file per
session in a private per-user folder, never in the project. Anything unreadable, a missing session id,
or a payload it doesn't understand is treated as tainted: refused, never waved through. Stdlib only.
"""

import json
import os
import sys
import tempfile
import time
import urllib.request

GUARD_NAME = "flow_guard"
DELEGATION_TOOLS = ("Agent", "Task")
MESSAGE_TOOLS = ("SendMessage",)      # continuing an agent that may already be running: judged as outbound
STATE_MAX_AGE = 7 * 24 * 3600
BOARD_URL = os.environ.get("TEAMBUILD_BOARD_URL", "http://127.0.0.1:8123/hook")


def state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "agent-troops", "flow")


def notify_board(event, reason):
    """Tell the local board (if one is running) that this call was refused. Never raises."""
    try:
        payload = {
            "hook_event_name": "GuardBlocked", "guard": GUARD_NAME, "tool_name": event.get("tool_name"),
            "reason": reason, "agent_type": event.get("agent_type"),
            "agent_id": event.get("agent_id"), "cwd": event.get("cwd"),
        }
        req = urllib.request.Request(
            BOARD_URL, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass


def _safe_name(session_id):
    ok = all(c.isalnum() or c in "-_" for c in session_id)
    return session_id if ok and 0 < len(session_id) <= 128 else None


def load_state(folder, session_id):
    """{"tainted": bool, "by": [...]}. Raises on anything odd, so the caller fails closed."""
    name = _safe_name(session_id)
    if name is None:
        raise ValueError("unusable session id")
    path = os.path.join(folder, name + ".json")
    if not os.path.exists(path):
        return {"tainted": False, "by": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("tainted"), bool):
        raise ValueError("corrupt state")
    return data


def save_state(folder, session_id, data):
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(folder, _safe_name(session_id) + ".json"))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune(folder, now=None):
    now = now or time.time()
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if name.endswith(".json") and now - os.path.getmtime(p) > STATE_MAX_AGE:
                os.unlink(p)
    except OSError:
        pass


def target_of(event):
    """(kind, name): kind is "delegate", "message" or None (not ours to judge)."""
    tool = event.get("tool_name")
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool in DELEGATION_TOOLS:
        return "delegate", inp.get("subagent_type")
    if tool in MESSAGE_TOOLS:
        return "message", inp.get("to")
    return None, None


def decide(event, private, outbound, state):
    """Pure. Returns (verdict, reason, new_state). verdict: "allow" | "refuse"."""
    kind, name = target_of(event)
    tainted = bool(state.get("tainted"))
    if kind == "message":
        if tainted:
            return "refuse", "messages to other agents are closed once private data has been read", state
        return "allow", "", state
    known = name in private or name in outbound
    is_private = name in private or not known
    is_outbound = name in outbound or not known
    if is_outbound and tainted:
        who = name or "an unnamed agent"
        return "refuse", ("this team does no more web research after private data has been read in the "
                          "session (after %s; asked for %s). Finish with what you have, or start a "
                          "new session for new research" % (", ".join(state.get("by") or ["?"]), who)), state
    if is_private and not tainted:
        return "allow", "", {"tainted": True, "by": [name or "an unnamed agent"]}
    if is_private:
        by = list(state.get("by") or [])
        if name and name not in by:
            by.append(name)
        return "allow", "", {"tainted": True, "by": by}
    return "allow", "", state


def main(argv, folder=None):
    def opt(flag):
        return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag), None)

    orchestrator = opt("--orchestrator")
    private = [r for r in (opt("--private") or "").split(",") if r]
    outbound = [r for r in (opt("--outbound") or "").split(",") if r]
    mode = opt("--mode") or "block"
    folder = folder or state_dir()
    try:
        event = json.load(sys.stdin)
        if not orchestrator or mode not in ("block", "ask"):
            raise ValueError("bad arguments")
        if event.get("agent_type") != orchestrator:
            return 0
        kind, _ = target_of(event)
        if kind is None:
            return 0
        session_id = event.get("session_id")
        if not isinstance(session_id, str):
            raise ValueError("no session id")
        state = load_state(folder, session_id)
        verdict, reason, new_state = decide(event, set(private), set(outbound), state)
        if new_state is not state:
            save_state(folder, session_id, new_state)
            prune(folder)
    except Exception as exc:  # any surprise refuses rather than allows
        sys.stderr.write("flow_guard: blocked (internal error: %s)\n" % type(exc).__name__)
        return 2
    if verdict == "allow":
        return 0
    notify_board(event, reason)
    if mode == "ask":
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": "flow_guard: " + reason + ". Allow it anyway?",
        }}))
        return 0
    sys.stderr.write("flow_guard: blocked - %s.\n" % reason)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
