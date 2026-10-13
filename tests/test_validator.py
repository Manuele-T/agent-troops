"""Offline invariant tests. No network access required."""

import copy
import json
from pathlib import Path

import pytest

from teambuild.architect import TRIFECTA, Architect, check_no_lethal_trifecta, role_legs

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINTS_PATH = ROOT / "teambuild" / "blueprints.json"
FIXTURES = ROOT / "tests" / "fixtures"

FIXTURE_NAMES = [
    "roster_code_review.json",
    "roster_research.json",
]


@pytest.fixture(scope="module")
def architect() -> Architect:
    return Architect(BLUEPRINTS_PATH)


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r", encoding="utf-8") as f:
        return json.load(f)


def find_role(roster: dict, primitive: str) -> dict:
    return next(r for r in roster["roles"] if r["primitive"] == primitive)


# ---- canned fixtures & fallback rosters always validate --------------------- #

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_canned_fixture_rosters_validate(architect, fixture_name):
    roster = load_fixture(fixture_name)
    assert architect.validate(roster) == []


@pytest.mark.parametrize("blueprint_id", ["code_review", "research", "dependency_check"])
def test_fallback_roster_always_validates(architect, blueprint_id):
    roster = architect.fallback_roster(blueprint_id, prefix="tb")
    assert architect.validate(roster) == []


# ---- invariant: exactly one orchestrator ------------------------------------ #

def test_rejects_zero_orchestrators(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    find_role(roster, "orchestrator")["primitive"] = "builder"
    errors = architect.validate(roster)
    assert any("orchestrator" in e for e in errors)


def test_rejects_duplicate_orchestrators(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    dup = copy.deepcopy(find_role(roster, "orchestrator"))
    dup["id"] += "-2"
    roster["roles"].append(dup)
    errors = architect.validate(roster)
    assert any("orchestrator" in e for e in errors)


# ---- invariant: reviewer tools subset of {Read, Grep, Glob} ----------------- #

def test_rejects_reviewer_tools_beyond_read_only(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    find_role(roster, "reviewer")["tools"] = ["Read", "Grep", "Glob", "Write"]
    errors = architect.validate(roster)
    assert any("reviewer" in e.lower() for e in errors)


# ---- invariant: gated roles carry an approval note --------------------------- #

def test_rejects_gated_role_missing_approval_note(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    shipper = find_role(roster, "github_publisher")
    shipper["identity"] = "Posts the review to the pull request."
    shipper["description"] = "Posts the cr review."
    errors = architect.validate(roster)
    assert any("approval" in e.lower() for e in errors)


# ---- invariant: tools within primitive maximum (+ catalog membership) ------- #

def test_rejects_tools_exceeding_primitive_max(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    find_role(roster, "reviewer")["tools"] = ["Read", "Grep", "Glob", "Bash"]
    errors = architect.validate(roster)
    assert any("exceeds primitive" in e for e in errors)


def test_rejects_unknown_tool_name(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    find_role(roster, "builder")["tools"].append("Frobnicate")
    errors = architect.validate(roster)
    assert any("Frobnicate" in e for e in errors)


def test_rejects_unknown_primitive(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    find_role(roster, "builder")["primitive"] = "wizard"
    errors = architect.validate(roster)
    assert any("unknown primitive" in e for e in errors)


# ---- invariant: no non-gated role holds the lethal trifecta ------------------ #

@pytest.mark.parametrize("tools, is_trifecta", [
    (["Read", "WebFetch"], True),
    (["Grep", "WebSearch"], True),
    (["Bash"], True),
    (["Glob", "WebFetch"], True),
    (["WebFetch", "WebSearch"], False),
    (["Read", "Grep", "Glob", "Write", "Edit"], False),
    (["Task"], False),
])
def test_trifecta_leg_combinations(architect, tools, is_trifecta):
    roles = [{"id": "x", "tools": tools, "gated": False}]
    assert bool(check_no_lethal_trifecta(roles, architect.tool_legs)) is is_trifecta


RAW_SHELL = {"allowed_tools": ["Read", "Bash"], "gated": False, "boundary_text": []}   # test-only: Bash with no allowlist


def with_raw_shell(architect):
    """A copy of the architect that also knows an ungated primitive with an unguarded shell, as a
    planner or a hand-edited blueprints.json might add. The validator must still refuse it."""
    arch = copy.copy(architect)
    arch.primitives = {**architect.primitives, "raw_shell": RAW_SHELL}
    return arch


def test_rejects_non_gated_trifecta_role(architect):
    arch = with_raw_shell(architect)
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    shipper = find_role(roster, "github_publisher")
    shipper["primitive"], shipper["gated"] = "raw_shell", False
    errors = arch.validate(roster)
    assert any("trifecta" in e for e in errors)
    assert not any("unknown primitive" in e for e in errors)      # refused for the trifecta itself


def test_gated_role_may_hold_trifecta(architect):
    roster = load_fixture("roster_code_review.json")
    shipper = find_role(roster, "github_publisher")
    assert shipper["gated"] and role_legs(shipper["tools"], architect.tool_legs) >= set(TRIFECTA)
    assert architect.validate(roster) == []


def test_no_non_gated_primitive_maximum_holds_trifecta(architect):
    for name, prim in architect.primitives.items():
        if prim["gated"]:
            continue
        assert not role_legs(prim["allowed_tools"], architect.tool_legs) >= set(TRIFECTA), name


def test_every_catalog_tool_has_legs(architect):
    assert architect.tool_catalog == set(architect.tool_legs)


# ---- blueprint questions only offer choices the team can honour ------------- #

@pytest.mark.parametrize("blueprint_id", ["code_review", "research", "dependency_check"])
def test_blueprint_questions_offer_real_choices(architect, blueprint_id):
    for q in architect.blueprints[blueprint_id]["questions"]:
        assert len(q["options"]) >= 2, q["id"]
        for opt in q["options"]:
            # gated roles always need approval, and the CLI can't take free text
            assert not opt.lower().startswith(("auto", "custom")), (q["id"], opt)


# ---- invariant: deny_rules always include the secret-file set --------------- #

def test_rejects_empty_deny_rules(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    roster["deny_rules"] = []
    errors = architect.validate(roster)
    assert any("deny_rules" in e for e in errors)


def test_rejects_partial_deny_rules(architect):
    roster = copy.deepcopy(load_fixture("roster_code_review.json"))
    roster["deny_rules"] = ["Read(./.env)"]
    errors = architect.validate(roster)
    assert any("deny_rules" in e for e in errors)
