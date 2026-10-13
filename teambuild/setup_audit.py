"""The rest of a project's Claude Code setup: hooks, MCP servers, skills, commands, plugins.

Agents are only part of what can turn Claude Code against its user. Each check here answers to a
known attack or mistake:

  * Hooks run with the user's rights on every matching event, outside any agent's tool list.
    A hook that rewrites Claude Code's settings or permissions removes the human approval for good
    (PromptArmor's 2026 write-up on hijacked marketplace plugins); one that answers permission
    prompts with "allow" approves calls nobody saw; one that downloads and runs code, or sends data to
    another computer, is a back door. `disableAllHooks` switches every guard off.
  * MCP servers in .mcp.json: a remote server receives whatever its tools are given; an unpinned
    `npx`/`uvx` package runs whatever version is newest at start (supply chain); a secret written
    into the file usually ends up committed. `enableAllProjectMcpServers` approves them all unasked.
  * Skills, slash commands and agent files are read by the model: hidden HTML comments, invisible
    Unicode, "ignore previous instructions"-style text and `curl ... | sh` are how instructions get
    smuggled in; a `!` line in a command or skill runs a shell command as soon as it is used.

Pattern checks, offline, no model: they find the obvious cases and miss clever ones, which the report
says. Secret values are never printed.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

TEMPLATES = Path(__file__).parent / "templates"
OUR_HOOK_SCRIPTS = ("emit.py", "bash_guard.py", "read_guard.py", "flow_guard.py", "board_gate.py")
SCRIPT_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1")
MAX_SCAN_BYTES = 512 * 1024

SETTINGS_FILE = r"(?:settings(?:\.local)?\.json|\.claude\.json|managed-settings\.json)"
SHELL_SETTINGS_WRITE_RE = re.compile(
    rf"(?:>>?|\btee(?:\s+-a)?)\s*[\"']?[^\s\"'|;&]*{SETTINGS_FILE}|\bsed\s+-i[^\n]*{SETTINGS_FILE}|\b(?:cp|mv|ln)\b[^\n]*{SETTINGS_FILE}",
    re.I)
SETTINGS_MENTION_RE = re.compile(rf"{SETTINGS_FILE}|[\"']permissions[\"'][^\n]{{0,80}}[\"']allow[\"']", re.I)
CODE_WRITE_RE = re.compile(r"write_text|writeFileSync|writeFile\(|json\.dump\(|\bopen\([^)]*,\s*['\"][wa]", re.I)
DOWNLOAD_RUN_RE = re.compile(
    r"(?:curl|wget)[^\n|;]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b|(?:curl|wget)[^\n|;]*\|\s*python|base64\s+(?:-d|--decode)"
    r"|\beval\s*(?:\(\s*[^)\s]|[\"'$])|/dev/tcp/|\bnc\s+-|\bncat\b|\bnetcat\b|\bdig\b[^\n]*\btxt\b", re.I)
NETWORK_RE = re.compile(r"\b(?:curl|wget)\b|requests\.(?:get|post)|urllib\.request|fetch\(|https?://(?!127\.0\.0\.1|localhost)[\w.-]+", re.I)
ALLOW_DECISION_RE = re.compile(r"(?:permissionDecision|permission|decision)[\"']?\s*[:=]\s*[\"']allow", re.I)
HIDDEN_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.S)
# What makes a hidden comment worth a warning: an address, a command, secrets, sending, or override wording.
RISKY_COMMENT_RE = re.compile(
    r"https?://|\b(?:curl|wget|bash|sh -c|powershell|base64|eval)\b|~/\.|\.ssh\b|\.pem\b|\.env\b|\bid_rsa\b"
    r"|\b(?:send|upload|post|exfiltrat\w*|forward|leak|transmit)\b|\b(?:secret|token|password|credential|api[_ -]?key|private key)s?\b",
    re.I)
MARKER_RE = re.compile(r"\s*[\w:./-]{0,60}\s*")      # one token: BEGIN:rules, prettier-ignore, OPENWIKI:START
INVISIBLE_RE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
INJECTION_RE = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|rules)|disregard (?:the|your|all) "
    r"(?:previous |prior )?(?:instructions|rules)|do not (?:tell|inform|mention (?:this )?to) the user|without (?:asking|telling) the user"
    r"|you are now (?:in )?(?:developer|dan|admin) mode", re.I)
BANG_LINE_RE = re.compile(r"!`([^`\n]+)`")
SECRET_NAME_RE = re.compile(r"key|token|secret|password|passwd|credential", re.I)


def _read(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _short(text: str, n: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# --------------------------------------------------------------------------- #
# Hooks                                                                        #
# --------------------------------------------------------------------------- #

def _hook_commands(settings: dict, source: str) -> list[tuple[str, str, str]]:
    """(event, command, source file) for every command hook."""
    out = []
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            for h in entry.get("hooks", []) if isinstance(entry, dict) else []:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.append((str(event), h["command"], source))
    return out


def _scripts_in(command: str, target: Path) -> list[Path]:
    """Script files inside the project that a hook command runs."""
    try:
        words = shlex.split(command.replace("$CLAUDE_PROJECT_DIR", str(target)).replace("${CLAUDE_PROJECT_DIR}", str(target)))
    except ValueError:
        words = command.split()
    found = []
    for w in words:
        if w.endswith(SCRIPT_SUFFIXES):
            p = Path(w) if Path(w).is_absolute() else target / w
            if p.is_file():
                found.append(p)
    return found


def judge_hook(label: str, command: str, scripts: list[Path], target: Path, tool: str = "Claude Code") -> list[tuple[str, str]]:
    """Findings for one hook command (of any tool) and the scripts inside the project it runs."""
    notes: list[tuple[str, str]] = []
    text = command + "\n" + "\n".join(_read(s) for s in scripts)
    if SHELL_SETTINGS_WRITE_RE.search(text) or (SETTINGS_MENTION_RE.search(text) and CODE_WRITE_RE.search(text)):
        notes.append(("FAIL", f"{label} can change {tool}'s settings or permissions: a hook can use that to "
                              "remove the approval prompt for good (the hijacked-plugin attack)"))
    if ALLOW_DECISION_RE.search(text):
        notes.append(("WARN", f"{label} answers permission prompts with \"allow\": it can approve tool calls "
                              "without asking you"))
    if DOWNLOAD_RUN_RE.search(text):
        notes.append(("WARN", f"{label} downloads and runs code, decodes hidden data or opens a raw connection"))
    elif NETWORK_RE.search(text):
        notes.append(("WARN", f"{label} contacts another computer; hooks see every tool call's input, so this "
                              "can carry data out"))
    if not notes:
        inside = [str(s.relative_to(target)) for s in scripts if s.is_relative_to(target)]
        notes.append(("INFO", f"{label}: not one of agent-troops' hooks; "
                              + (f"nothing suspicious found in {', '.join(inside)}" if inside else "check what it does")))
    return notes


def hook_notes(target: Path) -> list[tuple[str, str]]:
    claude = target / ".claude"
    notes: list[tuple[str, str]] = []
    commands = []
    has_our_guards = False
    for name in ("settings.json", "settings.local.json"):
        data = _load_json(claude / name)
        commands += _hook_commands(data, name)
        if data.get("disableAllHooks") is True:
            notes.append(("WARN", f"{name}: disableAllHooks is on, so no hook runs (guards and approvals included)"))
    for event, command, source in commands:
        scripts = _scripts_in(command, target)
        hooks_dir = (claude / "hooks").resolve()
        ours = [s for s in scripts if s.resolve().parent in (hooks_dir, hooks_dir / "agent-troops-guard")
                and s.name in OUR_HOOK_SCRIPTS]
        if ours:
            has_our_guards = True
            if any(s.name == "board_gate.py" for s in ours) and " --all" in command:
                notes.append(("INFO", f"guard mode is on ({source}): web calls and shell commands that can reach "
                                      "the network wait for Allow/Deny on the board, for every session here"
                                      + (", MCP tools too" if " --mcp" in command else "")
                                      + ("; sensitive files are marked" if " --sensitive" in command else "")))
            for s in ours:
                if _read(s) != _read(TEMPLATES / s.name):
                    notes.append(("WARN", f"{s.relative_to(target)} differs from this version of agent-troops (an older "
                                          "copy, or edited): re-create the team, or compare it by hand"))
            continue
        notes += judge_hook(f"{source} {event} hook `{_short(command, 80)}`", command, scripts, target)
    if has_our_guards:
        notes = [("FAIL", t) if t.startswith(("settings.json: disableAllHooks", "settings.local.json: disableAllHooks"))
                 else (lvl, t) for lvl, t in notes]
    return notes


# --------------------------------------------------------------------------- #
# MCP servers and plugins                                                      #
# --------------------------------------------------------------------------- #

def _unpinned(command: str, args: list[str]) -> str | None:
    runner = Path(command).name
    if runner not in ("npx", "bunx", "pnpx", "uvx", "pipx"):
        return None
    words = [a for a in args if isinstance(a, str) and not a.startswith("-") and a != "run"]
    if not words:
        return None
    pkg = words[0]
    if runner in ("uvx", "pipx"):
        return None if ("==" in pkg or re.search(r"@\d", pkg)) else pkg
    name = pkg[1:] if pkg.startswith("@") else pkg
    return None if "@" in name else pkg


def _is_reference(value: str) -> bool:
    """${VAR}, ${env:VAR}, ${input:x} (VS Code, Cursor), $VAR (Gemini), %VAR% (Windows): read at start, not stored."""
    return value.startswith("$") or bool(re.fullmatch(r"%\w+%", value))


BEARER_RE = re.compile(r"(?i)bearer\s+[^$%\s]")


def judge_mcp_server(source: str, name: str, cfg: object, url_keys: tuple[str, ...] = ("url",),
                     header_keys: tuple[str, ...] = ("headers",)) -> list[tuple[str, str]]:
    """Findings for one MCP server entry, from any tool's config: remote, unpinned package, literal secret."""
    if not isinstance(cfg, dict):
        return []
    notes: list[tuple[str, str]] = []
    label = f"{source} server `{name}`"
    for key in url_keys:
        url = cfg.get(key)
        if isinstance(url, str) and url:
            host = re.sub(r"^[a-z]+://", "", url).split("/")[0]
            if not host.startswith(("127.0.0.1", "localhost")):
                notes.append(("WARN", f"{label} is remote ({host}): whatever its tools are given is sent there"))
                break
    command = cfg.get("command")
    args = cfg.get("args") if isinstance(cfg.get("args"), list) else []
    if isinstance(command, str):
        pkg = _unpinned(command, args)
        if pkg:
            notes.append(("WARN", f"{label} runs `{_short(pkg, 60)}` without a pinned version: "
                                  "each start may run newer, unreviewed code"))
    pairs: dict = {}
    for key in ("env",) + header_keys:
        if isinstance(cfg.get(key), dict):
            pairs.update(cfg[key])
    literal = sorted(str(k) for k, v in pairs.items() if isinstance(v, str) and v and not _is_reference(v)
                     and (SECRET_NAME_RE.search(str(k)) or BEARER_RE.match(v)))
    if literal:
        notes.append(("WARN", f"{label} has a secret written into the file ({', '.join(literal)}; values not shown): "
                              "reference an environment variable instead"))
    return notes


def mcp_notes(target: Path) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    mcp = _load_json(target / ".mcp.json")
    servers = mcp.get("mcpServers") if isinstance(mcp.get("mcpServers"), dict) else {}
    for name, cfg in servers.items():
        notes += judge_mcp_server(".mcp.json", name, cfg)
    if servers:
        notes.append(("INFO", f"MCP servers in .mcp.json: {', '.join(sorted(servers))}. Their tools are not "
                              "classified by this audit; check what each can read and send"))
    for name in ("settings.json", "settings.local.json"):
        data = _load_json(target / ".claude" / name)
        if data.get("enableAllProjectMcpServers") is True:
            notes.append(("WARN", f"{name}: enableAllProjectMcpServers approves every server in .mcp.json without asking"))
        plugins = data.get("enabledPlugins")
        if isinstance(plugins, dict) and any(plugins.values()):
            notes.append(("INFO", f"{name}: plugins enabled ({', '.join(sorted(k for k, v in plugins.items() if v))}). "
                                  "Their hooks, agents and servers live outside this project and are not audited here"))
    return notes


# --------------------------------------------------------------------------- #
# Skills, commands, agent files                                                #
# --------------------------------------------------------------------------- #

def _instruction_files(target: Path) -> list[Path]:
    claude = target / ".claude"
    files: list[Path] = []
    for sub, pattern in (("skills", "**/*.md"), ("commands", "**/*.md"), ("agents", "*.md")):
        d = claude / sub
        if d.is_dir():
            files += sorted(p for p in d.glob(pattern) if p.is_file())
    skills = claude / "skills"
    if skills.is_dir():
        files += sorted(p for p in skills.rglob("*") if p.is_file() and p.suffix in SCRIPT_SUFFIXES)
    return files


def instruction_notes(target: Path) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    for path in _instruction_files(target):
        rel = str(path.relative_to(target)).replace("\\", "/")
        notes += judge_instructions(path, target, bang_lines=path.suffix == ".md" and ".claude/agents" not in rel)
    return notes


def judge_instructions(path: Path, target: Path, bang_lines: bool = False,
                       bang_re: re.Pattern = BANG_LINE_RE) -> list[tuple[str, str]]:
    """Findings for one file the model reads (or that a skill runs), from any tool."""
    notes: list[tuple[str, str]] = []
    rel = path.relative_to(target)
    text = _read(path)
    if not text:
        return notes
    if INVISIBLE_RE.search(text):
        notes.append(("WARN", f"{rel} contains invisible characters, a known way to hide instructions from a reader"))
    hidden = [c for c in HIDDEN_COMMENT_RE.findall(text) if c.strip() and not MARKER_RE.fullmatch(c)]
    if hidden and path.suffix in (".md", ".mdc"):
        risky = [c for c in hidden if RISKY_COMMENT_RE.search(c) or INJECTION_RE.search(c)]
        shown = (risky or hidden)[0]
        notes.append(("WARN" if risky else "INFO",
                      f"{rel} has hidden HTML comment(s) the model reads but a rendered page does not show"
                      + (" (with an address, a command, secrets or sending in them)" if risky else "")
                      + f": \"{_short(shown, 70)}\""))
    m = INJECTION_RE.search(text)
    if m:
        notes.append(("WARN", f"{rel} contains instruction-override wording: \"{_short(m.group(0), 60)}\""))
    if DOWNLOAD_RUN_RE.search(text):
        notes.append(("WARN", f"{rel} downloads and runs code, decodes hidden data or opens a raw connection"))
    if bang_lines:
        for cmd in bang_re.findall(text):
            level = "WARN" if (DOWNLOAD_RUN_RE.search(cmd) or NETWORK_RE.search(cmd)) else "INFO"
            notes.append((level, f"{rel} runs a shell command as soon as it is used: `{_short(cmd, 70)}`"))
    return notes


def setup_notes(target: str | Path) -> list[tuple[str, str]]:
    target = Path(target)
    from teambuild.other_tools import other_tool_notes      # local: other_tools builds on this module
    notes = hook_notes(target) + mcp_notes(target) + instruction_notes(target) + other_tool_notes(target)
    return apply_policy(target, notes)


FORBIDDEN = {      # policy "forbid" key -> what the matching finding says
    "remote_mcp": lambda t: " server `" in t and " is remote (" in t,
    "unpinned_mcp": lambda t: "without a pinned version" in t,
    "unknown_hooks": lambda t: "not one of agent-troops' hooks" in t,
}


def _guard_in_shared_settings(target: Path) -> bool:
    data = _load_json(target / ".claude" / "settings.json")
    return any("board_gate.py" in c and " --all" in c for _, c, _ in _hook_commands(data, "settings.json"))


def apply_policy(target: Path, notes: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The repository's .agent-troops.json, enforced: invalid = FAIL, require_guard, forbid -> FAIL."""
    from teambuild import policy_file
    path = target / policy_file.NAME
    if not path.is_file():
        return notes
    policy = policy_file.load(path)
    if not policy.valid:
        return notes + [("FAIL", f"{policy_file.NAME} is invalid ({'; '.join(policy.errors)}), so none of its rules "
                                 "apply and the board allows nothing automatically for this repository")]
    out = []
    for lvl, text in notes:
        rule = next((k for k, match in FORBIDDEN.items() if policy.forbids(k) and match(text)), None)
        out.append(("FAIL", f"{text} [forbidden by {policy_file.NAME}: {rule}]") if rule else (lvl, text))
    guard = _guard_in_shared_settings(target)
    if policy.require_guard and not guard:
        out.append(("FAIL", f"{policy_file.NAME} requires guard mode, but .claude/settings.json does not install it "
                            "(team-build guard install --shared, then commit .claude/)"))
    elif policy.sensitive and not guard:
        out.append(("WARN", f"{policy_file.NAME} lists sensitive files, but nothing in this repository enforces them: "
                            "install guard mode with --shared, or each person uses the plugin or a local install"))
    out.append(("INFO", policy_file.describe(policy).replace(str(path), policy_file.NAME)))
    return out
