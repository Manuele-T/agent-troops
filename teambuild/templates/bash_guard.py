#!/usr/bin/env python3
"""PreToolUse hook: let one role run only an allowlist of Bash commands. Fails closed.

Registered in .claude/settings.json, one entry per guarded role:
    bash_guard.py --role cr-poster --allow 'gh pr view' --allow 'gh pr comment'
It objects to nothing unless the hook event's agent_type equals --role, so other agents and
the user's own sessions in the same repo are untouched. (It lives in settings.json, not in the
agent's frontmatter, because frontmatter hooks did not fire in headless tests - see
docs/shakeout-log.md.) Reads the hook JSON on stdin. Exit 0 = no objection; exit 2 = block
(stderr goes to the model).

Deliberately dumb: a command runs only if it has no shell metacharacters at all (chaining,
substitution, redirects, variables, globs-by-brace) and its tokens start with an allowed entry.
Prefix matching alone is trivially escaped (`git push && rm -rf ~`), hence the character ban.
"""

import json
import os
import shlex
import sys
import urllib.request

GUARD_NAME = "bash_guard"
FORBIDDEN_CHARS = set(";&|<>$`\\\n\r\x00{}()!")
DENY_TOKENS = {"--force", "-f", "--force-with-lease", "--delete", "--mirror", "--no-verify",
               "--token", "-t"}       # a deploy token on the command line ends up in logs and history
DENY_TOKEN_PREFIXES = ("--receive-pack", "--upload-pack", "--exec", "--output", "--force-with-lease=", "--token=")
URLISH = ("://", "@", ":")


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


def decide(command, allow):
    """Return (allowed, reason). Pure, so it can be tested without a shell."""
    if not isinstance(command, str) or not command.strip():
        return False, "empty command"
    bad = sorted(c for c in set(command) if c in FORBIDDEN_CHARS)
    if bad:
        shown = "".join(repr(c)[1:-1] for c in bad)
        return False, "shell metacharacters are not allowed (%s)" % shown
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return False, "could not parse command (%s)" % exc
    if not tokens:
        return False, "empty command"
    for entry in allow:
        prefix = entry.split()
        if tokens[: len(prefix)] != prefix:
            continue
        rest = tokens[len(prefix):]
        for tok in rest:
            if tok in DENY_TOKENS or tok.startswith(DENY_TOKEN_PREFIXES):
                return False, "flag %r is not allowed" % tok
        if prefix[:2] == ["git", "push"] and any(u in tok for tok in rest for u in URLISH):
            return False, "git push may only name a configured remote, not a URL or refspec"
        return True, "matches allowed command %r" % entry
    return False, "command is not on this role's allowlist"


def main(argv):
    allow = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--allow"]
    role = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--role"), None)
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") != "Bash":
            return 0
        if role is not None and event.get("agent_type") != role:
            return 0
        ok, reason = decide((event.get("tool_input") or {}).get("command"), allow)
    except Exception as exc:  # any surprise blocks rather than allows
        sys.stderr.write("bash_guard: blocked (internal error: %s)\n" % exc)
        return 2
    if ok:
        return 0
    notify_board(event, "Bash", reason)
    sys.stderr.write(
        "bash_guard: blocked - %s. Allowed commands for this role: %s\n"
        % (reason, ", ".join(allow) or "none")
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
