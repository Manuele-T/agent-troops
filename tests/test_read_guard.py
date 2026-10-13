"""Read fence: pure decision logic, the real hook process, and scaffolding — all offline."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from teambuild.cc_scaffold import destroy, scaffold
from teambuild.templates.read_guard import decide, literal_prefix

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "teambuild" / "templates" / "read_guard.py"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture()
def project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "a.txt").write_text("a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    return proj, outside


def d(tool, tool_input, proj, **kw):
    return decide(tool, tool_input, cwd=str(proj), root=str(proj), **kw)


def test_reads_inside_the_project_are_allowed(project):
    proj, _ = project
    assert d("Read", {"file_path": str(proj / "src" / "a.txt")}, proj)[0]
    assert d("Read", {"file_path": "src/a.txt"}, proj)[0]              # relative to cwd
    assert d("Read", {"file_path": str(proj / "not_yet_created.txt")}, proj)[0]
    assert d("Grep", {"pattern": "x"}, proj)[0]                         # no path = cwd
    assert d("Grep", {"pattern": "x", "path": "src"}, proj)[0]
    assert d("Glob", {"pattern": "**/*.py"}, proj)[0]
    assert d("Glob", {"pattern": "*.md", "path": str(proj)}, proj)[0]


def test_reads_outside_the_project_are_blocked(project):
    proj, outside = project
    assert not d("Read", {"file_path": str(outside / "secret.txt")}, proj)[0]
    assert not d("Read", {"file_path": "/etc/hostname"}, proj)[0]
    assert not d("Grep", {"pattern": "x", "path": str(outside)}, proj)[0]
    assert not d("Glob", {"pattern": "*", "path": str(outside)}, proj)[0]


@pytest.mark.parametrize("path", ["../outside/secret.txt", "src/../../outside/secret.txt", "./../outside"])
def test_parent_traversal_is_blocked(project, path):
    proj, _ = project
    assert not d("Read", {"file_path": path}, proj)[0]


def test_symlink_pointing_outside_is_blocked(project):
    proj, outside = project
    (proj / "link").symlink_to(outside / "secret.txt")
    (proj / "dirlink").symlink_to(outside, target_is_directory=True)
    assert not d("Read", {"file_path": str(proj / "link")}, proj)[0]
    assert not d("Read", {"file_path": str(proj / "dirlink" / "secret.txt")}, proj)[0]
    assert not d("Grep", {"pattern": "x", "path": str(proj / "dirlink")}, proj)[0]


def test_sibling_directory_with_same_prefix_is_not_inside(project):
    proj, _ = project
    sibling = proj.parent / (proj.name + "-evil")
    sibling.mkdir()
    (sibling / "x.txt").write_text("x")
    assert not d("Read", {"file_path": str(sibling / "x.txt")}, proj)[0]


def test_home_expansion_is_resolved(project):
    proj, _ = project
    assert not d("Read", {"file_path": "~/.bashrc"}, proj)[0]


@pytest.mark.parametrize("pattern", ["/etc/*", "/etc/**/*.conf", "~/.ssh/*", "../*", "../../*.txt", "src/../../*"])
def test_glob_patterns_that_reach_outside_are_blocked(project, pattern):
    proj, _ = project
    assert not d("Glob", {"pattern": pattern}, proj)[0]


def test_absolute_glob_pattern_inside_the_project_is_allowed(project):
    proj, _ = project
    assert d("Glob", {"pattern": str(proj) + "/src/*.txt"}, proj)[0]


def test_scratchpad_dir_can_be_allowed_explicitly(project, tmp_path):
    proj, outside = project
    assert not d("Read", {"file_path": str(outside / "secret.txt")}, proj)[0]
    assert d("Read", {"file_path": str(outside / "secret.txt")}, proj, extra_roots=(str(outside),))[0]


def test_literal_prefix():
    assert literal_prefix("/etc/**/*.conf") == "/etc/"
    assert literal_prefix("/etc/passwd") == "/etc"
    assert literal_prefix("**/*.py") == "."


# ---- the real hook process ------------------------------------------------------ #

def run_hook(event, project, *args):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(project), TEAMBUILD_BOARD_URL="http://127.0.0.1:9/hook")
    return subprocess.run([sys.executable, str(GUARD), *args],
                          input=event if isinstance(event, str) else json.dumps(event),
                          capture_output=True, text=True, env=env)


def ev(tool, tool_input, proj, agent_type="rs-researcher"):
    e = {"tool_name": tool, "tool_input": tool_input, "cwd": str(proj)}
    if agent_type:
        e["agent_type"] = agent_type
    return e


def test_hook_blocks_a_team_role_reading_outside(project):
    proj, outside = project
    p = run_hook(ev("Read", {"file_path": str(outside / "secret.txt")}, proj), proj, "--roles", "rs-researcher,rs-writer")
    assert p.returncode == 2 and "read_guard: blocked" in p.stderr


def test_hook_allows_a_team_role_reading_inside(project):
    proj, _ = project
    p = run_hook(ev("Read", {"file_path": str(proj / "src" / "a.txt")}, proj), proj, "--roles", "rs-researcher")
    assert p.returncode == 0


@pytest.mark.parametrize("agent_type", ["someone-else", None])
def test_hook_leaves_other_agents_and_the_main_thread_alone(project, agent_type):
    proj, outside = project
    p = run_hook(ev("Read", {"file_path": str(outside / "secret.txt")}, proj, agent_type), proj, "--roles", "rs-researcher")
    assert p.returncode == 0


def test_hook_ignores_other_tools(project):
    proj, _ = project
    p = run_hook(ev("Bash", {"command": "cat /etc/passwd"}, proj), proj, "--roles", "rs-researcher")
    assert p.returncode == 0


@pytest.mark.parametrize("bad", ["", "not json", "[]", "null"])
def test_hook_fails_closed_on_malformed_input(project, bad):
    proj, _ = project
    assert run_hook(bad, proj, "--roles", "rs-researcher").returncode == 2


# ---- scaffolding ---------------------------------------------------------------- #

def guard_commands(target):
    s = json.loads((target / ".claude" / "settings.json").read_text())
    return [h["command"] for e in s["hooks"]["PreToolUse"] for h in e["hooks"] if "read_guard.py" in h["command"]]


def test_fence_is_off_by_default(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_research.json").read_text()), tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "read_guard.py").exists()
    assert guard_commands(tmp_path) == []


def test_fence_flag_installs_script_and_scoped_hook(tmp_path):
    roster = json.loads((FIXTURES / "roster_research.json").read_text())
    scaffold(roster, tmp_path, fence_reads=True)
    assert (tmp_path / ".claude" / "hooks" / "read_guard.py").exists()
    (cmd,) = guard_commands(tmp_path)
    for r in roster["roles"]:
        assert r["id"] in cmd
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entry = next(e for e in s["hooks"]["PreToolUse"] if "read_guard" in json.dumps(e))
    assert entry["matcher"] == "Read|Grep|Glob"
    assert "permissions" not in s or "blockReadsOutsideWorkingDirectories" not in s.get("permissions", {})


def test_fence_is_idempotent_and_destroy_removes_everything_it_added(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"theme": "dark"}))
    roster = json.loads((FIXTURES / "roster_research.json").read_text())
    scaffold(roster, tmp_path, fence_reads=True)
    scaffold(roster, tmp_path, fence_reads=True)
    assert len(guard_commands(tmp_path)) == 1
    destroy("rs", tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "read_guard.py").exists()
    s = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert s["theme"] == "dark" and guard_commands(tmp_path) == []


def test_two_fenced_teams_each_keep_their_own_hook(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_research.json").read_text()), tmp_path, fence_reads=True)
    scaffold(json.loads((FIXTURES / "roster_code_review.json").read_text()), tmp_path, fence_reads=True)
    assert len(guard_commands(tmp_path)) == 2
    destroy("rs", tmp_path)
    (cmd,) = guard_commands(tmp_path)
    assert "cr-" in cmd and (tmp_path / ".claude" / "hooks" / "read_guard.py").exists()


def test_fence_dry_run_writes_nothing(tmp_path):
    actions = scaffold(json.loads((FIXTURES / "roster_research.json").read_text()), tmp_path,
                       dry_run=True, fence_reads=True)
    assert any("read_guard.py" in a for a in actions)
    assert not (tmp_path / ".claude").exists()
