"""`team-build ui` startup: local-only binding, fresh token per launch, sensible port handling."""

from __future__ import annotations

import inspect
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from teambuild.ui import server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def captured(monkeypatch):
    calls = {"run": [], "open": []}

    def fake_run(self, *a, **kw):
        cfg = self.config
        calls["run"].append((cfg.app, {"host": cfg.host, "port": cfg.port,
                                        "timeout_graceful_shutdown": cfg.timeout_graceful_shutdown}))

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: calls["open"].append(url))
    monkeypatch.setattr(server, "start_board", lambda: None)
    return calls


def test_serve_binds_loopback_only_and_offers_no_way_to_change_it(captured):
    assert "host" not in inspect.signature(server.serve).parameters
    server.serve(port=18124, open_browser=False)
    _, kw = captured["run"][0]
    assert kw["host"] == "127.0.0.1" and kw["port"] >= 18124
    assert server.HOST == "127.0.0.1"
    assert kw["timeout_graceful_shutdown"] == server.GRACEFUL_SECONDS


def test_each_launch_gets_a_new_token_and_the_page_url_has_none(captured, capsys):
    server.serve(port=18130, open_browser=True)
    server.serve(port=18140, open_browser=True)
    (app1, _), (app2, _) = captured["run"]
    assert app1.state.token != app2.state.token and len(app1.state.token) >= 32
    for url in captured["open"]:
        assert url.startswith("http://127.0.0.1:") and app1.state.token not in url and app2.state.token not in url
    assert "this computer only" in capsys.readouterr().out


def test_allowed_hosts_follow_the_port_actually_used(captured):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        server.serve(port=taken, open_browser=False)
    app, kw = captured["run"][0]
    assert kw["port"] != taken
    from fastapi.testclient import TestClient
    with TestClient(app, base_url=f"http://127.0.0.1:{kw['port']}") as c:
        assert c.get("/api/blueprints").status_code == 200
    with TestClient(app, base_url=f"http://127.0.0.1:{taken}") as c:           # wrong port in Host
        assert c.get("/api/blueprints").status_code == 403


def test_no_browser_when_told_not_to(captured):
    server.serve(port=18150, open_browser=False)
    assert captured["open"] == []


def test_find_free_port_skips_busy_ones():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        assert server.find_free_port(taken) != taken


def test_cli_ui_rejects_a_bad_port():
    p = subprocess.run([sys.executable, "-m", "teambuild.cli", "ui", "--port", "abc"],
                       capture_output=True, text=True, timeout=30, cwd=ROOT)
    assert p.returncode != 0 and "usage" in (p.stderr + p.stdout)


def test_root_option_narrows_everything_to_that_folder(captured, tmp_path):
    root = tmp_path / "only-here"
    (root / "proj").mkdir(parents=True)
    server.serve(port=18160, open_browser=False, root=root)
    app, kw = captured["run"][0]
    from fastapi.testclient import TestClient
    headers = {"x-team-build-token": app.state.token}
    with TestClient(app, base_url=f"http://127.0.0.1:{kw['port']}") as c:
        assert c.post("/api/audit", json={"target": str(root / "proj")}, headers=headers).status_code == 200
        r = c.post("/api/audit", json={"target": str(tmp_path)}, headers=headers)
        assert r.status_code == 400 and "outside the folder you started with" in r.json()["detail"]
        assert c.get("/api/status").json()["home"] == str(root.resolve())


def test_cli_ui_root_must_be_an_existing_folder(tmp_path):
    p = subprocess.run([sys.executable, "-m", "teambuild.cli", "ui", "--root", str(tmp_path / "nope")],
                       capture_output=True, text=True, timeout=30, cwd=ROOT)
    assert p.returncode != 0 and "--root must be an existing folder" in (p.stderr + p.stdout)


def test_the_board_started_beside_the_ui_is_stopped_on_shutdown(captured, monkeypatch):
    """uvicorn re-raises SIGINT/SIGTERM after the app's shutdown hook, which skips atexit and
    finally blocks, so the board must be stopped from the lifespan hook."""
    class FakeBoard:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("terminate was enough")

    board = FakeBoard()
    monkeypatch.setattr(server, "start_board", lambda: board)
    server.serve(port=18170, open_browser=False)
    app, kw = captured["run"][0]
    from fastapi.testclient import TestClient
    with TestClient(app, base_url=f"http://127.0.0.1:{kw['port']}") as c:
        assert c.get("/api/status").status_code == 200
        assert board.terminated is False
    assert board.terminated is True
