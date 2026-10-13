"""Board backend: main-session events, refusals, boundaries. Skipped if the board deps are absent."""

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from teambuild.board import hook_listener  # noqa: E402
from teambuild.board.app import app  # noqa: E402
from teambuild.cc_scaffold import scaffold  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture()
def board():
    b = hook_listener.board
    b.agents.clear()
    b.raw_log.clear()
    b.run = hook_listener._new_run()
    b.delegations.clear()
    b._seen.clear()
    hook_listener._BOUNDARIES_CACHE.clear()
    return b


@pytest.fixture()
def team_repo(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_code_review.json").read_text()), tmp_path)
    return str(tmp_path)


def hook(kind, agent_type, agent_id=None, cwd=None, **extra):
    p = {"hook_event_name": kind, "agent_type": agent_type, **extra}
    if agent_id:
        p["agent_id"] = agent_id
    if cwd:
        p["cwd"] = cwd
    return p


def test_a_plain_session_is_the_main_robot(board):
    """Guard mode: a session with no agent name gets one robot, its prompt is the job, its last message the answer."""
    board.handle_hook({"hook_event_name": "UserPromptSubmit", "prompt": "fix the build", "session_id": "s"})
    board.handle_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "make"}})
    assert board.agents["main"].name == "main session" and board.agents["main"].state == "working"
    board.handle_hook({"hook_event_name": "SubagentStop", "last_assistant_message": "helper text"})
    board.handle_hook({"hook_event_name": "Stop", "last_assistant_message": "fixed"})
    assert board.run["job"] == "fix the build" and board.run["final"] == "fixed"
    assert board.run["reports"] == []          # Claude Code's own helpers are not reports


def test_the_same_event_sent_twice_counts_once(board):
    """A team's emit.py and guard mode's both send each event."""
    e = {"hook_event_name": "PreToolUse", "agent_type": "x-researcher", "tool_name": "WebSearch",
         "tool_input": {"query": "q"}, "tool_use_id": "t1"}
    board.handle_hook(dict(e))
    board.handle_hook(dict(e))
    assert len(board.agents["x-researcher"].activity) == 1
    board.handle_hook({**e, "tool_use_id": "t2"})
    assert len(board.agents["x-researcher"].activity) == 2


def test_main_session_role_without_agent_id_gets_a_robot(board):
    board.handle_hook(hook("PreToolUse", "cr-orchestrator", tool_name="Agent", tool_input={"prompt": "x"}))
    a = board.agents["cr-orchestrator"]
    assert a.state == "working" and a.current_task_id == "main:cr-orchestrator"


def test_stop_marks_the_main_session_done(board):
    board.handle_hook(hook("PreToolUse", "cr-orchestrator", tool_name="Agent"))
    board.handle_hook(hook("Stop", "cr-orchestrator"))
    assert board.agents["cr-orchestrator"].state == "done"


def test_guard_block_turns_the_role_blocked_and_is_logged_in_its_feed(board):
    q = board.subscribe()
    board.handle_hook(hook("PreToolUse", "cr-poster", "s1", tool_name="Bash", tool_input={"command": "rm -rf /"}))
    board.handle_hook(hook("GuardBlocked", "cr-poster", "s1", tool_name="Bash", guard="bash_guard",
                           reason="command is not on this role's allowlist"))
    a = board.agents["cr-poster"]
    assert a.state == "blocked"
    item = a.activity[-1]
    assert item["kind"] == "blocked" and item["label"] == "Bash" and "allowlist" in item["text"]
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert events[-1]["kind"] == "blocked" and events[-1]["payload"]["assignee"] == "cr-poster"
    board.unsubscribe(q)


def test_a_role_that_was_blocked_recovers_on_its_next_call(board):
    board.handle_hook(hook("GuardBlocked", "cr-poster", "s1", tool_name="Bash", reason="no"))
    assert board.agents["cr-poster"].state == "blocked"
    board.handle_hook(hook("PreToolUse", "cr-poster", "s1", tool_name="Bash", tool_input={"command": "gh pr view 1"}))
    assert board.agents["cr-poster"].state == "working"


def test_guard_block_from_a_main_session_role_without_agent_id(board):
    board.handle_hook(hook("GuardBlocked", "cr-poster", tool_name="Bash", reason="no", guard="bash_guard"))
    assert board.agents["cr-poster"].state == "blocked"


def test_boundaries_come_from_the_repos_agent_files(board, team_repo):
    board.handle_hook(hook("PreToolUse", "cr-poster", "s1", cwd=team_repo, tool_name="Bash"))
    board.handle_hook(hook("PreToolUse", "cr-writer", "b1", cwd=team_repo, tool_name="Write"))
    shipper = board.agents["cr-poster"].boundaries
    assert shipper["legs"] == ["exfiltration", "private_data"] and shipper["gated"] and shipper["guarded"]
    assert board.agents["cr-writer"].boundaries["legs"] == ["private_data"]


def test_boundaries_are_none_without_a_readable_repo(board, tmp_path):
    board.handle_hook(hook("PreToolUse", "cr-poster", "s1", tool_name="Bash"))
    board.handle_hook(hook("PreToolUse", "cr-writer", "b1", cwd=str(tmp_path), tool_name="Write"))
    assert board.agents["cr-poster"].boundaries is None
    assert board.agents["cr-writer"].boundaries is None


def test_http_endpoints_expose_state_boundaries_and_refusals(board, team_repo):
    client = TestClient(app, base_url="http://127.0.0.1:8123")
    client.post("/hook", json=hook("PreToolUse", "cr-poster", "s1", cwd=team_repo, tool_name="Bash",
                                   tool_input={"command": "ls /"}))
    client.post("/hook", json=hook("GuardBlocked", "cr-poster", "s1", cwd=team_repo, tool_name="Bash",
                                   guard="bash_guard", reason="command is not on this role's allowlist"))
    (agent,) = client.get("/agents").json()
    assert agent["id"] == "cr-poster" and agent["state"] == "blocked"
    assert agent["boundaries"]["gated"] is True and "private_data" in agent["boundaries"]["legs"]
    feed = client.get("/agents/cr-poster/activity").json()
    assert feed["available"] and feed["items"][-1]["kind"] == "blocked"


def test_the_board_cannot_give_agents_work():
    """The task-queue endpoints from the board's original project are gone: the board only shows."""
    client = TestClient(app, base_url="http://127.0.0.1:8123")
    assert client.post("/act", json={"agentId": "x", "action": "spawn"}).status_code in (404, 405)
    assert client.get("/tasks").status_code == 404


def test_a_real_guard_process_reaches_the_board_endpoint(board, team_repo, monkeypatch):
    """The guard's POST body must be exactly what the board understands."""

    posted = []
    monkeypatch.setattr(hook_listener.board, "handle_hook", lambda payload: posted.append(payload))
    client = TestClient(app, base_url="http://127.0.0.1:8123")
    guard = ROOT / "teambuild" / "templates" / "bash_guard.py"
    # run the guard with a stub board URL by capturing what notify_board would send
    import importlib.util
    spec = importlib.util.spec_from_file_location("bash_guard_mod", guard)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sent = {}

    class FakeResp:
        def close(self):
            pass

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        return FakeResp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod.notify_board({"agent_type": "cr-poster", "agent_id": "s1", "cwd": team_repo}, "Bash", "not allowed")
    client.post("/hook", json=sent["body"])
    assert posted and posted[0]["hook_event_name"] == "GuardBlocked"
    assert posted[0]["agent_type"] == "cr-poster" and posted[0]["reason"] == "not allowed"


# ---- results: the job, what each member was asked, reports, final answer ------ #

def test_a_job_its_delegations_reports_and_answer_are_kept_as_text(board):
    board.handle_hook(hook("PreToolUse", "rs-researcher", "x0", tool_name="WebSearch"))   # an older job's leftovers
    board.handle_hook(hook("UserPromptSubmit", "rs-orchestrator", prompt="Find note app prices."))
    assert board.run["job"] == "Find note app prices." and board.run["reports"] == [] and board.run["final"] is None
    board.handle_hook(hook("PreToolUse", "rs-orchestrator", tool_name="Agent",
                           tool_input={"subagent_type": "rs-researcher", "prompt": "Get Notion's price."}))
    board.handle_hook(hook("SubagentStop", "rs-researcher", "a1", last_assistant_message="Notion: $10"))
    board.handle_hook(hook("Stop", "rs-orchestrator", last_assistant_message="| App | Price |\n|---|---|\n| Notion | $10 |"))
    assert board.run["asked"] == {"rs-researcher": "Get Notion's price."}
    assert [(r["agent"], r["text"]) for r in board.run["reports"]] == [("rs-researcher", "Notion: $10")]
    assert board.run["final"].startswith("| App | Price |")
    run = TestClient(app, base_url="http://127.0.0.1:8123").get("/run").json()
    assert run["job"] == "Find note app prices." and run["final"] == board.run["final"]


def test_a_new_job_starts_a_clean_run(board):
    board.handle_hook(hook("UserPromptSubmit", "rs-orchestrator", prompt="first"))
    board.handle_hook(hook("Stop", "rs-orchestrator", last_assistant_message="answer one"))
    board.handle_hook(hook("UserPromptSubmit", "rs-orchestrator", prompt="second"))
    assert board.run["job"] == "second" and board.run["final"] is None and board.run["asked"] == {}


def test_very_long_reports_are_cut_and_odd_values_ignored(board):
    board.handle_hook(hook("SubagentStop", "rs-writer", "a2", last_assistant_message="x" * 60_000))
    (r,) = board.run["reports"]
    assert len(r["text"]) < 50_100 and r["text"].endswith("longer than the board shows]")
    board.handle_hook(hook("SubagentStop", "rs-analyst", "a3", last_assistant_message={"not": "text"}))
    assert len(board.run["reports"]) == 1


NOTIFICATION = """<task-notification>
<task-id>adf140b80436e4ff8</task-id>
<tool-use-id>toolu_1</tool-use-id>
<status>completed</status>
<summary>Agent "Gather prices" finished</summary>
<result>## Notion
Plus: $10</result>
</task-notification>"""


def test_a_background_member_reporting_back_is_not_a_new_job(board):
    """Seen live: a member run in the background reports back as a <task-notification> message."""
    board.handle_hook(hook("UserPromptSubmit", "rs-orchestrator", prompt="Find note app prices."))
    board.handle_hook(hook("PreToolUse", "rs-orchestrator", tool_name="Agent", tool_use_id="toolu_1",
                           tool_input={"subagent_type": "rs-researcher", "prompt": "Get prices."}))
    board.handle_hook(hook("UserPromptSubmit", "rs-orchestrator", prompt=NOTIFICATION))
    assert board.run["job"] == "Find note app prices." and board.run["asked"] == {"rs-researcher": "Get prices."}
    assert [(r["agent"], r["text"]) for r in board.run["reports"]] == [("rs-researcher", "## Notion\nPlus: $10")]
    board.handle_hook(hook("SubagentStop", "rs-researcher", "a1", last_assistant_message="## Notion\nPlus: $10"))
    assert len(board.run["reports"]) == 1                                  # the same report only once


def test_claude_codes_own_helpers_are_not_team_reports(board):
    """Seen live: an internal helper stops under the main agent's own name ("(No obvious follow-up)")."""
    board.handle_hook(hook("SubagentStop", "rs-orchestrator", "h1", last_assistant_message="(No obvious follow-up)"))
    assert board.run["reports"] == []
