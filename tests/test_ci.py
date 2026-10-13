"""CI use of the audit: --fail-on, the GitHub Action's audit script (action.yml) and the pre-commit hook.
Offline: the action's script is run with bash against fixture repos, as a runner would run it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teambuild.audit import audit
from teambuild.cli import audit_exit_code

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / "tests" / "fixtures" / "ci"


def test_fail_on_levels(tmp_path):
    poisoned, clean = audit(CI / "poisoned"), audit(CI / "clean")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"x": {"command": "npx", "args": ["-y", "pkg"]}}}')
    warned = audit(tmp_path)
    assert (poisoned.overall, warned.overall, clean.overall) == ("FAIL", "WARN", "NO AGENTS")
    assert [audit_exit_code(r, "fail") for r in (poisoned, warned, clean)] == [1, 0, 0]
    assert [audit_exit_code(r, "warn") for r in (poisoned, warned, clean)] == [1, 1, 0]
    assert [audit_exit_code(r, "never") for r in (poisoned, warned, clean)] == [0, 0, 0]


def cli(*args):
    return subprocess.run([sys.executable, "-m", "teambuild.cli", "--audit", *args], capture_output=True, text=True, cwd=ROOT)


def test_fail_on_on_the_command_line():
    assert cli(str(CI / "poisoned")).returncode == 1
    assert cli(str(CI / "poisoned"), "--fail-on", "never").returncode == 0
    assert cli(str(CI / "clean"), "--fail-on", "warn").returncode == 0
    assert cli(str(CI / "clean"), "--fail-on", "sometimes").returncode == 1


def action_script() -> str:
    """The `run:` block of the action's Audit step."""
    text = (ROOT / "action.yml").read_text()
    block = text.split("id: audit", 1)[1].split("run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith("        "):
            break
        lines.append(line[8:])
    return "\n".join(lines)


@pytest.mark.parametrize("fixture, fail_on, status, result", [
    ("poisoned", "fail", 1, "FAIL"),
    ("poisoned", "never", 0, "FAIL"),
    ("clean", "fail", 0, "NO AGENTS"),
])
def test_the_action_script(tmp_path, fixture, fail_on, status, result):
    if not shutil.which("bash"):
        pytest.skip("needs bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "team-build").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m teambuild.cli "$@"\n')
    (bin_dir / "team-build").chmod(0o755)
    out, summary = tmp_path / "out", tmp_path / "summary"
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
           "GITHUB_STEP_SUMMARY": str(summary), "TARGET": str(CI / fixture), "FAIL_ON": fail_on,
           "PYTHONPATH": str(ROOT), "HOME": str(tmp_path)}
    p = subprocess.run(["bash", "-e", "-c", action_script()], env=env, capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == status, p.stderr
    assert out.read_text().strip() == f"result={result}"
    if result != "NO AGENTS":
        assert "settings or permissions" in summary.read_text()


def test_the_action_pins_what_it_uses():
    text = (ROOT / "action.yml").read_text()
    for uses in re.findall(r"uses:\s*(\S+)", text):
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", uses), f"{uses} is not pinned to a commit"
    assert "${{ inputs." not in action_script()          # inputs arrive as environment variables only


def test_the_pre_commit_hook():
    text = (ROOT / ".pre-commit-hooks.yaml").read_text()
    assert "id: agent-troops-audit" in text and "entry: team-build --audit" in text
    assert "pass_filenames: false" in text and "language: python" in text
