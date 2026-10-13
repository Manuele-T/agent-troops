"""Interactive runs: opening the user's own `claude --agent` in a terminal (Phase 11, step 3)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from teambuild.ui import terminal  # noqa: E402
from teambuild.ui.app import create_app  # noqa: E402

TOKEN = "test-token-0123456789"
H = {"x-team-build-token": TOKEN}
HEADLESS_FLAGS = {"-p", "--print", "--output-format", "--input-format", "--max-budget-usd", "--settings",
                  "--allowedTools", "--dangerously-skip-permissions", "--permission-mode"}


def make_folder(tmp_path, team="cr"):
    folder = tmp_path / "proj"
    (folder / ".claude" / "agents").mkdir(parents=True)
    (folder / ".claude" / "agents" / f"{team}-orchestrator.md").write_text("---\nname: x\n---\n")
    return folder


class Recorder:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, argv, **kw):
        if self.fail:
            raise OSError("cannot start")
        self.calls.append((argv, kw))
        return SimpleNamespace(pid=1)


@pytest.mark.parametrize("name, expected_tail", [
    ("gnome-terminal", ["--", "/bin/claude", "--agent", "cr-orchestrator"]),
    ("konsole", ["-e", "/bin/claude", "--agent", "cr-orchestrator"]),
    ("xfce4-terminal", ["-x", "/bin/claude", "--agent", "cr-orchestrator"]),
    ("x-terminal-emulator", ["-e", "/bin/claude", "--agent", "cr-orchestrator"]),
])
def test_each_terminal_gets_an_interactive_command(tmp_path, name, expected_tail):
    folder = make_folder(tmp_path)
    rec = Recorder()
    out = terminal.open_team(folder, "cr-orchestrator", "/bin/claude",
                             which=lambda n: f"/usr/bin/{n}" if n == name else None,
                             platform="linux", popen=rec)
    assert out["opened"] and out["terminal"] == name
    argv, kw = rec.calls[0]
    assert argv[0] == f"/usr/bin/{name}" and argv[-4:] == expected_tail
    assert not HEADLESS_FLAGS & set(argv)                 # interactive: no headless or approval flags
    assert kw["cwd"] == str(folder) and "env" not in kw and kw["start_new_session"] is True   # inherited as-is
    if name in ("gnome-terminal", "xfce4-terminal"):
        assert f"--working-directory={folder}" in argv
    assert out["command"] == f"cd {folder} && claude --agent cr-orchestrator"


def test_preference_order(tmp_path):
    folder = make_folder(tmp_path)
    rec = Recorder()
    terminal.open_team(folder, "cr-orchestrator", "claude", which=lambda n: f"/x/{n}", platform="linux", popen=rec)
    assert rec.calls[0][0][0] == "/x/gnome-terminal"


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_other_systems_get_the_command_to_copy(tmp_path, platform):
    rec = Recorder()
    out = terminal.open_team(make_folder(tmp_path), "cr-orchestrator", "claude",
                             which=lambda n: "/x", platform=platform, popen=rec)
    assert out == {"opened": False, "terminal": None, "command": out["command"]} and rec.calls == []


def test_no_terminal_or_a_failed_start_falls_back_to_copy(tmp_path):
    folder = make_folder(tmp_path)
    assert not terminal.open_team(folder, "cr-orchestrator", "claude", which=lambda n: None, platform="linux")["opened"]
    out = terminal.open_team(folder, "cr-orchestrator", "claude", which=lambda n: "/x",
                             platform="linux", popen=Recorder(fail=True))
    assert not out["opened"] and "claude --agent cr-orchestrator" in out["command"]


@pytest.mark.parametrize("team", ["cr-poster", "../x-orchestrator", "x;rm -rf ~-orchestrator", "zz-orchestrator"])
def test_bad_or_missing_teams_are_refused(tmp_path, team):
    with pytest.raises(terminal.TerminalError):
        terminal.open_team(make_folder(tmp_path), team, "claude", which=lambda n: "/x", platform="linux", popen=Recorder())


def test_copyable_command_quotes_odd_folders(tmp_path):
    odd = tmp_path / "my proj; echo hi"
    assert terminal.copyable(odd, "cr-orchestrator") == f"cd '{odd}' && claude --agent cr-orchestrator"


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #

@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    folder = make_folder(home)
    rec = []

    def opener(f, team, exe, job=None):
        rec.append((f, team, exe, job))
        return {"opened": True, "terminal": "fake", "command": terminal.copyable(f, team, job)}

    def app_with(login: str):
        return create_app(token=TOKEN, home=home, allowed_hosts={"testserver"}, config_file=tmp_path / "c.json",
                          find_claude=lambda: "/bin/claude", terminal_opener=opener)

    return SimpleNamespace(home=home, folder=folder, rec=rec, app_with=app_with)


def test_terminal_endpoint_opens_the_team(env):
    with TestClient(env.app_with("subscription")) as c:
        r = c.post("/api/terminal", json={"folder": str(env.folder), "team": "cr-orchestrator"}, headers=H)
        assert r.status_code == 200 and r.json()["opened"]
        assert env.rec == [(env.folder, "cr-orchestrator", "/bin/claude", None)]
        assert c.post("/api/terminal", json={"folder": str(env.folder), "team": "cr-orchestrator"}).status_code == 403
        assert c.post("/api/terminal", json={"folder": "/etc", "team": "cr-orchestrator"}, headers=H).status_code == 400


def test_run_mode_only_says_whether_a_terminal_can_be_opened(env):
    with TestClient(env.app_with("subscription")) as c:
        assert set(c.get("/api/run-mode").json()) == {"terminal"}
        assert c.post("/api/runs", json={"folder": str(env.folder), "team": "cr-orchestrator", "job": "go"},
                      headers=H).status_code in (404, 405)          # no automated runs any more


# --------------------------------------------------------------------------- #
# Starting on a job typed in the page                                         #
# --------------------------------------------------------------------------- #

JOB = "Compare Notion's and Obsidian's prices; don't guess. $(touch /tmp/x) `id` 'quoted' \"too\""


@pytest.mark.parametrize("name", ["gnome-terminal", "konsole", "xfce4-terminal", "x-terminal-emulator"])
def test_the_job_is_one_argument_after_the_agent(tmp_path, name):
    rec = Recorder()
    terminal.open_team(make_folder(tmp_path), "cr-orchestrator", "/bin/claude", JOB,
                       which=lambda n: f"/usr/bin/{n}" if n == name else None, platform="linux", popen=rec)
    argv, kw = rec.calls[0]
    assert argv[-4:] == ["/bin/claude", "--agent", "cr-orchestrator", JOB]       # exactly as typed, not shell text
    assert "shell" not in kw and not HEADLESS_FLAGS & set(argv)


def test_the_command_to_copy_quotes_the_job_for_a_shell(tmp_path):
    cmd = terminal.copyable(tmp_path / "my proj", "cr-orchestrator", JOB)
    import shlex
    assert shlex.split(cmd)[-4:] == ["claude", "--agent", "cr-orchestrator", JOB]
    assert terminal.copyable(tmp_path, "cr-orchestrator", "   ") == terminal.copyable(tmp_path, "cr-orchestrator")


@pytest.mark.parametrize("job", ["--dangerously-skip-permissions", "-p hello", "x" * 20_001, "a\x00b", 42])
def test_a_job_that_could_be_read_as_an_option_or_is_odd_is_refused(tmp_path, job):
    rec = Recorder()
    with pytest.raises(terminal.TerminalError):
        terminal.open_team(make_folder(tmp_path), "cr-orchestrator", "claude", job,
                           which=lambda n: "/x", platform="linux", popen=rec)
    assert rec.calls == []


def test_terminal_endpoint_passes_the_job_and_refuses_a_dash(env, tmp_path):
    real = create_app(token=TOKEN, home=env.home, allowed_hosts={"testserver"}, config_file=tmp_path / "c2.json",
                      find_claude=lambda: "/bin/claude",
                      terminal_opener=lambda f, t, e, j=None: terminal.open_team(f, t, e, j, which=lambda n: None))
    with TestClient(env.app_with("subscription")) as c:
        c.post("/api/terminal", json={"folder": str(env.folder), "team": "cr-orchestrator", "job": "Find prices."}, headers=H)
        assert env.rec[-1][3] == "Find prices."
    with TestClient(real) as c:
        r = c.post("/api/terminal", json={"folder": str(env.folder), "team": "cr-orchestrator", "job": "-p x"}, headers=H)
        assert r.status_code == 400 and "dash" in r.json()["detail"]
        r = c.post("/api/terminal", json={"folder": str(env.folder), "team": "cr-orchestrator", "job": "Find prices."}, headers=H)
        assert r.status_code == 200 and r.json()["command"].endswith("claude --agent cr-orchestrator 'Find prices.'")
