"""`team-build ui`: start the local web interface.

Binds 127.0.0.1 only, on purpose: there is no option to listen on another address. Local only, no
hosted version, no telemetry. The page token is generated fresh each launch and lives only in the
page this server serves.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

from teambuild.ui.app import create_app
from teambuild.ui.security import new_token

HOST = "127.0.0.1"
BOARD_PORT = 8123
PORT_TRIES = 20
GRACEFUL_SECONDS = 3      # a held approval request must not keep Ctrl+C waiting for its ten minutes


def make_config(app, port: int):
    """The one place the listener is configured: loopback only, short graceful shutdown."""
    import uvicorn

    return uvicorn.Config(app, host=HOST, port=port, log_level="warning",
                          timeout_graceful_shutdown=GRACEFUL_SECONDS)


def find_free_port(start: int) -> int:
    for port in range(start, start + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)     # as uvicorn does: ignore TIME_WAIT
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"no free port between {start} and {start + PORT_TRIES - 1}; use --port")


def start_board() -> subprocess.Popen | None:
    """Start the board beside the UI when its port is free. Best effort: the UI works fine without it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((HOST, BOARD_PORT)) == 0:
            return None          # something (probably a board) is already listening
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "teambuild.board.app:app", "--host", HOST,
             "--port", str(BOARD_PORT), "--log-level", "warning"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def serve(port: int = 8124, open_browser: bool = True, root: Path | None = None) -> None:
    """`root` narrows the folders the page may touch (default: the home folder)."""
    import uvicorn

    port = find_free_port(port)
    url = f"http://{HOST}:{port}"
    board = start_board()

    def stop_board() -> None:
        if board is not None and board.poll() is None:
            board.terminate()
            try:
                board.wait(timeout=5)
            except subprocess.TimeoutExpired:
                board.kill()

    app = create_app(
        token=new_token(),
        allowed_hosts={f"{HOST}:{port}", f"localhost:{port}"},
        board_port=BOARD_PORT,
        home=root,
        home_label="your home folder" if root is None else "the folder you started with",
        on_shutdown=stop_board,
    )
    print(f"Team Builder is running at {url}  (this computer only; press Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.Server(make_config(app, port)).run()
