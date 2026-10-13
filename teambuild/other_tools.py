"""The same checks for the other AI coding tools in a repository: Cursor, VS Code / GitHub Copilot, OpenAI Codex
CLI and Gemini CLI, plus the instruction files several tools share (AGENTS.md, CLAUDE.md).

Only files whose name and format were confirmed in each tool's own documentation (2026-10-06) are read:

  Cursor           .cursor/mcp.json ("mcpServers")            cursor.com/docs/context/mcp
                   .cursor/hooks.json ("hooks": {event: [{command}]})   cursor.com/docs/agent/hooks
                   .cursor/rules/**/*.mdc, *.md                cursor.com/docs/context/rules
  VS Code/Copilot  .vscode/mcp.json ("servers")                code.visualstudio.com/docs/copilot/customization/mcp-servers
                   .github/copilot-instructions.md, .github/instructions/**/*.instructions.md
                                                               .../customization/custom-instructions
  Codex CLI        .codex/config.toml ([mcp_servers.NAME])     learn.chatgpt.com/docs/extend/mcp
                   AGENTS.md, AGENTS.override.md               learn.chatgpt.com/docs/agent-configuration/agents-md
  Gemini CLI       .gemini/settings.json ("mcpServers", "hooks")    geminicli.com/docs/tools/mcp-server, /docs/hooks
                   GEMINI.md                                   geminicli.com/docs/cli/gemini-md
                   .gemini/commands/**/*.toml (!{...} runs a shell command)  geminicli.com/docs/cli/custom-commands

Not read, because the format could not be confirmed: Cursor's older `.cursorrules`, Codex hooks.

The checks are the ones setup_audit.py applies to Claude Code (remote or unpinned MCP servers, secrets written
into a config, hooks that rewrite settings, answer "allow", download-and-run or reach the network, hidden text in
instruction files), plus Gemini's `"trust": true`, which skips every confirmation for that server's tools.
Instruction files are looked for in the project and its sub-folders (dependency and build folders skipped).
Pattern checks, offline: they find the obvious cases and miss clever ones.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from teambuild.setup_audit import _load_json, _read, judge_hook, judge_instructions, judge_mcp_server

try:
    import tomllib
except ImportError:          # Python < 3.11 is not supported anyway; keep the module importable
    tomllib = None

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build", "target", ".tox",
             ".mypy_cache", ".pytest_cache", "site-packages", ".next", "vendor"}
SHARED_INSTRUCTIONS = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "GEMINI.md")
MAX_DEPTH = 6
MAX_FILES = 300
GEMINI_SHELL_RE = re.compile(r"!\{([^}\n]+)\}")


def _walk(target: Path):
    """Files under target, skipping dependency/build folders and symlinked folders; bounded."""
    count = 0
    base_depth = len(target.parts)
    for root, dirs, files in os.walk(target):
        here = Path(root)
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (here / d).is_symlink()
                         and len(here.parts) - base_depth < MAX_DEPTH)
        for f in sorted(files):
            count += 1
            if count > MAX_FILES * 50:
                return
            yield here / f


def _scripts(command: str, base: Path, target: Path) -> list[Path]:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    out = []
    for w in words:
        p = Path(w) if Path(w).is_absolute() else base / w
        try:
            if p.is_file() and p.resolve().is_relative_to(target.resolve()):
                out.append(p)
        except OSError:
            continue
    return out


def _servers(source: str, servers: object, **kw) -> list[tuple[str, str]]:
    notes = []
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            notes += judge_mcp_server(source, str(name), cfg, **kw)
    return notes


def cursor_notes(target: Path) -> list[tuple[str, str]]:
    notes = _servers(".cursor/mcp.json", _load_json(target / ".cursor" / "mcp.json").get("mcpServers"))
    hooks = _load_json(target / ".cursor" / "hooks.json").get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            for e in entries if isinstance(entries, list) else []:
                cmd = e.get("command") if isinstance(e, dict) else None
                if isinstance(cmd, str):
                    notes += judge_hook(f".cursor/hooks.json {event} hook `{cmd[:80]}`", cmd,
                                        _scripts(cmd, target, target), target, tool="Cursor")
    rules = target / ".cursor" / "rules"
    if rules.is_dir():
        for p in sorted(rules.rglob("*")):
            if p.is_file() and p.suffix in (".mdc", ".md"):
                notes += judge_instructions(p, target)
    return notes


def vscode_notes(target: Path) -> list[tuple[str, str]]:
    notes = _servers(".vscode/mcp.json", _load_json(target / ".vscode" / "mcp.json").get("servers"))
    gh = target / ".github"
    files = [gh / "copilot-instructions.md"] + (sorted((gh / "instructions").rglob("*.instructions.md"))
                                                if (gh / "instructions").is_dir() else [])
    for p in files:
        if p.is_file():
            notes += judge_instructions(p, target)
    return notes


def codex_notes(target: Path) -> list[tuple[str, str]]:
    path = target / ".codex" / "config.toml"
    if not path.is_file() or tomllib is None:
        return []
    try:
        data = tomllib.loads(_read(path))
    except (tomllib.TOMLDecodeError, ValueError):
        return [("WARN", ".codex/config.toml could not be read as TOML, so it was not checked")]
    return _servers(".codex/config.toml", data.get("mcp_servers"), header_keys=("http_headers",))


def gemini_notes(target: Path) -> list[tuple[str, str]]:
    settings = _load_json(target / ".gemini" / "settings.json")
    servers = settings.get("mcpServers")
    notes = _servers(".gemini/settings.json", servers, url_keys=("url", "httpUrl"))
    if isinstance(servers, dict):
        for name, cfg in servers.items():
            if isinstance(cfg, dict) and cfg.get("trust") is True:
                notes.append(("WARN", f".gemini/settings.json server `{name}` has \"trust\": true: Gemini CLI runs its "
                                      "tools without asking you"))
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event, entries in hooks.items():
            for e in entries if isinstance(entries, list) else []:
                for h in e.get("hooks", []) if isinstance(e, dict) else []:
                    cmd = h.get("command") if isinstance(h, dict) else None
                    if isinstance(cmd, str):
                        notes += judge_hook(f".gemini/settings.json {event} hook `{cmd[:80]}`", cmd,
                                            _scripts(cmd, target, target), target, tool="Gemini CLI")
    commands = target / ".gemini" / "commands"
    if commands.is_dir():
        for p in sorted(commands.rglob("*.toml")):
            notes += judge_instructions(p, target, bang_lines=True, bang_re=GEMINI_SHELL_RE)
    return notes


def shared_instruction_notes(target: Path) -> list[tuple[str, str]]:
    """AGENTS.md, AGENTS.override.md, CLAUDE.md, GEMINI.md anywhere in the project (bounded walk)."""
    notes, seen = [], 0
    for p in _walk(target):
        if p.name in SHARED_INSTRUCTIONS:
            seen += 1
            if seen > MAX_FILES:
                notes.append(("INFO", f"more than {MAX_FILES} instruction files: only the first {MAX_FILES} were checked"))
                break
            notes += judge_instructions(p, target)
    return notes


TOOLS = {
    "Cursor": (".cursor/mcp.json", ".cursor/hooks.json", ".cursor/rules"),
    "VS Code / Copilot": (".vscode/mcp.json", ".github/copilot-instructions.md", ".github/instructions"),
    "Codex CLI": (".codex/config.toml",),
    "Gemini CLI": (".gemini/settings.json", ".gemini/commands"),
}


def found_tools(target: Path) -> list[str]:
    return [name for name, paths in TOOLS.items() if any((target / p).exists() for p in paths)]


def other_tool_notes(target: str | Path) -> list[tuple[str, str]]:
    target = Path(target)
    notes = cursor_notes(target) + vscode_notes(target) + codex_notes(target) + gemini_notes(target)
    notes += shared_instruction_notes(target)
    tools = found_tools(target)
    if tools:
        notes.append(("INFO", f"other AI coding tools configured here: {', '.join(tools)} (their MCP servers, hooks "
                              "and instruction files were checked too)"))
    return notes
