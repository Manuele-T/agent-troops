#!/usr/bin/env python3
"""PreToolUse hook: ask on the board before Claude Code reaches outside this computer.

Two ways to run it.

Team mode, registered in .claude/settings.json by the scaffolder for a team's roles whose tools can
send data off the machine (web search and fetch, the gated roles' shell):
    board_gate.py --roles rese-researcher
Only events whose agent_type is a listed role are judged; every other session in the repo is untouched.
Every web call and every shell command of those roles gets a card.

Guard mode, installed by `team-build guard install` or by the agent-troops plugin, for any session:
    board_gate.py --all [--mcp] [--sensitive 'config/*,*.pem'] [--flow block|ask]
Every event is judged, with or without an agent name (a plain session shows on the board as "main").
Web fetches and searches always get a card; a shell command only when it can reach the network or run
downloaded code (outward_reason below: curl, ssh, git push, a URL, `| sh`...), so everyday commands stay
with Claude Code's own permissions; MCP tools only with --mcp. With --sensitive, a session that has read
a matching file (Read, Grep in content mode, or a shell command naming it) is marked, and from then on
its outward calls are refused (--flow block, the default) or carry a red warning on the card (--flow
ask). --plugin-options reads the same settings from the plugin's userConfig (CLAUDE_PLUGIN_OPTION_*). A
repository's shared policy (.agent-troops.json in the project or a parent folder) adds its "sensitive"
patterns, and its "after_sensitive": "block" wins over "ask". If that file is invalid, its patterns are not
used here; the board then warns on every call from the repository and allows nothing automatically.

For a call it judges it asks the board (http://127.0.0.1:8123/gate) and waits for a person to click
Allow or Deny there; the board holds the request open until then. The answer goes back to Claude Code
as permissionDecision:

  * Allow on the board  -> "allow": Claude Code runs the call without its own terminal prompt.
  * Deny on the board   -> "deny", with the reason shown to the model.
  * board not running   -> no answer from this hook: Claude Code's own permissions apply as if it
                           were not installed (in a terminal, its usual prompt asks you).
  * no answer in time, or anything unexpected after the board took the request -> "deny".

Other hooks still apply: bash_guard.py refuses a command off the role's allowlist whatever is clicked
here (a refusal from any hook wins). So for a shell command this hook first applies the same role's
allowlist, read from the bash_guard entry in the project's .claude/settings.json, with bash_guard's
own decide(): a command bash_guard will refuse gets no card (and no answer from this hook). If that
lookup fails for any reason, the card is shown as usual. When the same call reaches the board twice
(team mode and guard mode both installed), the board shows one card and answers both.

The outward-command list is a heuristic, a second line after Claude Code's own prompts and not a
sandbox: a program can reach the network in ways it does not list. Stdlib only.
"""

import fnmatch
import json
import os
import re
import shlex
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

GATE_URL = os.environ.get("TEAMBUILD_BOARD_GATE_URL", "http://127.0.0.1:8123/gate")
BOARD_URL = os.environ.get("TEAMBUILD_BOARD_URL", "http://127.0.0.1:8123/hook")
WAIT_SECONDS = 570          # below the hook timeout the installer sets (600), so the hook answers itself
DETAIL_MAX = 2000
DETAIL_FIELDS = {"WebFetch": "url", "WebSearch": "query", "Bash": "command"}
WEB_TOOLS = ("WebFetch", "WebSearch")
MAIN = "main"               # the board's name for a session that has no agent name
STATE_MAX_AGE = 7 * 24 * 3600

# --------------------------------------------------------------------------- #
# Which shell commands reach outside (guard mode)                              #
# --------------------------------------------------------------------------- #

NETWORK_PROGRAMS = {
    "curl", "wget", "aria2c", "http", "https", "xh", "httpie",
    "ssh", "scp", "sftp", "rsync", "ftp", "lftp", "telnet", "nc", "ncat", "netcat", "socat",
    "dig", "nslookup", "host", "drill", "ping",
    "aws", "gcloud", "gsutil", "az", "s3cmd", "rclone",
    "sendmail", "mail", "mailx", "mutt", "swaks",
}
SUBCOMMANDS = {             # program -> sub-commands that send something out
    "git": {"push", "send-email", "request-pull"},
    "npm": {"publish"}, "pnpm": {"publish"}, "yarn": {"publish", "npm"}, "bun": {"publish"},
    "cargo": {"publish"}, "gem": {"push"}, "twine": {"upload"}, "poetry": {"publish"},
    "uv": {"publish"}, "flit": {"publish"}, "hatch": {"publish"},
    "docker": {"push", "login"}, "podman": {"push", "login"},
}
GH_READ_ONLY = {"view", "list", "status", "diff", "checks", "browse", "search", "help", "checkout", "clone", "watch"}
WRAPPERS = {"sudo", "env", "timeout", "nice", "nohup", "xargs", "time", "command", "exec", "watch",
            "stdbuf", "ionice", "doas", "builtin"}
SPLIT_RE = re.compile(r"\|\||&&|[;|&\n]|\$\(|`|\(|\)")
URL_RE = re.compile(r"\b(?:https?|ftp|wss?)://(?!(?:127\.0\.0\.1|localhost|\[::1\])(?:[:/]|$))", re.I)
RAW_RE = [
    (re.compile(r"/dev/(?:tcp|udp)/"), "opens a raw network connection"),
    (re.compile(r"\|\s*(?:sudo\s+)?(?:ba|z|da|k)?sh\b|\|\s*python[0-9.]*\s*(?:-\s*)?$", re.M), "pipes text into a shell"),
    (re.compile(r"\bbase64\s+(?:-d|--decode|-D)\b"), "decodes hidden data"),
    (re.compile(r"\b(?:python[0-9.]*|node|deno|bun|ruby|perl|php)\s+(?:-\S+\s+)*-[ce]\b.*(?:urllib|requests|httpx|http\.client"
                r"|socket|smtplib|ftplib|fetch\(|https?\.(?:get|request)|net/http|Net::HTTP|LWP|curl_)", re.S),
     "runs inline code that uses the network"),
]


def _words(part):
    try:
        return shlex.split(part)
    except ValueError:
        return part.split()


def _program(words):
    """(program, the words after it), skipping wrappers like sudo/env/timeout and VAR=value."""
    i = 0
    while i < len(words):
        w = words[i]
        name = os.path.basename(w)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w) or w.startswith("-") or name in WRAPPERS \
                or re.fullmatch(r"\d+[smhd]?", w):
            i += 1
            continue
        return name, words[i + 1:]
    return None, []


def outward_reason(command):
    """Why this shell command can send data off the computer (or run downloaded code), or None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for rx, why in RAW_RE:
        if rx.search(command):
            return why
    if URL_RE.search(command):
        return "contains a web address"
    for part in SPLIT_RE.split(command):
        prog, rest = _program(_words(part))
        if not prog:
            continue
        if prog in NETWORK_PROGRAMS:
            return "runs %s, which talks to other computers" % prog
        if prog == "git":           # git -C DIR / -c key=value come before the sub-command
            rest = [a for i, a in enumerate(rest) if not (i and rest[i - 1] in ("-C", "-c", "--git-dir", "--work-tree"))]
        args = [a for a in rest if not a.startswith("-")]
        if prog in SUBCOMMANDS and args and args[0] in SUBCOMMANDS[prog]:
            return "runs %s %s" % (prog, args[0])
        if prog == "gh" and args and (args[0] == "api" or (len(args) > 1 and args[1] not in GH_READ_ONLY)):
            return "runs gh %s, which can write to GitHub" % " ".join(args[:2])
    return None


# --------------------------------------------------------------------------- #
# Sensitive files (guard mode, --sensitive)                                    #
# --------------------------------------------------------------------------- #

def project_dir(event):
    return os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or os.getcwd()


POLICY_NAME = ".agent-troops.json"


def repo_policy(start):
    """(sensitive patterns, after_sensitive) from the repository's .agent-troops.json, if one is found and
    usable (same rules as teambuild/policy_file.py, kept minimal here); ([], None) otherwise."""
    try:
        here = os.path.abspath(start)
        for _ in range(40):
            path = os.path.join(here, POLICY_NAME)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                pats = data.get("sensitive", []) if isinstance(data, dict) and data.get("version") == 1 else None
                flow = data.get("after_sensitive", "block")
                if not (isinstance(pats, list) and all(isinstance(p, str) and p.strip() for p in pats)
                        and flow in ("block", "ask")):
                    return [], None
                return [p.strip() for p in pats], flow
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
    except Exception:
        pass
    return [], None


def _matches(path, patterns, root):
    if not isinstance(path, str) or not path:
        return None
    full = os.path.normpath(os.path.join(root, os.path.expanduser(path)))
    rel = os.path.relpath(full, root).replace(os.sep, "/")
    for pat in patterns:
        if pat.startswith(("/", "~")):
            if fnmatch.fnmatch(full, os.path.normpath(os.path.expanduser(pat))):
                return pat
        elif not rel.startswith("../") and (fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(os.path.basename(rel), pat)):
            return pat
    return None


def _search_covers(path, patterns, root):
    """A content search under `path` can return lines from a file matching one of the patterns."""
    hit = _matches(path, patterns, root)
    if hit:
        return hit
    full = os.path.normpath(os.path.join(root, os.path.expanduser(path or ".")))
    for pat in patterns:
        base = os.path.normpath(os.path.expanduser(pat)) if pat.startswith(("/", "~")) else os.path.join(root, pat)
        fixed = base.split("*")[0].split("?")[0].split("[")[0]
        if "/" not in pat.lstrip("/~") or fixed.startswith(full.rstrip(os.sep) + os.sep) or fixed == full:
            return pat
    return None


def sensitive_touch(event, patterns):
    """The pattern this call reads a sensitive file through, or None."""
    if not patterns:
        return None
    root = project_dir(event)
    tool = event.get("tool_name")
    inp = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    if tool in ("Read", "NotebookRead"):
        return _matches(inp.get("file_path") or inp.get("notebook_path"), patterns, root)
    if tool == "Grep" and inp.get("output_mode") == "content":
        return _search_covers(inp.get("path"), patterns, root)
    if tool == "Bash":
        for w in _words(inp.get("command") or ""):
            for piece in re.split(r"[=<>@]", w):
                hit = _matches(piece, patterns, root) if piece and not piece.startswith("-") else None
                if hit:
                    return hit
    return None


def state_dir():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "agent-troops", "sensitive")


def _state_file(folder, session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("unusable session id")
    return os.path.join(folder, session_id + ".json")


def read_marks(folder, session_id):
    """Patterns this session has read files through. Raises on anything odd (the caller fails closed)."""
    path = _state_file(folder, session_id)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("corrupt state")
    return data


def add_mark(folder, session_id, pattern):
    marks = read_marks(folder, session_id)
    if pattern in marks:
        return
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(marks + [pattern], f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _state_file(folder, session_id))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        now = time.time()
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if name.endswith(".json") and now - os.path.getmtime(p) > STATE_MAX_AGE:
                os.unlink(p)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# The board                                                                    #
# --------------------------------------------------------------------------- #

def detail_of(event):
    """The part of the call a person needs to judge it: the URL, the search, the command."""
    tool_input = event.get("tool_input") or {}
    value = tool_input.get(DETAIL_FIELDS.get(event.get("tool_name"), ""), "")
    if not isinstance(value, str) or not value:
        try:
            value = json.dumps(tool_input)
        except (TypeError, ValueError):
            value = ""
    return value[:DETAIL_MAX]


def ask_board(event, url=GATE_URL, wait=WAIT_SECONDS, agent=None, warnings=None):
    """"allow" | "deny" | "unreachable". Never raises."""
    body = {
        "agent_type": agent or event.get("agent_type"), "tool_name": event.get("tool_name"),
        "detail": detail_of(event), "session_id": event.get("session_id"), "cwd": event.get("cwd"),
    }
    if warnings:
        body["warnings"] = warnings
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=wait) as resp:
            answer = json.loads(resp.read(10_000).decode("utf-8"))
    except urllib.error.URLError as exc:
        # Connection refused: no board on this computer. Anything else (timeout mid-wait, a reset
        # after the board took the request, an HTTP error) means the board was there: refuse.
        refused = isinstance(exc.reason, ConnectionRefusedError)
        return "unreachable" if refused and not isinstance(exc, urllib.error.HTTPError) else "deny"
    except ConnectionRefusedError:
        return "unreachable"
    except (OSError, socket.timeout, ValueError):
        return "deny"
    decision = answer.get("decision") if isinstance(answer, dict) else None
    return decision if decision in ("allow", "deny") else "deny"


def notify_board(event, agent, reason):
    """Tell the board (if one is running) that a call was refused here. Never raises."""
    try:
        payload = {"hook_event_name": "GuardBlocked", "guard": "board_gate", "tool_name": event.get("tool_name"),
                   "reason": reason, "agent_type": agent, "agent_id": event.get("agent_id"), "cwd": event.get("cwd")}
        req = urllib.request.Request(BOARD_URL, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.3).close()
    except Exception:
        pass


def refused_by_bash_guard(event, hooks_dir=None):
    """True when bash_guard.py, configured for this role, will refuse this shell command anyway."""
    if event.get("tool_name") != "Bash" or not event.get("agent_type"):
        return False
    try:
        hooks_dir = hooks_dir or os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(hooks_dir), "settings.json"), encoding="utf-8") as f:
            settings = json.load(f)
        allow = None
        for entry in settings.get("hooks", {}).get("PreToolUse", []):
            for h in entry.get("hooks", []):
                argv = shlex.split(h.get("command", ""))
                if any(a.endswith("bash_guard.py") for a in argv) and argv[argv.index("--role") + 1] == event.get("agent_type"):
                    allow = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--allow"]
        if allow is None:
            return False
        sys.path.insert(0, hooks_dir)
        from bash_guard import decide
        ok, _ = decide((event.get("tool_input") or {}).get("command"), allow)
        return not ok
    except Exception:
        return False


def output(decision, reason):
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision, "permissionDecisionReason": reason,
    }}))


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #

def options(argv, env=None):
    """{"roles", "all", "mcp", "sensitive", "flow"} from the command line (or the plugin's userConfig)."""
    env = os.environ if env is None else env

    def opt(flag):
        return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag), None)

    o = {"roles": [r for r in (opt("--roles") or "").split(",") if r], "all": "--all" in argv,
         "mcp": "--mcp" in argv, "sensitive": [p.strip() for p in (opt("--sensitive") or "").split(",") if p.strip()],
         "flow": opt("--flow") or "block"}
    if "--plugin-options" in argv:
        o["all"] = True
        o["mcp"] = o["mcp"] or env.get("CLAUDE_PLUGIN_OPTION_MCP_CALLS", "").lower() in ("true", "1", "yes")
        o["sensitive"] += [p.strip() for p in env.get("CLAUDE_PLUGIN_OPTION_SENSITIVE_FILES", "").split(",") if p.strip()]
        o["flow"] = env.get("CLAUDE_PLUGIN_OPTION_AFTER_SENSITIVE") or o["flow"]
    if o["flow"] not in ("block", "ask"):
        o["flow"] = "block"
    return o


def guard_mode(event, o, url, folder):
    """Guard mode: judge any session's call. Writes the hook's answer (or nothing)."""
    tool = event.get("tool_name") or ""
    agent = event.get("agent_type") or MAIN
    shared, shared_flow = repo_policy(project_dir(event))
    if shared:
        own = o["sensitive"]            # with patterns of its own, the stricter of the two settings wins
        flow = ("block" if "block" in (o["flow"], shared_flow) else "ask") if own else shared_flow
        o = dict(o, sensitive=own + [p for p in shared if p not in own], flow=flow)
    if o["sensitive"]:
        hit = sensitive_touch(event, o["sensitive"])
        if hit:
            try:
                add_mark(folder, event.get("session_id"), hit)
            except Exception:
                output("deny", "board_gate: this reads a file marked sensitive (%s) and the mark could not be "
                               "recorded, so it is refused" % hit)
                return
    if tool in WEB_TOOLS:
        pass
    elif tool == "Bash":
        if not outward_reason((event.get("tool_input") or {}).get("command")):
            return
    elif tool.startswith("mcp__"):
        if not o["mcp"]:
            return
    else:
        return
    project_hooks = os.path.join(project_dir(event), ".claude", "hooks")
    if refused_by_bash_guard(event, project_hooks):
        return
    warnings = []
    if o["sensitive"]:
        try:
            marks = read_marks(folder, event.get("session_id"))
        except Exception:
            marks = ["(the session's record could not be read)"]
        if marks:
            why = "this session has read files marked sensitive (%s)" % ", ".join(marks)
            if o["flow"] == "block":
                notify_board(event, agent, why)
                output("deny", "board_gate: refused, %s. Nothing more may leave this computer from this session; "
                               "finish with what you have, or start a new session" % why)
                return
            warnings.append(why)
    verdict = ask_board(event, url, agent=agent, warnings=warnings)
    if verdict == "allow":
        output("allow", "board_gate: allowed on the board")
    elif verdict == "deny":
        output("deny", "board_gate: denied on the board (or no answer in time)")


def main(argv, url=None, folder=None):
    o = options(argv)
    try:
        event = json.load(sys.stdin)
    except ValueError:
        output("deny", "board_gate: could not read the hook event")
        return 0
    if not isinstance(event, dict):
        return 0
    if o["all"]:
        guard_mode(event, o, url or GATE_URL, folder or state_dir())
        return 0
    if event.get("agent_type") not in o["roles"]:
        return 0
    if refused_by_bash_guard(event):
        return 0      # bash_guard refuses it; asking a person would be a pointless card
    verdict = ask_board(event, url or GATE_URL)
    if verdict == "allow":
        output("allow", "board_gate: allowed on the board")
    elif verdict == "unreachable":
        pass      # no board: leave the decision to Claude Code's own permissions, unchanged
    else:
        output("deny", "board_gate: denied on the board (or no answer in time)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
