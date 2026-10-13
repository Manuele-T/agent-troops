"""The repository's shared policy (.agent-troops.json): validation, the board's site lists, guard mode's sensitive
files, the audit's enforcement and the approval log's record of it. Offline."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from teambuild import guard, policy_file
from teambuild.approval_log import render_markdown
from teambuild.audit import audit
from teambuild.setup_audit import setup_notes
from tests.test_board_gate import Board, free_port

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "teambuild" / "templates" / "board_gate.py"


def write_policy(folder: Path, **data) -> Path:
    path = folder / policy_file.NAME
    path.write_text(json.dumps({"version": 1, **data}))
    return path


# --------------------------------------------------------------------------- #
# The file                                                                     #
# --------------------------------------------------------------------------- #

def test_the_template_is_valid_and_sets_nothing(tmp_path):
    p = policy_file.load(policy_file.write_template(tmp_path))
    assert p.valid and "no rules set" in policy_file.describe(p)
    with pytest.raises(FileExistsError):
        policy_file.write_template(tmp_path)


@pytest.mark.parametrize("data, error", [
    ({"version": 1, "forbid": {"remote_mpc": True}}, 'unknown key "forbid.remote_mpc"'),      # a typo is an error
    ({"version": 1, "site": {}}, 'unknown key "site"'),
    ({"version": 2}, '"version" must be 1'),
    ({}, '"version" must be 1'),
    ({"version": 1, "sites": {"block": ["https://pastebin.com"]}}, "is not a site name"),
    ({"version": 1, "sites": {"allow": "docs.python.org"}}, '"sites.allow" must be a list'),
    ({"version": 1, "sensitive": "config/*"}, '"sensitive" must be a list'),
    ({"version": 1, "after_sensitive": "warn"}, '"after_sensitive" must be'),
    ({"version": 1, "require_guard": "yes"}, '"require_guard" must be true or false'),
    ({"version": 1, "forbid": {"remote_mcp": 1}}, '"forbid.remote_mcp" must be true or false'),
    (["not", "an", "object"], "must hold a JSON object"),
])
def test_mistakes_are_errors(data, error):
    assert any(error in e for e in policy_file.validate(data))


def test_an_invalid_file_relaxes_nothing(tmp_path):
    (tmp_path / policy_file.NAME).write_text("{not json")
    p = policy_file.for_folder(tmp_path)
    assert not p.valid and p.sites("allow") == set() and p.sensitive == [] and p.after_sensitive == "block"


def test_found_from_a_sub_folder_with_a_hash(tmp_path):
    path = write_policy(tmp_path, sites={"block": ["pastebin.com"]})
    (tmp_path / "src" / "deep").mkdir(parents=True)
    p = policy_file.for_folder(tmp_path / "src" / "deep")
    assert p.path == path and len(p.digest) == 64 and p.sites("block") == {"pastebin.com"}


# --------------------------------------------------------------------------- #
# The board                                                                    #
# --------------------------------------------------------------------------- #

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from teambuild.board import app as board_app  # noqa: E402
from teambuild.board.gate import gate  # noqa: E402

OK = {"x-board-token": board_app.TOKEN}


@pytest.fixture
def client(tmp_path, monkeypatch):
    gate.pending.clear()
    gate.new_job()
    monkeypatch.setattr(gate, "sites_path", tmp_path / "sites.json")
    monkeypatch.setattr(board_app, "LOG_PATH", tmp_path / "approvals.jsonl")
    board_app.recent_decisions.clear()
    with TestClient(board_app.app, base_url="http://127.0.0.1:8123") as c:
        yield c


def fetch(url, cwd):
    return {"agent_type": "main", "tool_name": "WebFetch", "detail": url, "cwd": str(cwd), "session_id": "s"}


def pending(client):
    end = time.time() + 5
    while time.time() < end:
        p = client.get("/gate").json()["pending"]
        if p:
            return p
        time.sleep(0.02)
    raise AssertionError("no pending approval")


def background(client, body):
    result = {}
    t = threading.Thread(target=lambda: result.update(client.post("/gate", json=body).json()), daemon=True)
    t.start()
    return t, result


def test_the_repository_block_beats_a_personal_allow(client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_policy(repo, sites={"block": ["pastebin.com"], "allow": ["docs.python.org"]})
    gate.sites_path.write_text(json.dumps({"allow": ["pastebin.com"]}))
    assert client.post("/gate", json=fetch("https://pastebin.com/raw/x", repo)).json() == {"decision": "deny"}
    assert client.post("/gate", json=fetch("https://docs.python.org/3/", repo)).json() == {"decision": "allow"}
    hows = [d["how"] for d in board_app.recent_decisions]
    assert hows == ["site on the repository's block list", "site on the repository's allow list"]
    assert all("sha256" in d["policy"] for d in board_app.recent_decisions)
    # outside the repository the person's own list applies, and no policy is recorded
    assert client.post("/gate", json=fetch("https://pastebin.com/raw/y", tmp_path)).json() == {"decision": "allow"}
    assert board_app.recent_decisions[-1]["policy"] is None


def test_an_invalid_policy_means_a_card_with_a_warning(client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / policy_file.NAME).write_text('{"version": 1, "forbid": {"typo": true}}')
    gate.sites_path.write_text(json.dumps({"allow": ["example.com"]}))
    t, r = background(client, fetch("https://example.com/", repo))
    (card,) = pending(client)
    assert "is invalid" in card["warnings"][-1] and not card["job_allowable"]
    client.post(f"/gate/{card['id']}/deny", headers=OK)
    t.join(5)


def test_the_log_report_shows_the_policy(client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_policy(repo, sites={"block": ["pastebin.com"]})
    client.post("/gate", json=fetch("https://pastebin.com/x", repo))
    text = render_markdown(list(board_app.recent_decisions))
    assert "Repository policy" in text and policy_file.NAME in text and "sha256" in text


# --------------------------------------------------------------------------- #
# Guard mode's hook                                                            #
# --------------------------------------------------------------------------- #

def run(event, url, *args, state):
    env = {"TEAMBUILD_BOARD_GATE_URL": url, "TEAMBUILD_BOARD_URL": f"http://127.0.0.1:{free_port()}/hook",
           "PATH": "/usr/bin:/bin", "XDG_RUNTIME_DIR": str(state)}
    p = subprocess.run([sys.executable, str(HOOK), *args], input=json.dumps(event), capture_output=True, text=True, env=env)
    return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


def ev(tool, cwd, **inp):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": inp, "session_id": "s1", "cwd": str(cwd)}


@pytest.fixture
def stub():
    b = Board(b'{"decision": "allow"}')
    yield b
    b.close()


def test_the_policy_marks_sensitive_files_for_guard_mode(stub, tmp_path):
    write_policy(tmp_path, sensitive=["customers/*"])
    run(ev("Read", tmp_path, file_path=str(tmp_path / "customers/list.csv")), stub.url, "--all", state=tmp_path / "st")
    out = run(ev("WebSearch", tmp_path, query="q"), stub.url, "--all", state=tmp_path / "st")
    assert out["permissionDecision"] == "deny" and "customers/*" in out["permissionDecisionReason"]


def test_the_policy_can_ask_and_block_wins_when_both_are_set(stub, tmp_path):
    write_policy(tmp_path, sensitive=["*.pem"], after_sensitive="ask")
    st = tmp_path / "st"
    run(ev("Read", tmp_path, file_path="certs/a.pem"), stub.url, "--all", state=st)
    run(ev("WebSearch", tmp_path, query="q"), stub.url, "--all", state=st)
    assert "*.pem" in stub.seen[-1]["warnings"][0]                      # the repository said ask: a card
    out = run(ev("WebSearch", tmp_path, query="q2"), stub.url, "--all", "--sensitive", "x/*", "--flow", "block", state=st)
    assert out["permissionDecision"] == "deny"                            # the person said block: block wins


def test_an_invalid_policy_adds_no_patterns_in_the_hook(stub, tmp_path):
    (tmp_path / policy_file.NAME).write_text('{"version": 1, "sensitive": "customers/*"}')
    run(ev("Read", tmp_path, file_path="customers/a.csv"), stub.url, "--all", state=tmp_path / "st")
    assert run(ev("WebSearch", tmp_path, query="q"), stub.url, "--all", state=tmp_path / "st")["permissionDecision"] == "allow"


# --------------------------------------------------------------------------- #
# The audit                                                                    #
# --------------------------------------------------------------------------- #

def levels(notes, word):
    return [lvl for lvl, t in notes if word in t]


def test_an_invalid_policy_fails_the_audit(tmp_path):
    (tmp_path / policy_file.NAME).write_text('{"version": 1, "forbid": {"remote_mpc": true}}')
    assert levels(setup_notes(tmp_path), "is invalid") == ["FAIL"] and audit(tmp_path).failed


def test_forbid_turns_findings_into_failures(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "r": {"type": "http", "url": "https://mcp.vendor.example/v1"},
        "u": {"command": "npx", "args": ["-y", "@vendor/srv"]}}}))
    assert levels(setup_notes(tmp_path), "is remote") == ["WARN"]
    write_policy(tmp_path, forbid={"remote_mcp": True})
    notes = setup_notes(tmp_path)
    assert levels(notes, "is remote") == ["FAIL"] and levels(notes, "without a pinned version") == ["WARN"]
    assert "forbidden by .agent-troops.json: remote_mcp" in " ".join(t for _, t in notes)


def test_forbid_unknown_hooks_covers_other_tools_too(tmp_path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/hooks.json").write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "./fmt.sh"}]}}))
    write_policy(tmp_path, forbid={"unknown_hooks": True})
    assert levels(setup_notes(tmp_path), "stop hook") == ["FAIL"]


def test_require_guard_needs_the_shared_install(tmp_path):
    write_policy(tmp_path, require_guard=True)
    assert levels(setup_notes(tmp_path), "requires guard mode") == ["FAIL"]
    guard.install(tmp_path, "local", home=tmp_path / "home")             # personal: a CI checkout cannot see it
    assert levels(setup_notes(tmp_path), "requires guard mode") == ["FAIL"]
    guard.remove(tmp_path, home=tmp_path / "home")
    guard.install(tmp_path, "shared", home=tmp_path / "home")
    notes = setup_notes(tmp_path)
    assert levels(notes, "requires guard mode") == [] and not audit(tmp_path).failed
    assert "guard mode required" in " ".join(t for _, t in notes)


def test_sensitive_files_without_guard_mode_are_a_warning(tmp_path):
    write_policy(tmp_path, sensitive=["config/*"])
    assert levels(setup_notes(tmp_path), "nothing in this repository enforces them") == ["WARN"]


# --------------------------------------------------------------------------- #
# Command line and guard status                                                #
# --------------------------------------------------------------------------- #

def cli(*args):
    return subprocess.run([sys.executable, "-m", "teambuild.cli", "policy", *args], capture_output=True, text=True, cwd=ROOT)


def test_policy_init_and_check(tmp_path):
    assert cli("init", str(tmp_path)).returncode == 0
    assert cli("init", str(tmp_path)).returncode == 1                  # never overwrites
    assert cli("check", str(tmp_path)).returncode == 0
    (tmp_path / policy_file.NAME).write_text('{"version": 1, "sites": {"allow": ["bad name"]}}')
    r = cli("check", str(tmp_path))
    assert r.returncode == 1 and "INVALID" in r.stdout


def test_guard_status_shows_the_policy(tmp_path):
    write_policy(tmp_path, sites={"block": ["pastebin.com"]})
    text = guard.format_status(guard.status(tmp_path, home=tmp_path / "home", port=free_port()))
    assert "repository policy" in text and "0 allowed, 1 blocked" in text
