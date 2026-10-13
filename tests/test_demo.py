"""`team-build demo`: the scripted attacks must be stopped by the real guards, with and without a board."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from teambuild.demo import Demo, make_team

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubBoard:
    """Answers /gate: Allow for an ordinary page, Deny for anything flagged, the way a person would."""

    def __init__(self):
        self.asked = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.asked.append(body)
                decision = "deny" if "collect." in body.get("detail", "") else "allow"
                data = json.dumps({"decision": decision}).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/gate"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def run_demo(tmp_path, monkeypatch, gate_url):
    monkeypatch.setenv("TEAMBUILD_BOARD_GATE_URL", gate_url)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))          # flow_guard's state, not the real one
    folder = tmp_path / "demo"
    folder.mkdir()
    make_team(folder)
    lines = []
    return dict(Demo(folder, say=lines.append, pause=0).run()), folder, lines


def test_every_attack_is_stopped_with_a_board(tmp_path, monkeypatch):
    board = StubBoard()
    results, folder, _ = run_demo(tmp_path, monkeypatch, board.url)
    assert results["poisoned repo"].startswith("audit FAIL")
    assert results["hidden instructions"].startswith("page fetch allowed on the board")
    assert results["0DIN shell trick"].startswith("REFUSED by bash_guard: shell metacharacters")
    assert results["data smuggled in a URL"] == "DENIED on the board"
    assert results["relay back to the web"] == "analyst: allowed; researcher REFUSED by flow_guard"
    assert [a["tool_name"] for a in board.asked] == ["WebFetch", "WebFetch"]   # no card for the refused shell trick
    assert not (folder / ".claude" / "settings.local.json").exists()          # the poisoned files were removed
    assert not (folder / ".claude" / "commands" / "setup.md").exists()
    board.server.shutdown()


def test_without_a_board_the_guards_still_hold(tmp_path, monkeypatch):
    results, _, _ = run_demo(tmp_path, monkeypatch, f"http://127.0.0.1:{free_port()}/gate")
    assert results["0DIN shell trick"].startswith("REFUSED by bash_guard")
    assert results["relay back to the web"].endswith("REFUSED by flow_guard")
    assert "Claude Code would ask you" in results["data smuggled in a URL"]


def test_the_demo_payloads_are_inert():
    from teambuild import demo
    text = demo.INJECTED_PAGE + demo.POISON_HOOK + demo.POISON_COMMAND
    hosts = {h for h in __import__("re").findall(r"https?://([\w.-]+)", text)} | {"setup.demo.invalid"}
    assert all(h.endswith(".invalid") for h in hosts), hosts                  # never resolves
    assert demo.FAKE_KEY == "AKIAIOSFODNN7EXAMPLE"                            # AWS's documented example key


def test_cli_refuses_a_folder_that_is_not_empty(tmp_path):
    (tmp_path / "x").write_text("keep me")
    p = subprocess.run([sys.executable, "-m", "teambuild.cli", "demo", "--target", str(tmp_path)],
                       capture_output=True, text=True, cwd=ROOT)
    assert p.returncode != 0 and "not empty" in p.stderr and (tmp_path / "x").read_text() == "keep me"
