"""One-way flow (Phase 12): once private data has entered a session, no more web. Offline.

The relay this closes: web text reaches the researcher, comes back to the orchestrator, which asks the
analyst for private files and then asks the researcher to "look up" a URL carrying them. No member holds
the trifecta; the team does. flow_guard.py judges the orchestrator's delegations and refuses the last
step, whatever the models would have done."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from teambuild.architect import Architect
from teambuild.audit import audit
from teambuild.cc_scaffold import destroy, scaffold
from teambuild.templates import flow_guard as fg

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS = ROOT / "teambuild" / "blueprints.json"
GUARD = ROOT / "teambuild" / "templates" / "flow_guard.py"
ORCH, PRIVATE, OUTBOUND = "rs-orchestrator", ["rs-analyst", "rs-writer"], ["rs-researcher"]


@pytest.fixture
def arch():
    return Architect(BLUEPRINTS)


def delegate(to, session="s1", agent=ORCH, tool="Agent"):
    ev = {"hook_event_name": "PreToolUse", "tool_name": tool, "agent_type": agent,
          "tool_input": {"subagent_type": to, "prompt": "do it"}}
    if session is not None:
        ev["session_id"] = session
    return ev


def run_hook(event, state_dir, mode="block", stdin=None, extra=()):
    argv = [sys.executable, str(GUARD), "--orchestrator", ORCH, "--private", ",".join(PRIVATE),
            "--outbound", ",".join(OUTBOUND), "--mode", mode, *extra]
    env = {**os.environ, "XDG_RUNTIME_DIR": str(state_dir)}      # test only: point the state at tmp
    p = subprocess.run(argv, input=stdin if stdin is not None else json.dumps(event),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout, p.stderr


# --------------------------------------------------------------------------- #
# The decision (pure)                                                          #
# --------------------------------------------------------------------------- #

def test_research_then_private_then_research_is_refused():
    state = {"tainted": False, "by": []}
    v, _, state = fg.decide(delegate("rs-researcher"), set(PRIVATE), set(OUTBOUND), state)
    assert v == "allow" and state["tainted"] is False
    v, _, state = fg.decide(delegate("rs-analyst"), set(PRIVATE), set(OUTBOUND), state)
    assert v == "allow" and state == {"tainted": True, "by": ["rs-analyst"]}
    v, why, state = fg.decide(delegate("rs-researcher"), set(PRIVATE), set(OUTBOUND), state)
    assert v == "refuse" and "rs-analyst" in why and "new session" in why


def test_private_work_can_continue_after_taint():
    state = {"tainted": True, "by": ["rs-analyst"]}
    v, _, state = fg.decide(delegate("rs-writer"), set(PRIVATE), set(OUTBOUND), state)
    assert v == "allow" and state["by"] == ["rs-analyst", "rs-writer"]


@pytest.mark.parametrize("target", [None, "", "general-purpose", "some-other-agent"])
def test_unknown_targets_count_as_both_kinds(target):
    ev = delegate(target)
    v, _, state = fg.decide(ev, set(PRIVATE), set(OUTBOUND), {"tainted": False, "by": []})
    assert v == "allow" and state["tainted"] is True                 # unknown = may read files: taints
    v, _, _ = fg.decide(ev, set(PRIVATE), set(OUTBOUND), {"tainted": True, "by": ["x"]})
    assert v == "refuse"                                              # unknown = may reach the web: refused


def test_task_tool_name_is_judged_too():
    v, _, _ = fg.decide(delegate("rs-researcher", tool="Task"), set(PRIVATE), set(OUTBOUND), {"tainted": True, "by": ["a"]})
    assert v == "refuse"


def test_messages_to_running_agents_close_after_taint():
    msg = {"tool_name": "SendMessage", "agent_type": ORCH, "tool_input": {"to": "a1b2"}, "session_id": "s1"}
    assert fg.decide(msg, set(PRIVATE), set(OUTBOUND), {"tainted": False, "by": []})[0] == "allow"
    assert fg.decide(msg, set(PRIVATE), set(OUTBOUND), {"tainted": True, "by": ["a"]})[0] == "refuse"


# --------------------------------------------------------------------------- #
# The hook process                                                             #
# --------------------------------------------------------------------------- #

def test_hook_blocks_the_relay_step_with_a_reason_the_model_sees(tmp_path):
    assert run_hook(delegate("rs-researcher"), tmp_path)[0] == 0
    assert run_hook(delegate("rs-analyst"), tmp_path)[0] == 0
    code, out, err = run_hook(delegate("rs-researcher"), tmp_path)
    assert code == 2 and out == "" and "flow_guard: blocked" in err and "rs-analyst" in err


def test_ask_mode_hands_the_step_to_a_human(tmp_path):
    run_hook(delegate("rs-analyst"), tmp_path, mode="ask")
    code, out, _ = run_hook(delegate("rs-researcher"), tmp_path, mode="ask")
    decision = json.loads(out)["hookSpecificOutput"]
    assert code == 0 and decision["permissionDecision"] == "ask" and decision["hookEventName"] == "PreToolUse"
    assert "Allow it anyway" in decision["permissionDecisionReason"]


def test_sessions_do_not_share_taint(tmp_path):
    run_hook(delegate("rs-analyst", session="A"), tmp_path)
    assert run_hook(delegate("rs-researcher", session="B"), tmp_path)[0] == 0
    assert run_hook(delegate("rs-researcher", session="A"), tmp_path)[0] == 2


def test_taint_survives_across_hook_calls_like_a_resumed_session(tmp_path):
    run_hook(delegate("rs-writer", session="same"), tmp_path)
    for _ in range(3):
        assert run_hook(delegate("rs-researcher", session="same"), tmp_path)[0] == 2


def test_other_agents_and_tools_are_not_judged(tmp_path):
    run_hook(delegate("rs-analyst"), tmp_path)
    assert run_hook(delegate("rs-researcher", agent=None), tmp_path)[0] == 0          # the user's own session
    assert run_hook(delegate("rs-researcher", agent="rs-writer"), tmp_path)[0] == 0   # not the orchestrator
    read = {"tool_name": "Read", "agent_type": ORCH, "tool_input": {"file_path": "x"}, "session_id": "s1"}
    assert run_hook(read, tmp_path)[0] == 0


@pytest.mark.parametrize("stdin", ["not json", "", "[]"])
def test_garbage_input_fails_closed(tmp_path, stdin):
    assert run_hook(None, tmp_path, stdin=stdin)[0] == 2


@pytest.mark.parametrize("session", [None, "", "../../etc/passwd", "a/b", "x" * 200, 7])
def test_missing_or_odd_session_ids_fail_closed(tmp_path, session):
    ev = delegate("rs-researcher")
    ev["session_id"] = session
    if session is None:
        del ev["session_id"]
    assert run_hook(ev, tmp_path)[0] == 2
    assert not any(p.name == "passwd" for p in tmp_path.rglob("*"))


def test_corrupt_or_unreadable_state_fails_closed(tmp_path):
    run_hook(delegate("rs-researcher", session="c"), tmp_path)        # creates the folder
    folder = tmp_path / "agent-troops" / "flow"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "c.json").write_text("{broken")
    assert run_hook(delegate("rs-researcher", session="c"), tmp_path)[0] == 2
    (folder / "d.json").write_text(json.dumps({"tainted": "no"}))
    assert run_hook(delegate("rs-researcher", session="d"), tmp_path)[0] == 2


def test_bad_arguments_fail_closed(tmp_path):
    assert run_hook(delegate("rs-researcher"), tmp_path, mode="maybe")[0] == 2
    p = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(delegate("rs-researcher")),
                       capture_output=True, text=True, env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)})
    assert p.returncode == 2


def test_state_is_private_and_outside_the_project(tmp_path):
    run_hook(delegate("rs-analyst", session="p"), tmp_path)
    folder = tmp_path / "agent-troops" / "flow"
    f = folder / "p.json"
    assert json.loads(f.read_text()) == {"tainted": True, "by": ["rs-analyst"]}
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700 and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not list(folder.glob(".tmp-*"))


def test_old_state_files_are_pruned(tmp_path):
    folder = tmp_path / "flow"
    folder.mkdir()
    old = folder / "old.json"
    old.write_text("{}")
    os.utime(old, (0, 0))
    fg.prune(str(folder))
    assert not old.exists()


def test_taint_is_recorded_before_the_private_member_runs(tmp_path):
    """Written at delegation time (PreToolUse), so a crash mid-run can't leave the session looking clean."""
    run_hook(delegate("rs-analyst", session="early"), tmp_path)
    assert json.loads((tmp_path / "agent-troops" / "flow" / "early.json").read_text())["tainted"] is True


# --------------------------------------------------------------------------- #
# Classification, validator, scaffolder, template, audit                       #
# --------------------------------------------------------------------------- #

def test_classification_per_blueprint(arch):
    assert arch.flow_roles(arch.fallback_roster("research", "rs")) == (PRIVATE, OUTBOUND)
    assert arch.flow_roles(arch.fallback_roster("code_review", "cr"))[1] == []        # no web member
    assert arch.flow_roles(arch.fallback_roster("dependency_check", "dc"))[1] == ["dc-researcher"]


def test_validator_refuses_an_open_relay_unless_the_person_opts_out(arch):
    r = arch.fallback_roster("research", "rs")
    assert arch.validate(r) == []                                      # absent = block
    assert arch.validate({**r, "flow_guard": "ask"}) == []
    open_ = {**r, "flow_guard": "off"}
    assert any("relay open" in e for e in arch.validate(open_))
    assert arch.validate(open_, allow_relay_open=True) == []
    assert any("flow_guard must be" in e for e in arch.validate({**r, "flow_guard": "maybe"}))
    assert arch.validate({**arch.fallback_roster("code_review", "cr"), "flow_guard": "off"}) == []  # nothing to close


def test_a_planner_cannot_switch_the_guard_off(arch):
    roster = {**arch.fallback_roster("research", "rs"), "flow_guard": "off"}
    calls = []

    def llm(system, user):
        calls.append(1)
        return json.dumps(roster)

    from teambuild.planner import assemble_with_fallback
    a = Architect(BLUEPRINTS, llm_call=llm)
    out, source, _ = assemble_with_fallback(a, "b", "research", {}, None, "rs")
    assert source == "offline" and out.get("flow_guard", "block") != "off" and len(calls) == 2


def test_scaffold_installs_wires_and_destroy_removes(arch, tmp_path):
    scaffold(arch.fallback_roster("research", "rs"), tmp_path)
    assert (tmp_path / ".claude" / "hooks" / "flow_guard.py").read_text() == GUARD.read_text()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    entry = next(e for e in settings["hooks"]["PreToolUse"] if "flow_guard" in json.dumps(e))
    assert entry["matcher"] == "Agent|Task|SendMessage"
    cmd = entry["hooks"][0]["command"]
    assert "--orchestrator rs-orchestrator --private rs-analyst,rs-writer --outbound rs-researcher --mode block" in cmd
    orch = (tmp_path / ".claude" / "agents" / "rs-orchestrator.md").read_text()
    assert "One-way flow" in orch and "start a new session" in orch
    assert "One-way flow" not in (tmp_path / ".claude" / "agents" / "rs-analyst.md").read_text()
    destroy("rs", tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "flow_guard.py").exists()


def test_ask_mode_and_off_are_written_as_chosen(arch, tmp_path):
    scaffold({**arch.fallback_roster("research", "rs"), "flow_guard": "ask"}, tmp_path / "a")
    assert "--mode ask" in (tmp_path / "a" / ".claude" / "settings.json").read_text()
    scaffold({**arch.fallback_roster("research", "rs"), "flow_guard": "off"}, tmp_path / "b")
    assert not (tmp_path / "b" / ".claude" / "hooks" / "flow_guard.py").exists()
    assert "flow_guard" not in (tmp_path / "b" / ".claude" / "settings.json").read_text()


def test_teams_without_a_web_member_get_no_flow_guard(arch, tmp_path):
    scaffold(arch.fallback_roster("code_review", "cr"), tmp_path)
    assert not (tmp_path / ".claude" / "hooks" / "flow_guard.py").exists()
    assert "One-way flow" not in (tmp_path / ".claude" / "agents" / "cr-orchestrator.md").read_text()


def test_audit_team_level(arch, tmp_path):
    scaffold(arch.fallback_roster("research", "rs"), tmp_path / "on")
    res = audit(tmp_path / "on")
    assert res.overall in ("PASS", "WARN") and any("one-way flow on" in t for _, t in res.repo_notes)
    (tmp_path / "on" / ".claude" / "hooks" / "flow_guard.py").unlink()
    res = audit(tmp_path / "on")
    assert res.failed and any(l == "FAIL" and "fails open" in t for l, t in res.repo_notes)
    scaffold({**arch.fallback_roster("research", "rs"), "flow_guard": "off"}, tmp_path / "off")
    res = audit(tmp_path / "off")
    assert any(l == "WARN" and "relay open" in t for l, t in res.repo_notes) and res.overall == "WARN"


def test_audit_catches_a_hand_written_team_with_no_orchestrator(tmp_path):
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "web.md").write_text("---\nname: web\ndescription: fetches\ntools: WebFetch, WebSearch\n---\nx\n")
    (agents / "files.md").write_text("---\nname: files\ndescription: reads\ntools: Read, Grep\n---\nx\n")
    res = audit(tmp_path)
    assert any("agents outside any team: relay open" in t for _, t in res.repo_notes)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def cli(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DEEPSEEK", "TEAMBUILD"))}
    env.update({"DEEPSEEK_API_KEY": "", "TEAMBUILD_CONFIG": str(tmp_path / "none.json")})
    return subprocess.run([sys.executable, "-m", "teambuild.cli", "--no-ai", "market research on note apps",
                           "--prefix", "rs", "--dry-run", *args], input="1\n1\n1\n", capture_output=True,
                          text=True, cwd=tmp_path, env=env, timeout=60)


def test_cli_default_block_ask_and_opt_out(tmp_path):
    out = cli(tmp_path).stdout
    assert "one-way flow: on (block)" in out and "write .claude/hooks/flow_guard.py" in out
    assert "--mode ask" in cli(tmp_path, "--flow", "ask").stdout
    off = cli(tmp_path, "--no-flow-guard").stdout
    assert "[RELAY OPEN]" in off and "flow_guard.py" not in off
    assert cli(tmp_path, "--flow", "maybe").returncode != 0
