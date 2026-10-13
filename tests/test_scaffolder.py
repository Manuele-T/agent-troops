"""Scaffold fixtures into tmp_path."""

import json
from pathlib import Path

import pytest

from teambuild.cc_scaffold import destroy, scaffold
from teambuild.templates.agent_md import UNTRUSTED_TEXT_RULE

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

FIXTURE_NAMES = [
    "roster_code_review.json",
    "roster_research.json",
]


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML-frontmatter parser: good enough for our flat, single-line values."""
    assert text.startswith("---\n"), "file does not start with a frontmatter block"
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5:]
    data = {}
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data, body


def snapshot(target_dir: Path) -> dict:
    """Capture every file's relative path + contents under target_dir, for before/after diffs."""
    if not target_dir.exists():
        return {}
    return {
        str(p.relative_to(target_dir)): p.read_text(encoding="utf-8")
        for p in target_dir.rglob("*")
        if p.is_file()
    }


# ---- scaffold writes the expected files for every fixture ------------------- #

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_scaffold_writes_expected_files(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    target = tmp_path / roster["blueprint_id"]
    scaffold(roster, target)

    for role in roster["roles"]:
        agent_path = target / ".claude" / "agents" / f"{role['id']}.md"
        assert agent_path.exists(), f"missing {agent_path}"

    assert (target / ".claude" / "hooks" / "emit.py").exists()
    assert (target / ".claude" / "settings.json").exists()
    assert (target / ".claude" / ".teambuild-manifest.json").exists()


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_scaffold_writes_no_skills_dir(tmp_path, fixture_name):
    """No .claude/skills/ — a Skill shares no isolation with its same-named Agent,
    so a per-role Skill silently bypasses the Agent's tools: scoping (Phase 4)."""
    roster = load_fixture(fixture_name)
    target = tmp_path / roster["blueprint_id"]
    scaffold(roster, target)

    assert not (target / ".claude" / "skills").exists()


# ---- generated agent frontmatter roundtrips and matches the roster ---------- #

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_agent_frontmatter_roundtrips(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    target = tmp_path / roster["blueprint_id"]
    scaffold(roster, target)

    all_role_ids = [r["id"] for r in roster["roles"]]
    for role in roster["roles"]:
        text = (target / ".claude" / "agents" / f"{role['id']}.md").read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(text)
        assert frontmatter["name"] == role["id"]
        assert frontmatter["description"] == role["description"]
        assert frontmatter["model"] == role["model"]

        # "Task" renders as "Agent(<sibling ids>)" so a subagent can address its
        # named teammates by id — see agent_md.py's render_agent_md docstring.
        expected_tools = list(role["tools"])
        if "Task" in expected_tools:
            siblings = [rid for rid in all_role_ids if rid != role["id"]]
            expected_tools[expected_tools.index("Task")] = f"Agent({', '.join(siblings)})"
        assert frontmatter["tools"] == ", ".join(expected_tools)

        assert role["identity"] in body
        assert "# Pipeline" in body


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_every_agent_treats_outside_text_as_data(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    scaffold(roster, tmp_path)
    for role in roster["roles"]:
        text = (tmp_path / ".claude" / "agents" / f"{role['id']}.md").read_text(encoding="utf-8")
        assert UNTRUSTED_TEXT_RULE in text


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_handoff_text_matches_role(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    scaffold(roster, tmp_path)
    for role in roster["roles"]:
        text = (tmp_path / ".claude" / "agents" / f"{role['id']}.md").read_text(encoding="utf-8")
        if role["primitive"] == "orchestrator":
            assert "You receive work directly from the user." in text
            assert "verbatim" in text
        else:
            assert "You receive work from the orchestrator." in text


def test_guardrail_is_written_into_every_agent(tmp_path):
    roster = load_fixture("roster_research.json")
    roster["guardrail"] = "Cross-check every claim against a second source."
    scaffold(roster, tmp_path)
    for role in roster["roles"]:
        text = (tmp_path / ".claude" / "agents" / f"{role['id']}.md").read_text(encoding="utf-8")
        assert "- Team guardrail: Cross-check every claim against a second source." in text


# ---- settings.json merge preserves pre-existing user keys ------------------- #

def test_settings_merge_preserves_existing_user_keys(tmp_path):
    roster = load_fixture("roster_code_review.json")
    target = tmp_path / "repo"
    settings_path = target / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    pre_existing = {
        "env": {"MY_CUSTOM_VAR": "keep-me"},
        "permissions": {"deny": ["Read(./custom-secret.txt)"]},
    }
    settings_path.write_text(json.dumps(pre_existing, indent=2), encoding="utf-8")

    scaffold(roster, target)

    merged = json.loads(settings_path.read_text(encoding="utf-8"))
    assert merged["env"] == {"MY_CUSTOM_VAR": "keep-me"}
    assert "Read(./custom-secret.txt)" in merged["permissions"]["deny"]
    for rule in roster["deny_rules"]:
        assert rule in merged["permissions"]["deny"]
    for event in ("PreToolUse", "PostToolUse", "SubagentStop"):
        assert event in merged["hooks"]


def test_settings_merge_is_idempotent_no_duplicate_rules(tmp_path):
    roster = load_fixture("roster_code_review.json")
    target = tmp_path / "repo"

    scaffold(roster, target)
    scaffold(roster, target)  # scaffold the same team twice

    settings = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    assert len(deny) == len(set(deny))
    for event in ("PreToolUse", "PostToolUse", "SubagentStop"):
        commands = [h["command"] for entry in settings["hooks"][event] for h in entry["hooks"]]
        assert len(commands) == len(set(commands))


# ---- dry-run writes nothing --------------------------------------------------- #

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_dry_run_writes_nothing(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    target = tmp_path / roster["blueprint_id"]

    actions = scaffold(roster, target, dry_run=True)

    assert not target.exists()
    assert any("[dry-run]" in a for a in actions)


# ---- destroy returns the dir to its prior state ------------------------------ #

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_destroy_returns_dir_to_prior_state(tmp_path, fixture_name):
    roster = load_fixture(fixture_name)
    target = tmp_path / roster["blueprint_id"]
    target.mkdir(parents=True)

    before = snapshot(target)
    scaffold(roster, target)
    assert snapshot(target) != before  # sanity: scaffold actually wrote something

    destroy(roster["team_prefix"], target)

    after = snapshot(target)
    assert after == before


def test_destroy_of_one_team_preserves_a_second_team_sharing_the_repo(tmp_path):
    cr = load_fixture("roster_code_review.json")
    rs = load_fixture("roster_research.json")
    target = tmp_path / "shared-repo"

    scaffold(cr, target)
    scaffold(rs, target)

    destroy(cr["team_prefix"], target)

    # code_review's own agent files are gone
    for role in cr["roles"]:
        assert not (target / ".claude" / "agents" / f"{role['id']}.md").exists()

    # research's files, and the shared hook + manifest, survive
    for role in rs["roles"]:
        assert (target / ".claude" / "agents" / f"{role['id']}.md").exists()
    assert (target / ".claude" / "hooks" / "emit.py").exists()

    manifest = json.loads((target / ".claude" / ".teambuild-manifest.json").read_text(encoding="utf-8"))
    assert cr["team_prefix"] not in manifest["teams"]
    assert rs["team_prefix"] in manifest["teams"]

    settings = json.loads((target / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for rule in rs["deny_rules"]:
        assert rule in settings["permissions"]["deny"]
