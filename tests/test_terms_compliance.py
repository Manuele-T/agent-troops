"""Anthropic-terms compliance scan (written before the UI code).

The GUI launches the user's own, unmodified `claude` and lets it use whatever login the user
already has. So nothing in the product code may touch a Claude credential, set an ANTHROPIC_*
variable, import the Agent SDK, use --bare, use a shell string, alter the environment `claude`
inherits, or put "Claude Code" in a product title or heading. This test greps (and, for the
environment rules, parses) every source file in teambuild/ — which holds the UI code as
teambuild/ui/ — and fails on any hit.

Source: https://code.claude.com/docs/en/legal-and-compliance (re-read 2026-09-28).
The scan itself is tested on planted violations below, so "green" cannot mean "scanner broken".
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED_DIRS = [ROOT / "teambuild", ROOT / "plugin", ROOT / ".claude-plugin"]
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".html", ".css", ".json", ".toml", ".sh"}
# Image/icon files may not carry the vendor's name (logos).
ICON_SUFFIXES = {".png", ".svg", ".ico", ".jpg", ".jpeg", ".gif", ".webp"}

# The CLI's planner reads DEEPSEEK_API_KEY from a .env file. That mutates the CLI process's own
# environment, but the CLI never launches `claude`. The UI server must not do this (its
# environment is what `claude` inherits), so it is not on this list.
LOAD_DOTENV_ALLOWED = {"teambuild/cli.py"}

# --------------------------------------------------------------------------- #
# Text rules: (name, compiled regex). Any match in any scanned source file fails.
# --------------------------------------------------------------------------- #

TEXT_RULES: list[tuple[str, re.Pattern]] = [
    ("claude credential file", re.compile(r"\.credentials\.json|\.claude\.json", re.I)),
    ("keychain / keyring access", re.compile(r"keychain|keyring|find-generic-password|secret-tool|libsecret", re.I)),
    ("CLAUDE_CODE_OAUTH_TOKEN", re.compile(r"CLAUDE_CODE_OAUTH_TOKEN")),
    ("ANTHROPIC_* variable", re.compile(r"ANTHROPIC_[A-Z_]+")),
    ("oauth handling", re.compile(r"oauth", re.I)),
    ("session-key / session-token handling", re.compile(r"session[_-]?key|session[_-]?token", re.I)),
    ("Agent SDK", re.compile(r"claude[_-]agent[_-]sdk", re.I)),
    ("--bare flag", re.compile(r"(?<![\w-])--bare(?![\w-])")),
    ("shell=True", re.compile(r"shell\s*=\s*True")),
]

# "Claude Code" may appear in plain body copy ("runs Claude Code"), never in product chrome.
CHROME_PATTERNS = [
    re.compile(r"<title[^>]*>(?P<t>.*?)</title>", re.I | re.S),
    re.compile(r"<h[1-6][^>]*>(?P<t>.*?)</h[1-6]>", re.I | re.S),
    re.compile(r"document\.title\s*=\s*(?P<t>[^;\n]+)", re.I),
    re.compile(r"""\b(?:alt|aria-label|title)\s*=\s*["'](?P<t>[^"']*)["']""", re.I),
    re.compile(r"""\btitle\s*=\s*(?P<t>["'][^"'\n]*["'])"""),   # FastAPI(title="...") and friends
]
CLAUDE_CODE = re.compile(r"claude[\s_-]*code", re.I)

SUBPROCESS_FUNCS = {
    "run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput",
    "create_subprocess_exec", "create_subprocess_shell",
}
ENV_MUTATORS = {"update", "setdefault", "pop", "popitem", "clear", "__setitem__", "__delitem__"}


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute) and node.attr == "environ"
        and isinstance(node.value, ast.Name) and node.value.id == "os"
    )


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def ast_violations(rel: str, text: str) -> list[str]:
    """Python-only rules about the environment `claude` inherits."""
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel}: cannot parse ({exc.msg}); the compliance scan needs valid Python"]
    out: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            base = name.rsplit(".", 1)[-1]
            if base in SUBPROCESS_FUNCS and ("subprocess" in name or "asyncio" in name or base.startswith("create_subprocess")):
                if any(k.arg == "env" or k.arg is None for k in node.keywords):
                    out.append(f"{rel}:{line}: subprocess call passes env=... (the environment must be inherited unmodified)")
                if any(k.arg == "shell" for k in node.keywords):
                    out.append(f"{rel}:{line}: subprocess call passes shell=... (argument lists only)")
            if name in ("os.putenv", "os.unsetenv", "os.system", "os.popen"):
                out.append(f"{rel}:{line}: {name}() (changes the environment or uses a shell)")
            if isinstance(node.func, ast.Attribute) and _is_os_environ(node.func.value) and node.func.attr in ENV_MUTATORS:
                out.append(f"{rel}:{line}: os.environ.{node.func.attr}() mutates the environment")
            if base == "load_dotenv" and rel not in LOAD_DOTENV_ALLOWED:
                out.append(f"{rel}:{line}: load_dotenv() writes .env values into the environment")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and _is_os_environ(t.value):
                    out.append(f"{rel}:{line}: assignment to os.environ[...] mutates the environment")
    return out


def scan_text(rel: str, text: str) -> list[str]:
    """Every violation in one source file's text."""
    out: list[str] = []
    for name, rx in TEXT_RULES:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append(f"{rel}:{line}: {name}: {m.group(0)!r}")
    for rx in CHROME_PATTERNS:
        for m in rx.finditer(text):
            if CLAUDE_CODE.search(m.group("t")):
                line = text.count("\n", 0, m.start()) + 1
                out.append(f"{rel}:{line}: 'Claude Code' in a title/heading/label: {m.group('t').strip()[:60]!r}")
    if rel.endswith(".py"):
        out += ast_violations(rel, text)
    return out


def scan_tree(root: Path, dirs: list[Path]) -> list[str]:
    out: list[str] = []
    for d in dirs:
        for path in sorted(d.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix in ICON_SUFFIXES and re.search(r"anthropic|claude", path.name, re.I):
                out.append(f"{rel}: image/icon file named after the vendor")
                continue
            if path.suffix in SOURCE_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                out += scan_text(rel, text)
    return out


# --------------------------------------------------------------------------- #
# The real scan                                                                #
# --------------------------------------------------------------------------- #

def test_source_tree_is_clean():
    violations = scan_tree(ROOT, SCANNED_DIRS)
    assert not violations, "Anthropic-terms violations:\n" + "\n".join(violations)


def test_scan_covers_the_ui_code_once_it_exists():
    """The scan is by directory, so new UI files are covered automatically; this pins that."""
    files = [p for d in SCANNED_DIRS for p in d.rglob("*.py")]
    assert any(p.name == "architect.py" for p in files)


# --------------------------------------------------------------------------- #
# The scanner catches planted violations (so a green run means something)      #
# --------------------------------------------------------------------------- #

PLANTED = {
    "credentials file": ('p = "~/.claude/.credentials.json"\n', "claude credential file"),
    "claude.json": ('p = Path.home() / ".claude.json"\n', "claude credential file"),
    "keyring": ("import keyring\n", "keychain / keyring access"),
    "macos keychain": ('run(["security", "find-generic-password", "-s", "Claude"])\n', "keychain / keyring access"),
    "oauth env": ('tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")\n', "CLAUDE_CODE_OAUTH_TOKEN"),
    "anthropic key read": ('k = os.environ["ANTHROPIC_API_KEY"]\n', "ANTHROPIC_* variable"),
    "anthropic token": ('x = "ANTHROPIC_AUTH_TOKEN"\n', "ANTHROPIC_* variable"),
    "oauth word": ("def refresh_oauth(): pass\n", "oauth handling"),
    "sessionKey": ('c = cookies["sessionKey"]\n', "session-key / session-token handling"),
    "sdk import": ("import claude_agent_sdk\n", "Agent SDK"),
    "sdk dash": ('deps = ["claude-agent-sdk"]\n', "Agent SDK"),
    "bare flag": ('argv = ["claude", "-p", "hi", "--bare"]\n', "--bare flag"),
    "shell true": ('subprocess.run("claude -p hi", shell=True)\n', "shell=True"),
}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_planted_text_violation_is_caught(name):
    code, rule = PLANTED[name]
    hits = scan_text("teambuild/ui/planted.py", code)
    assert any(rule in h for h in hits), f"{name!r} was not flagged as {rule!r}: {hits}"


def test_planted_bare_lookalikes_are_not_flagged():
    # `--bare-metal`, `--barely` are not the flag
    assert not scan_text("x.py", 'a = ["--bare-metal", "--barely"]\n')


def test_planted_claude_code_in_title_is_caught():
    for html in (
        "<title>Claude Code Team Builder</title>",
        "<h1>Welcome to <b>Claude Code</b> teams</h1>",
        '<img src="x.png" alt="Claude Code logo">',
        'document.title = "Claude Code launcher";',
    ):
        hits = scan_text("teambuild/ui/frontend/index.html", html)
        assert any("'Claude Code' in a title" in h for h in hits), html
    assert any("title" in h for h in scan_text("app.py", 'app = FastAPI(title="Claude Code UI")\n'))


def test_plain_body_copy_may_say_it_runs_claude_code():
    ok = "<p>This page runs Claude Code on your machine, using your own login.</p>\n<title>Team Builder</title>"
    assert scan_text("teambuild/ui/frontend/index.html", ok) == []


@pytest.mark.parametrize("code", [
    'subprocess.run(["claude"], env={"X": "1"})\n',
    'subprocess.Popen(["claude"], env=dict(os.environ))\n',
    'await asyncio.create_subprocess_exec("claude", env=e)\n',
    'subprocess.run(["claude"], **kw)\n',
    'os.environ["PATH"] = "/x"\n',
    'os.environ.update({"A": "b"})\n',
    'os.environ.setdefault("A", "b")\n',
    'os.putenv("A", "b")\n',
    'del os.environ["A"]\n',
    'os.system("claude")\n',
    'subprocess.run(["claude"], shell=False)\n',
])
def test_planted_environment_violations_are_caught(code):
    assert ast_violations("teambuild/ui/planted.py", code), code


def test_reading_the_environment_is_fine():
    ok = textwrap.dedent('''
        import os, subprocess
        home = os.environ.get("HOME")
        subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
    ''')
    assert ast_violations("teambuild/ui/ok.py", ok) == []


def test_load_dotenv_only_allowed_in_the_cli():
    code = "from dotenv import load_dotenv\nload_dotenv()\n"
    assert ast_violations("teambuild/ui/server.py", code)
    assert ast_violations("teambuild/cli.py", code) == []


def test_vendor_named_icon_is_caught(tmp_path):
    (tmp_path / "teambuild").mkdir()
    (tmp_path / "teambuild" / "claude-logo.svg").write_text("<svg/>")
    hits = scan_tree(tmp_path, [tmp_path / "teambuild"])
    assert any("named after the vendor" in h for h in hits)


def test_scan_tree_end_to_end_on_a_planted_tree(tmp_path):
    pkg = tmp_path / "teambuild" / "ui"
    pkg.mkdir(parents=True)
    (pkg / "good.py").write_text("import os\nprint(os.environ.get('HOME'))\n")
    (pkg / "bad.py").write_text('import subprocess\nsubprocess.run("claude", shell=True)\n')
    hits = scan_tree(tmp_path, [tmp_path / "teambuild"])
    assert any(h.startswith("teambuild/ui/bad.py") for h in hits)
    assert not any("good.py" in h for h in hits)


# --------------------------------------------------------------------------- #
# No headless `claude -p`, anywhere                                            #
# --------------------------------------------------------------------------- #
# Anthropic's headless docs call `claude -p` the Agent SDK's CLI form, and the legal page says products
# using the Agent SDK should use API-key authentication. This tool only ever opens the user's own
# interactive Claude Code, so no headless flag may appear in its code at all. (Until 2026-10-07 an
# automated-run mode for API-key logins was allowed behind a login check; it was removed.)

HEADLESS_FLAGS = {"-p", "--print", "--output-format", "--input-format", "--max-budget-usd",
                  "--include-partial-messages"}
HEADLESS_SCANNED = [ROOT / "teambuild", ROOT / "plugin"]


def headless_violations(rel: str, text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"{rel}: cannot parse ({exc.msg})"]
    return [f"{rel}:{n.lineno}: headless flag {n.value!r}" for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and n.value in HEADLESS_FLAGS]


def scan_headless(root: Path, dirs: list[Path]) -> list[str]:
    out: list[str] = []
    for d in dirs:
        for path in sorted(d.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            out += headless_violations(rel, path.read_text(encoding="utf-8", errors="replace"))
    return out


def test_no_headless_claude_anywhere():
    violations = scan_headless(ROOT, HEADLESS_SCANNED)
    assert not violations, "headless `claude -p` flags in the code:\n" + "\n".join(violations)


@pytest.mark.parametrize("code", [
    'def plan(u):\n    return ["claude", "-p", u]\n',
    'argv = ["claude", "--print", "hi"]\n',
    'def x():\n    return ["claude", "--output-format", "json"]\n',
    'def headless_launch(permit, claude):\n    require_permit(permit)\n    return [claude, "-p"]\n',   # no exception any more
])
def test_planted_headless_violations_are_caught(code):
    assert headless_violations("teambuild/ui/anything.py", code), code
