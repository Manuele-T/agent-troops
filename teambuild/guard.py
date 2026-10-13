"""Guard mode: the board's approvals for any Claude Code session, no team needed.

    team-build guard install [--shared | --user] [--target DIR] [--mcp] [--sensitive PATTERNS] [--flow block|ask]
    team-build guard status  [--target DIR]
    team-build guard remove  [--user] [--target DIR]

Where it goes:

  local  (default)  DIR/.claude/settings.local.json   this project, only you (Claude Code keeps that file
                                                       out of git)
  shared            DIR/.claude/settings.json         this project, everyone who opens it (commit it)
  user              ~/.claude/settings.json           every project on this computer

It adds two things to that settings file: emit.py on the hook events the board shows (so a plain session
appears on the board as "main session"), and board_gate.py --all on web fetches, web searches and shell
commands (plus Read/Grep with --sensitive, MCP tools with --mcp). Both scripts are copied next to the
settings: DIR/.claude/hooks/agent-troops-guard/ for the project scopes, ~/.claude/agent-troops/guard/ for
the user scope. A guard.json beside them records exactly what was added, so `remove` takes out those
entries and files and nothing else; it never stores a copy of your settings. When the settings file was
formatted the way Claude Code writes it, remove leaves it byte-for-byte as it was.

No board running: the gate gives no answer and Claude Code asks as usual, so nothing breaks.
The agent-troops Claude Code plugin (plugin/) is the same guard installed through `/plugin`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
from pathlib import Path

from teambuild.cc_scaffold import PYTHON

TEMPLATES = Path(__file__).parent / "templates"
SCRIPTS = ("board_gate.py", "emit.py")
MANIFEST = "guard.json"
EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStop", "Stop")
GATE_TIMEOUT = 600            # seconds Claude Code waits for the gate; board_gate.py gives up at 570
PATTERN_RE = re.compile(r"[A-Za-z0-9_.*?/~\[\]!+-]{1,200}")
BOARD_PORT = 8123
PLUGIN_ID = "agent-troops@agent-troops"


SITE_RE = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)*")
SITES_MAX = 500


class GuardError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Site lists (the board reads the same file: teambuild/board/policy.py)          #
# --------------------------------------------------------------------------- #

def sites_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "agent-troops" / "sites.json"


def read_sites(path: Path | None = None) -> dict[str, list[str]]:
    try:
        data = json.loads((path or sites_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {k: [n for n in data.get(k, []) if isinstance(n, str)] if isinstance(data.get(k), list) else []
            for k in ("allow", "block")}


def write_sites(allow: list[str], block: list[str], path: Path | None = None) -> dict[str, list[str]]:
    """Check every name, then write the file (private, atomically). Raises GuardError, writing nothing."""
    clean = {}
    for key, names in (("allow", allow), ("block", block)):
        out = []
        for raw in names:
            n = raw.strip().lower().rstrip(".")
            if n.startswith(("http://", "https://")) or "/" in n:
                raise GuardError(f"{raw!r}: give a site name like docs.python.org, not an address")
            if n and not SITE_RE.fullmatch(n):
                raise GuardError(f"{raw!r} is not a site name")
            if n and n not in out:
                out.append(n)
        if len(out) > SITES_MAX:
            raise GuardError(f"at most {SITES_MAX} sites per list")
        clean[key] = out
    path = path or sites_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".sites-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return clean


def locate(scope: str, project: Path, home: Path | None = None) -> tuple[Path, Path, str]:
    """(settings file, scripts folder, how hook commands name that folder)."""
    home = home or Path.home()
    if scope == "user":
        folder = home / ".claude" / "agent-troops" / "guard"
        return home / ".claude" / "settings.json", folder, folder.as_posix()     # C:/Users/... also works in a shell
    if scope not in ("local", "shared"):
        raise GuardError(f"unknown scope {scope!r}")
    name = "settings.local.json" if scope == "local" else "settings.json"
    return (project / ".claude" / name, project / ".claude" / "hooks" / "agent-troops-guard",
            "$CLAUDE_PROJECT_DIR/.claude/hooks/agent-troops-guard")


def gate_command(base: str, mcp: bool = False, sensitive: list[str] | None = None, flow: str = "block") -> str:
    sensitive = sensitive or []
    for p in sensitive:
        if not PATTERN_RE.fullmatch(p):
            raise GuardError(f"not a usable file pattern: {p!r} (letters, digits and . _ - / * ? ~ [ ] ! only)")
    if flow not in ("block", "ask"):
        raise GuardError("--flow must be block or ask")
    cmd = f'{PYTHON} "{base}/board_gate.py" --all'
    if mcp:
        cmd += " --mcp"
    if sensitive:
        cmd += f" --sensitive '{','.join(sensitive)}' --flow {flow}"
    return cmd


def gate_matcher(mcp: bool, sensitive: bool) -> str:
    tools = ["WebFetch", "WebSearch", "Bash"] + (["Read", "Grep"] if sensitive else []) + (["mcp__.*"] if mcp else [])
    return "|".join(tools)


def entries(base: str, mcp: bool = False, sensitive: list[str] | None = None, flow: str = "block") -> list[tuple[str, dict]]:
    """(event, settings entry) pairs that guard mode adds."""
    emit = f'{PYTHON} "{base}/emit.py"'
    out = []
    for event in EVENTS:
        e = {"hooks": [{"type": "command", "command": emit}]}
        out.append((event, {"matcher": "*", **e} if event in ("PreToolUse", "PostToolUse") else e))
    out.append(("PreToolUse", {"matcher": gate_matcher(mcp, bool(sensitive)), "hooks": [
        {"type": "command", "command": gate_command(base, mcp, sensitive, flow), "timeout": GATE_TIMEOUT}]}))
    return out


def _read_settings(path: Path) -> tuple[dict, dict]:
    """(settings, how the file was written: indent and final newline)."""
    if not path.exists():
        return {}, {"indent": 2, "newline": True}
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as e:
        raise GuardError(f"{path} is not valid JSON ({e}); fix it first, nothing was changed")
    if not isinstance(data, dict):
        raise GuardError(f"{path} does not hold a JSON object; nothing was changed")
    m = re.search(r"\n( +)\"", text)
    return data, {"indent": len(m.group(1)) if m else 2, "newline": text.endswith("\n")}


def _write_settings(path: Path, data: dict, style: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=style["indent"]) + ("\n" if style["newline"] else ""), encoding="utf-8")


def _commands(entry: dict) -> set[str]:
    return {h.get("command") for h in entry.get("hooks", []) if isinstance(h, dict)}


def find(project: Path, home: Path | None = None) -> dict[str, dict]:
    """{scope: its guard.json} for every install that affects this project."""
    found = {}
    for scope in ("local", "user"):          # local and shared share the project folder
        _, folder, _ = locate(scope, project, home)
        try:
            m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and m.get("scope") in ("local", "shared", "user"):
            found[m["scope"]] = m
    return found


def install(project: Path, scope: str = "local", mcp: bool = False, sensitive: list[str] | None = None,
            flow: str = "block", home: Path | None = None) -> list[str]:
    project = Path(project).resolve()
    settings_path, folder, base = locate(scope, project, home)
    existing = find(project, home)
    other = next((s for s in existing if s != scope and s != "user" and scope != "user"), None)
    if other:
        raise GuardError(f"guard mode is already installed in this project ({other} scope): "
                         f"`team-build guard remove` first")
    lines = []
    if scope in existing:
        lines += remove(project, scope, home)
    new = entries(base, mcp, sensitive, flow)              # validates the options before anything is written
    data, style = _read_settings(settings_path)
    created_file = not settings_path.exists()
    created_hooks = "hooks" not in data
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise GuardError(f"{settings_path}: \"hooks\" is not an object; nothing was changed")
    created_events = [ev for ev in EVENTS if ev not in hooks]
    for event, entry in new:
        hooks.setdefault(event, []).append(entry)
    created_dirs = [str(d) for d in [folder, *folder.parents] if not d.exists()]
    folder.mkdir(parents=True, exist_ok=True)
    for name in SCRIPTS:
        shutil.copyfile(TEMPLATES / name, folder / name)
    manifest = {"scope": scope, "settings": str(settings_path), "created_file": created_file,
                "created_hooks": created_hooks, "created_events": created_events,
                "created_dirs": created_dirs,
                "commands": sorted({c for _, e in new for c in _commands(e)}),
                "options": {"mcp": mcp, "sensitive": sensitive or [], "flow": flow}}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    _write_settings(settings_path, data, style)
    lines.append(f"guard mode installed ({scope}): {settings_path}")
    lines.append(f"scripts: {folder}")
    return lines


def remove(project: Path, scope: str | None = None, home: Path | None = None) -> list[str]:
    """Take out exactly what install added. scope None: the project's own install (local or shared)."""
    project = Path(project).resolve()
    found = find(project, home)
    if scope is None:
        scope = next((s for s in ("local", "shared") if s in found), None)
    if scope not in found:
        return ["guard mode is not installed here" if scope != "user" else "guard mode is not installed for your user"]
    m = found[scope]
    settings_path, folder, _ = locate(scope, project, home)
    ours = set(m.get("commands", []))
    lines = []
    if settings_path.exists():
        data, style = _read_settings(settings_path)
        hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
        for event in list(hooks):
            if not isinstance(hooks[event], list):
                continue
            hooks[event] = [e for e in hooks[event] if not (isinstance(e, dict) and _commands(e) and _commands(e) <= ours)]
            if not hooks[event] and event in m.get("created_events", []):
                del hooks[event]
        if "hooks" in data and not hooks and m.get("created_hooks"):
            del data["hooks"]
        if m.get("created_file") and not data:
            settings_path.unlink()
            lines.append(f"removed {settings_path} (guard mode had created it)")
        else:
            _write_settings(settings_path, data, style)
            lines.append(f"removed guard mode's entries from {settings_path}")
    for name in SCRIPTS + (MANIFEST,):
        (folder / name).unlink(missing_ok=True)
    for d in [folder] + [Path(p) for p in m.get("created_dirs", []) if Path(p) != folder]:
        try:
            d.rmdir()              # only if empty: anything else put there stays
        except OSError:
            pass
    lines.append(f"removed {folder}")
    return lines


def board_running(port: int = BOARD_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def plugin_enabled(home: Path | None = None) -> bool:
    try:
        data = json.loads(((home or Path.home()) / ".claude" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    return isinstance(plugins, dict) and bool(plugins.get(PLUGIN_ID))


def status(project: Path, home: Path | None = None, port: int = BOARD_PORT) -> dict:
    project = Path(project).resolve()
    installs = []
    for scope, m in find(project, home).items():
        _, folder, _ = locate(scope, project, home)
        outdated = [n for n in SCRIPTS if not (folder / n).exists()
                    or (folder / n).read_bytes() != (TEMPLATES / n).read_bytes()]
        installs.append({"scope": scope, "settings": m.get("settings"), "options": m.get("options", {}),
                         "outdated": outdated})
    from teambuild import policy_file
    repo = policy_file.for_folder(project)
    return {"project": str(project), "installs": installs, "plugin": plugin_enabled(home),
            "board": board_running(port), "policy": policy_file.describe(repo) if repo else None}


def format_status(s: dict) -> str:
    where = {"local": "this project, only you", "shared": "this project, everyone (committed settings)",
             "user": "EVERY project on this computer"}
    out = [f"project: {s['project']}"]
    if not s["installs"] and not s["plugin"]:
        out.append("guard mode: not installed (team-build guard install)")
    for i in s["installs"]:
        o = i["options"]
        extra = []
        if o.get("mcp"):
            extra.append("MCP tools carded")
        if o.get("sensitive"):
            extra.append(f"sensitive files: {', '.join(o['sensitive'])} (then {o.get('flow', 'block')})")
        out.append(f"guard mode: on, {i['scope']} scope ({where[i['scope']]}) in {i['settings']}"
                   + (f"; {'; '.join(extra)}" if extra else ""))
        if i["outdated"]:
            out.append(f"  ! {', '.join(i['outdated'])} differ from this version: run install again to update")
    if s["plugin"]:
        out.append("guard mode: on through the agent-troops plugin (every project; /plugin to manage it)")
    if s.get("policy"):
        out.append(s["policy"])
    out.append("board: running at http://127.0.0.1:8123" if s["board"] else
               "board: NOT running, so calls fall back to Claude Code's own prompts (start it: team-build board)")
    return "\n".join(out)


USAGE = """usage:
  team-build guard install [--shared | --user] [--target DIR] [--mcp] [--sensitive PATTERNS] [--flow block|ask]
  team-build guard status  [--target DIR]
  team-build guard remove  [--user] [--target DIR]"""


def main(args: list[str]) -> int:
    if not args or args[0] not in ("install", "status", "remove"):
        print(USAGE)
        return 2
    action, rest = args[0], args[1:]

    def value(flag: str) -> str | None:
        if flag not in rest:
            return None
        i = rest.index(flag)
        if i + 1 >= len(rest):
            raise GuardError(f"{flag} needs a value\n{USAGE}")
        v = rest[i + 1]
        del rest[i:i + 2]
        return v

    try:
        target = Path(value("--target") or ".").expanduser()
        sensitive = [p.strip() for p in (value("--sensitive") or "").split(",") if p.strip()]
        flow = value("--flow") or "block"
        known = {"--shared", "--user", "--mcp"}
        unknown = [a for a in rest if a not in known]
        if unknown:
            raise GuardError(f"unknown option(s): {' '.join(unknown)}\n{USAGE}")
        if not target.is_dir():
            raise GuardError(f"not a folder: {target}")
        if "--shared" in rest and "--user" in rest:
            raise GuardError("choose one of --shared and --user")
        scope = "user" if "--user" in rest else "shared" if "--shared" in rest else "local"
        if action == "install":
            for line in install(target, scope, "--mcp" in rest, sensitive, flow):
                print(line)
            if scope == "user":
                print("This applies to every project on this computer. Undo: team-build guard remove --user")
            if not board_running():
                print("The board is not running: start it with `team-build board` (until then Claude Code asks as usual).")
            print("Takes effect in Claude Code sessions started from now on.")
        elif action == "remove":
            for line in remove(target, "user" if scope == "user" else None):
                print(line)
        else:
            print(format_status(status(target)))
    except GuardError as e:
        print(f"error: {e}")
        return 1
    return 0
