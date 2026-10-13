"""Run a team the ordinary way: the user's own interactive `claude --agent <orchestrator>` in a terminal.

This is the default way to run a team from the web page, and the only one for subscription logins.
The person works in Claude Code as usual; no headless flag is used. The job typed on the page can be
handed over as Claude Code's own first message (`claude --agent <orchestrator> "<job>"`, an ordinary
interactive session that starts with that message), so nothing has to be typed into the terminal.
Approvals and results then show on the board, through the hooks the team already has.

The job is one argument in an argument list, never shell text, and may not start with "-", so it can
never be read as a Claude Code option. The command shown for copying quotes it for a POSIX shell.

Linux: the first terminal found of gnome-terminal, konsole, xfce4-terminal, x-terminal-emulator is
started with an argument list (no shell), in the team folder, with the environment inherited as it is,
in its own session so closing the web page does not close it. macOS and Windows: not opened
automatically yet; the page shows the command to copy.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from teambuild.architect import ROLE_ID_RE

# name -> argv builder(terminal, folder, command)
TERMINALS: list[tuple[str, Callable[[str, Path, list[str]], list[str]]]] = [
    ("gnome-terminal", lambda t, d, c: [t, f"--working-directory={d}", "--", *c]),
    ("konsole", lambda t, d, c: [t, "--workdir", str(d), "-e", *c]),
    ("xfce4-terminal", lambda t, d, c: [t, f"--working-directory={d}", "-x", *c]),
    ("x-terminal-emulator", lambda t, d, c: [t, "-e", *c]),
]


MAX_JOB_CHARS = 20_000


class TerminalError(ValueError):
    pass


def clean_job(job: object) -> str | None:
    """The job as Claude Code's first message, or None for "start empty"."""
    if job is None:
        return None
    if not isinstance(job, str):
        raise TerminalError("the job must be text")
    job = job.strip()
    if not job:
        return None
    if len(job) > MAX_JOB_CHARS:
        raise TerminalError(f"the job is too long (at most {MAX_JOB_CHARS:,} characters)")
    if "\x00" in job:
        raise TerminalError("the job contains a character that cannot be passed on")
    if job.startswith("-"):
        raise TerminalError("start the job with a word, not a dash")
    return job


def team_command(orchestrator: str, claude: str = "claude", job: str | None = None) -> list[str]:
    if not ROLE_ID_RE.fullmatch(orchestrator) or not orchestrator.endswith("-orchestrator"):
        raise TerminalError("that is not an orchestrator role")
    job = clean_job(job)
    return [claude, "--agent", orchestrator] + ([job] if job else [])


def copyable(folder: Path, orchestrator: str, job: str | None = None) -> str:
    """What to paste into a terminal yourself (a POSIX shell: Linux, macOS, WSL, Git Bash)."""
    job = clean_job(job)
    tail = f" {shlex.quote(job)}" if job else ""
    return f"cd {shlex.quote(str(folder))} && claude --agent {orchestrator}{tail}"


def find_terminal(which: Callable[[str], str | None] = shutil.which, platform: str = sys.platform) -> tuple[str, str] | None:
    if not platform.startswith("linux"):
        return None
    for name, _ in TERMINALS:
        path = which(name)
        if path:
            return name, path
    return None


def open_team(folder: Path, orchestrator: str, claude: str, job: str | None = None,
              which: Callable[[str], str | None] = shutil.which, platform: str = sys.platform,
              popen=subprocess.Popen) -> dict:
    """Open a terminal running the team, starting on `job` if one is given. Returns what happened."""
    cmd = team_command(orchestrator, claude, job)
    shown = copyable(folder, orchestrator, job)
    if not (folder / ".claude" / "agents" / f"{orchestrator}.md").is_file():
        raise TerminalError(f"{orchestrator} was not found in that folder")
    found = find_terminal(which, platform)
    if not found:
        return {"opened": False, "terminal": None, "command": shown}
    name, path = found
    argv = dict(TERMINALS)[name](path, folder, cmd)
    try:
        # No env= : the terminal, and the `claude` in it, inherit this process's environment unchanged.
        proc = popen(argv, cwd=str(folder), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return {"opened": False, "terminal": name, "command": shown}
    # Terminal launchers often hand the window to a server process and exit at once; reap them so they
    # don't linger as zombies for as long as this server runs.
    if hasattr(proc, "wait"):
        threading.Thread(target=proc.wait, daemon=True).start()
    return {"opened": True, "terminal": name, "command": shown}
