"""team-build --audit: offline checks on hand-written agent directories."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from teambuild.audit import audit, format_text, parse_frontmatter, render_report
from teambuild.cc_scaffold import scaffold

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def write_agent(target: Path, name: str, tools=None, body="Do the job.", extra="", description="Does a job."):
    d = target / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    tools_line = f"tools: {tools}\n" if tools is not None else ""
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{tools_line}{extra}---\n{body}\n", encoding="utf-8"
    )


def role(result, rid):
    return next(r for r in result.roles if r.id == rid)


def test_good_team_passes(tmp_path):
    write_agent(tmp_path, "web", "WebFetch, WebSearch")
    write_agent(tmp_path, "repo", "Read, Grep, Glob, Write, Edit")
    result = audit(tmp_path)
    assert result.overall in ("PASS", "WARN") and not result.failed
    assert role(result, "web").legs == {"untrusted_content", "exfiltration"}
    assert role(result, "repo").legs == {"private_data"}


def test_read_plus_webfetch_is_a_trifecta_fail(tmp_path):
    write_agent(tmp_path, "everything", "Read, WebFetch")
    result = audit(tmp_path)
    r = role(result, "everything")
    assert r.verdict == "FAIL" and result.failed
    assert any("all three trifecta legs" in n for n in r.notes)


def test_missing_tools_line_inherits_everything_and_fails(tmp_path):
    write_agent(tmp_path, "open", tools=None)
    r = role(audit(tmp_path), "open")
    assert r.tools is None and r.verdict == "FAIL"
    assert any("inherits every tool" in n for n in r.notes)


def test_disallowed_tools_narrow_an_inheriting_agent(tmp_path):
    write_agent(tmp_path, "narrowed", tools=None, extra="disallowedTools: Bash, WebFetch, WebSearch\n")
    r = role(audit(tmp_path), "narrowed")
    assert "untrusted_content" not in r.legs and r.verdict != "FAIL"


def test_bash_is_all_three_legs_when_unguarded(tmp_path):
    write_agent(tmp_path, "shell", "Bash")
    assert role(audit(tmp_path), "shell").verdict == "FAIL"


def test_bash_specifier_does_not_narrow_bash(tmp_path):
    write_agent(tmp_path, "shell", "Bash(git status)")
    r = role(audit(tmp_path), "shell")
    assert r.verdict == "FAIL" and any("does not narrow" in n for n in r.notes)


def test_gated_by_own_text_downgrades_trifecta_to_warn(tmp_path):
    write_agent(tmp_path, "deployer", "Read, Bash",
                body="You require explicit human approval before every deploy.")
    r = role(audit(tmp_path), "deployer")
    assert r.gated and r.verdict == "WARN"
    assert any("cannot verify" in n for n in r.notes)


def test_bypass_permissions_voids_a_gate(tmp_path):
    write_agent(tmp_path, "deployer", "Read, Bash", extra="permissionMode: bypassPermissions\n",
                body="You require explicit human approval before every deploy.")
    r = role(audit(tmp_path), "deployer")
    assert r.verdict == "FAIL" and any("bypassPermissions" in n for n in r.notes)


def test_unclassified_mcp_tool_warns_and_is_not_counted(tmp_path):
    write_agent(tmp_path, "mailer", "Read, mcp__crm__lookup")
    r = role(audit(tmp_path), "mailer")
    assert r.verdict == "WARN" and any("mcp__crm__lookup" in n for n in r.notes)
    assert r.legs == {"private_data"}


def test_yaml_list_and_flow_list_tools_parse(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "a.md").write_text("---\nname: a\ndescription: x\ntools:\n  - Read\n  - Grep\n---\nbody\n")
    (d / "b.md").write_text("---\nname: b\ndescription: x\ntools: [WebFetch, WebSearch]\n---\nbody\n")
    result = audit(tmp_path)
    assert role(result, "a").tools == ["Read", "Grep"]
    assert role(result, "b").tools == ["WebFetch", "WebSearch"]


def test_agent_allowlist_is_noted_not_failed(tmp_path):
    write_agent(tmp_path, "boss", "Agent(a, b)")
    r = role(audit(tmp_path), "boss")
    assert r.verdict == "PASS" and any("claude --agent" in n for n in r.notes)


def test_file_without_frontmatter_is_skipped_not_crashed(tmp_path):
    d = tmp_path / ".claude" / "agents"
    d.mkdir(parents=True)
    (d / "notes.md").write_text("just some notes, no frontmatter\n")
    write_agent(tmp_path, "ok", "Read")
    result = audit(tmp_path)
    assert len(result.roles) == 1 and len(result.skipped) == 1


def test_no_agents_directory(tmp_path):
    result = audit(tmp_path)
    assert result.roles == [] and not result.failed
    assert "nothing to audit" in format_text(result)


# ---- scaffolded teams: the generator's own output must audit clean ------------ #

@pytest.mark.parametrize("fixture", ["roster_code_review.json", "roster_research.json"])
def test_generated_teams_do_not_fail(tmp_path, fixture):
    scaffold(json.loads((FIXTURES / fixture).read_text()), tmp_path)
    result = audit(tmp_path)
    assert not result.failed, format_text(result)


def test_generated_poster_is_seen_as_guarded_and_gated(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_code_review.json").read_text()), tmp_path)
    r = role(audit(tmp_path), "cr-poster")
    assert r.guarded and r.gated and r.legs == {"private_data", "exfiltration"}


def test_deleting_the_guard_script_fails_the_audit(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_code_review.json").read_text()), tmp_path)
    (tmp_path / ".claude" / "hooks" / "bash_guard.py").unlink()
    r = role(audit(tmp_path), "cr-poster")
    assert r.verdict == "FAIL" and any("fails open" in n for n in r.notes)
    assert r.legs >= {"private_data", "untrusted_content", "exfiltration"}


def test_removing_the_guard_entry_makes_the_poster_unguarded(tmp_path):
    scaffold(json.loads((FIXTURES / "roster_code_review.json").read_text()), tmp_path)
    p = tmp_path / ".claude" / "settings.json"
    s = json.loads(p.read_text())
    s["hooks"]["PreToolUse"] = [e for e in s["hooks"]["PreToolUse"] if "bash_guard" not in json.dumps(e)]
    p.write_text(json.dumps(s))
    r = role(audit(tmp_path), "cr-poster")
    assert not r.guarded and r.legs >= {"untrusted_content"}


def test_broad_allow_and_missing_secret_rules_are_repo_warnings(tmp_path):
    write_agent(tmp_path, "a", "Read")
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Read", "Bash(*)"]}})
    )
    notes = " ".join(t for _, t in audit(tmp_path).repo_notes)
    assert "pre-approves" in notes and "no deny rule" in notes


# ---- output + CLI -------------------------------------------------------------- #

def test_report_contains_the_evidence_an_auditor_needs(tmp_path):
    write_agent(tmp_path, "everything", "Read, WebFetch")
    text = render_report(audit(tmp_path), claude_version="2.1.233 (Claude Code)")
    for needle in ("# Agent permission audit", "2.1.233", "everything", "FAIL", "Limits of this audit", "private_data"):
        assert needle in text


def test_parse_frontmatter_returns_none_without_block():
    assert parse_frontmatter("# just markdown") is None


def cli(*args):
    return subprocess.run([sys.executable, "-m", "teambuild.cli", "--audit", *args],
                          capture_output=True, text=True, cwd=ROOT)


def test_cli_exit_codes_and_report_file(tmp_path):
    good, bad = tmp_path / "good", tmp_path / "bad"
    write_agent(good, "web", "WebFetch")
    write_agent(bad, "everything", "Read, WebFetch")
    assert cli(str(good)).returncode == 0
    report = tmp_path / "report.md"
    p = cli(str(bad), "--report", str(report))
    assert p.returncode == 1 and "[FAIL] everything" in p.stdout
    assert "Overall result:** **FAIL**" in report.read_text()


def test_cli_rejects_a_missing_directory(tmp_path):
    p = cli(str(tmp_path / "nope"))
    assert p.returncode != 0 and "not a directory" in (p.stderr + p.stdout)


def test_audit_reports_read_fence_state(tmp_path):
    roster = json.loads((FIXTURES / "roster_research.json").read_text())
    scaffold(roster, tmp_path)
    assert "read fence off" in " ".join(t for _, t in audit(tmp_path).repo_notes)
    other = tmp_path / "fenced"
    scaffold(roster, other, fence_reads=True)
    notes = " ".join(t for _, t in audit(other).repo_notes)
    assert "read fence on for" in notes and "rs-researcher" in notes
