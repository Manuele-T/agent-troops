"""Architect: route(), assemble(), validate(), fallback_roster().

No agent, no profile: two stateless LLM calls (router, assembler) + validation.
A CLI or backend drives the four-turn flow; this module owns the two fuzzy LLM jobs and
the deterministic checks around them. validate() and its check_* functions are pure —
no LLM, no I/O — so they run fully offline and are unit-testable without a network.

Usage sketch:
    arch = Architect("blueprints.json", llm_call=my_llm_call)
    route = arch.route(brief)                       # -> RouteResult
    # caller asks route.blueprint's questions from blueprints.json, collects answers
    roster = arch.assemble(route.blueprint_id, brief, answers, guardrail, prefix)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# (system_prompt, user_message) -> raw text response from the LLM
LlmCall = Callable[[str, str], str]

REVIEWER_MAX_TOOLS = {"Read", "Grep", "Glob"}
REQUIRED_ROLE_FIELDS = {
    "id", "primitive", "display_name", "description", "model", "tools", "gated", "identity",
}


# --------------------------------------------------------------------------- #
# Prompts (adapted for the tool-scoped roster).                              #
# Kept as constants so they live in code.                                     #
# --------------------------------------------------------------------------- #

ROUTER_SYSTEM = """You are the router for a tool that builds teams of Claude Code subagents. Your only job is to match a user's request to one blueprint, or report that you cannot.

You will be given a list of available blueprints (id, title, match_hints) and the user's free-text request. Decide which single blueprint best fits.

Rules:
- If exactly one blueprint clearly fits, return it. Do not ask a question when the intent is obvious.
- If the request genuinely fits 2 or 3 blueprints and you cannot choose, return them as ambiguous. Only do this when truly torn.
- If nothing fits, return null.

Return ONLY valid JSON, no prose, no markdown fences, in exactly one of these shapes:
{"match": "<blueprint_id>"}
{"ambiguous": ["<id>", "<id>"], "disambiguation": "<one short question offering the options in plain words>"}
{"match": null}"""

ASSEMBLER_SYSTEM = """You assemble a final agent-team roster from a chosen blueprint and the user's answers. You adapt a template; you do not invent structure. Follow every rule exactly.

You are given: the chosen blueprint (roles with primitive, description/identity templates, any tools override), the role_primitives table (each primitive's allowed_tools maximum, gated flag, fixed boundary text), the tool catalog (valid tool names), and the user's brief + answers.

Your job:
1. For each role, set its "id" to exactly "{prefix}-{role}" (the blueprint's "role" field, prefixed with the given team prefix and a hyphen — e.g. prefix "rs" + role "orchestrator" -> id "rs-orchestrator"). Fill its description and identity from the templates using the brief and answers (replace {prefix} and any answer-driven detail). Keep description to one sentence with a delegation trigger phrase ("Use PROACTIVELY...", "MUST BE USED..."); keep identity to 2-4 sentences. Set "handoff_to" to the same "{prefix}-{role}" ids, not bare role names. For the orchestrator role specifically, never weaken its template's "MUST BE USED... never do it yourself" phrasing into something softer like "Use PROACTIVELY" — without that explicit prohibition, Claude Code's top-level thread will just do the work itself and skip every subagent entirely.
2. Assign each role's tools as a subset of its primitive's allowed_tools maximum. You may NARROW the tool list based on answers. NEVER add a tool outside the primitive's maximum, and never add a tool outside the tool catalog.
3. Set each role's gated flag to its primitive's default. NEVER change it.
4. If a role is gated, its identity text MUST state that it requires human approval before acting.
5. Fill the blueprint's starter_task from the brief and answers.
6. If the guardrail answer names a real risk (not "skip"), turn it into one guardrail sentence and set roster.guardrail to it; otherwise set roster.guardrail to null.

Enforce these invariants; fix your own output before returning if it would violate one:
- exactly one orchestrator
- reviewer's tools are a subset of {Read, Grep, Glob}
- every gated role's identity mentions requiring approval
- every role's tools are a subset of its primitive's allowed_tools maximum, and every tool exists in the tool catalog
- no non-gated role's tools together grant private_data, untrusted_content and exfiltration (see tool_legs)
- deny_rules always include the secret-file deny set

Return ONLY valid JSON, no prose, no markdown fences, matching the roster schema exactly:
{"team_prefix": "...", "blueprint_id": "...", "roles": [{"id": "...", "primitive": "...", "display_name": "...", "description": "...", "model": "...", "tools": [...], "gated": false, "identity": "...", "handoff_to": [...]}], "deny_rules": [...], "guardrail": null}"""


# --------------------------------------------------------------------------- #
# Data types                                                                   #
# --------------------------------------------------------------------------- #

@dataclass
class RouteResult:
    blueprint_id: str | None = None
    ambiguous: list[str] = field(default_factory=list)
    disambiguation: str | None = None

    @property
    def needs_disambiguation(self) -> bool:
        return bool(self.ambiguous)

    @property
    def matched(self) -> bool:
        return self.blueprint_id is not None


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


# --------------------------------------------------------------------------- #
# Pure invariant checks — no LLM, no I/O. Each takes plain data and returns a #
# list of human-readable error strings (empty list = passes).                 #
# --------------------------------------------------------------------------- #

def check_role_fields_present(roles: list[dict]) -> list[str]:
    """The scaffolder indexes role fields directly (role['tools'], etc.) rather than
    using .get() — a role missing a required field crashes cc_scaffold instead of
    failing validation, so this must catch it before the roster ever reaches scaffold().
    """
    errors = []
    for r in roles:
        role_id = r.get("id", "?")
        missing = REQUIRED_ROLE_FIELDS - r.keys()
        if missing:
            errors.append(f"{role_id}: missing required field(s) {sorted(missing)}")
        elif not isinstance(r["tools"], list):
            errors.append(f"{role_id}: 'tools' must be a list")
    return errors


def check_single_orchestrator(roles: list[dict]) -> list[str]:
    count = sum(1 for r in roles if r.get("primitive") == "orchestrator")
    if count != 1:
        return [f"expected exactly 1 orchestrator, got {count}"]
    return []


def check_reviewer_tools_subset(roles: list[dict]) -> list[str]:
    errors = []
    for r in roles:
        if r.get("primitive") != "reviewer":
            continue
        tools = set(r.get("tools", []))
        extra = tools - REVIEWER_MAX_TOOLS
        if extra:
            errors.append(
                f"{r.get('id', '?')}: reviewer tools {sorted(extra)} exceed the "
                f"read-only maximum {sorted(REVIEWER_MAX_TOOLS)}"
            )
    return errors


def check_gated_roles_have_approval_note(roles: list[dict]) -> list[str]:
    errors = []
    for r in roles:
        if not r.get("gated"):
            continue
        text = f"{r.get('identity', '')} {r.get('description', '')}".lower()
        if "approval" not in text:
            errors.append(
                f"{r.get('id', '?')}: gated role's identity/description does not "
                f"mention requiring approval"
            )
    return errors


def check_tools_within_primitive_max(
    roles: list[dict], primitives: dict[str, Any], tool_catalog: set[str]
) -> list[str]:
    errors = []
    for r in roles:
        role_id = r.get("id", "?")
        prim_name = r.get("primitive")
        prim = primitives.get(prim_name) if isinstance(prim_name, str) else None
        if prim is None:
            errors.append(f"{role_id}: unknown primitive '{prim_name}'")
            continue
        max_tools = set(prim["allowed_tools"])
        for t in r.get("tools", []):
            if t not in tool_catalog:
                errors.append(f"{role_id}: tool '{t}' is not in the tool catalog")
            elif t not in max_tools:
                errors.append(
                    f"{role_id}: tool '{t}' exceeds primitive '{prim_name}' "
                    f"maximum {sorted(max_tools)}"
                )
    return errors


TRIFECTA = ("private_data", "untrusted_content", "exfiltration")

# The prefix becomes part of file names (.claude/agents/<prefix>-<role>.md) and of hook command
# lines, so it is held to a strict shape everywhere it can arrive from outside.
PREFIX_RE = re.compile(r"[a-z](?:[a-z0-9-]{0,14}[a-z0-9])?")


def check_prefix(prefix: Any) -> str | None:
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        return "team prefix must be 1-16 characters: lowercase letters, digits and hyphens, starting with a letter"
    return None


def answer_sentence(question: dict, answer: str) -> str:
    """One plain sentence for a chosen answer, from the question's own `phrase` template."""
    phrase = question.get("phrase") or f"{question.get('text', 'Setting').rstrip('?')}: {{answer}}."
    return phrase.replace("{answer}", answer)


def role_legs(tools: list[str], tool_legs: dict[str, list[str]]) -> set[str]:
    return {leg for t in tools for leg in tool_legs.get(t, [])}


def role_leg_set(
    role: dict,
    tool_legs: dict[str, list[str]],
    primitives: dict[str, Any] | None = None,
    guarded_legs: dict[str, list[str]] | None = None,
) -> set[str]:
    """Trifecta legs a role holds. `Bash` counts as fewer legs when the role's primitive
    defines a bash_allow list, because the scaffolder then attaches bash_guard.py to it."""
    prim = (primitives or {}).get(role.get("primitive"), {})
    guarded = bool(prim.get("bash_allow"))
    legs: set[str] = set()
    for t in role.get("tools", []):
        if t == "Bash" and guarded and guarded_legs and "Bash" in guarded_legs:
            legs |= set(guarded_legs["Bash"])
        else:
            legs |= set(tool_legs.get(t, []))
    return legs


def check_no_lethal_trifecta(
    roles: list[dict],
    tool_legs: dict[str, list[str]],
    primitives: dict[str, Any] | None = None,
    guarded_legs: dict[str, list[str]] | None = None,
) -> list[str]:
    """Rule of Two, per role: an unsupervised role holding private data, untrusted content
    and a way to send data out can be steered by injected text into leaking. Gated roles
    are exempt because a human approves each action; the CLI flags them instead.
    """
    errors = []
    for r in roles:
        if r.get("gated"):
            continue
        if role_leg_set(r, tool_legs, primitives, guarded_legs) >= set(TRIFECTA):
            errors.append(
                f"{r.get('id', '?')}: tools {r.get('tools')} grant all three lethal-trifecta legs "
                f"(private data, untrusted content, exfiltration) on a non-gated role"
            )
    return errors


ROLE_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")


def check_role_ids(roster: dict, expected_prefix: str | None = None) -> list[str]:
    """Role ids become file names (.claude/agents/<id>.md), and description/model are written into
    a frontmatter block a line at a time. A roster that came from a model or over an API must not
    be able to escape the folder or add a frontmatter line of its own."""
    errors = []
    prefix = expected_prefix if expected_prefix is not None else roster.get("team_prefix")
    problem = check_prefix(prefix)
    if problem:
        return [f"team_prefix: {problem}"]
    if expected_prefix is not None and roster.get("team_prefix") != expected_prefix:
        errors.append(f"team_prefix must be '{expected_prefix}'")
    for r in roster.get("roles", []):
        rid = r.get("id")
        if not isinstance(rid, str) or not ROLE_ID_RE.fullmatch(rid) or not rid.startswith(f"{prefix}-"):
            errors.append(f"role id {rid!r} must be '{prefix}-<role>' using lowercase letters, digits and hyphens")
        for name in ("description", "display_name"):
            v = r.get(name)
            if not isinstance(v, str) or "\n" in v or "\r" in v or not v.strip():
                errors.append(f"{rid}: {name} must be one non-empty line of text")
        if not isinstance(r.get("model"), str) or not MODEL_RE.fullmatch(r["model"]):
            errors.append(f"{rid}: model {r.get('model')!r} is not a plain model name or alias")
        if not isinstance(r.get("identity"), str) or not r["identity"].strip():
            errors.append(f"{rid}: identity must be non-empty text")
        if not all(isinstance(h, str) and ROLE_ID_RE.fullmatch(h) for h in r.get("handoff_to") or []):
            errors.append(f"{rid}: handoff_to must list role ids")
    g = roster.get("guardrail")
    if g is not None and (not isinstance(g, str) or "\n" in g or "\r" in g):
        errors.append("guardrail must be a single line of text or null")
    return errors


def check_gated_matches_primitive(roles: list[dict], primitives: dict[str, Any]) -> list[str]:
    """`gated` is the primitive's fixed default and is never the caller's to change: the approval
    gate and the audit both rely on gated roles saying so."""
    errors = []
    for r in roles:
        prim = primitives.get(r.get("primitive"))
        if prim is not None and bool(r.get("gated")) != bool(prim["gated"]):
            errors.append(f"{r.get('id', '?')}: gated must be {bool(prim['gated'])} for primitive '{r.get('primitive')}'")
    return errors


def check_unique_role_ids(roles: list[dict]) -> list[str]:
    ids = [r.get("id") for r in roles]
    return [f"duplicate role id '{i}'" for i in sorted({x for x in ids if ids.count(x) > 1})]


FLOW_MODES = ("block", "ask", "off")


def flow_roles(
    roles: list[dict],
    tool_legs: dict[str, list[str]],
    primitives: dict[str, Any] | None = None,
    guarded_legs: dict[str, list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """(private, outbound) role ids for the one-way-flow guard.
    private = holds private_data; outbound = not gated and holds untrusted_content + exfiltration.
    Gated roles are not outbound: a human already approves each of their actions."""
    private, outbound = [], []
    for r in roles:
        if r.get("primitive") == "orchestrator":
            continue
        legs = role_leg_set(r, tool_legs, primitives, guarded_legs)
        if "private_data" in legs:
            private.append(r["id"])
        if not r.get("gated") and {"untrusted_content", "exfiltration"} <= legs:
            outbound.append(r["id"])
    return private, outbound


def check_relay_closed(
    roster: dict,
    tool_legs: dict[str, list[str]],
    primitives: dict[str, Any] | None = None,
    guarded_legs: dict[str, list[str]] | None = None,
    allow_relay_open: bool = False,
) -> list[str]:
    """Team-level rule: if the team has a web member and a file-reading member, the one-way-flow guard
    must be on (roster.flow_guard "block" or "ask"; absent means "block"). "off" needs an explicit
    opt-out by the person, never by a planner's output alone."""
    mode = roster.get("flow_guard", "block")
    if mode not in FLOW_MODES:
        return [f"flow_guard must be one of {', '.join(FLOW_MODES)}"]
    private, outbound = flow_roles(roster["roles"], tool_legs, primitives, guarded_legs)
    if private and outbound and mode == "off" and not allow_relay_open:
        return [f"relay open: {', '.join(outbound)} can reach the web and {', '.join(private)} can read "
                f"files, and the one-way-flow guard is off (opt out explicitly with --no-flow-guard)"]
    return []


def check_deny_rules_include_secrets(
    deny_rules: list[str], secret_deny_rules: list[str]
) -> list[str]:
    missing = [rule for rule in secret_deny_rules if rule not in deny_rules]
    if missing:
        return [f"deny_rules missing required secret-file rules: {missing}"]
    return []


# --------------------------------------------------------------------------- #
# Architect                                                                    #
# --------------------------------------------------------------------------- #

class Architect:
    def __init__(self, blueprints_path: str | Path, llm_call: LlmCall | None = None):
        with open(blueprints_path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        self.tool_catalog: set[str] = set(self.data["tool_catalog"])
        self.primitives: dict[str, Any] = self.data["role_primitives"]
        self.blueprints: dict[str, Any] = self.data["blueprints"]
        self.secret_deny_rules: list[str] = self.data["secret_deny_rules"]
        self.tool_legs: dict[str, list[str]] = {
            k: v for k, v in self.data["tool_legs"].items() if not k.startswith("_")
        }
        self.guarded_legs: dict[str, list[str]] = {
            k: v for k, v in self.data.get("guarded_tool_legs", {}).items() if not k.startswith("_")
        }
        self._llm_call = llm_call

    @property
    def has_llm(self) -> bool:
        return self._llm_call is not None

    # ---- LLM plumbing (injectable so tests can stub it) ------------------- #

    def _call(self, system: str, user: str) -> str:
        if self._llm_call is None:
            raise RuntimeError(
                "no llm_call configured — pass one to Architect(..., llm_call=...) "
                "to use route()/assemble()"
            )
        return self._llm_call(system, user)

    @staticmethod
    def _parse_json(text: str) -> Any:
        cleaned = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        return json.loads(cleaned)

    # ---- Turn 2: route ------------------------------------------------------ #

    def route(self, brief: str) -> RouteResult:
        catalog = [
            {"id": bp["id"], "title": bp["title"], "match_hints": bp.get("match_hints", [])}
            for bp in self.blueprints.values()
        ]
        user = f'Available blueprints:\n{json.dumps(catalog, indent=2)}\n\nUser request:\n"{brief}"'
        raw = self._call(ROUTER_SYSTEM, user)
        obj = self._parse_json(raw)
        if obj.get("ambiguous"):
            return RouteResult(ambiguous=obj["ambiguous"], disambiguation=obj.get("disambiguation"))
        return RouteResult(blueprint_id=obj.get("match"))

    # ---- Assemble (after answers collected) ---------------------------------- #

    def assemble(
        self,
        blueprint_id: str,
        brief: str,
        answers: dict[str, str],
        guardrail: str | None,
        prefix: str,
        retry: bool = True,
    ) -> dict:
        bp = self.blueprints[blueprint_id]
        user = (
            f"Chosen blueprint:\n{json.dumps(bp, indent=2)}\n\n"
            f"Role primitives:\n{json.dumps(self.primitives, indent=2)}\n\n"
            f"Tool catalog (valid tool names):\n{json.dumps(sorted(self.tool_catalog))}\n\n"
            f"Secret-file deny set (deny_rules must include all of these):\n"
            f"{json.dumps(self.secret_deny_rules)}\n\n"
            f'Original brief:\n"{brief}"\n\n'
            f"Answers:\n{json.dumps(answers, indent=2)}\n"
            f'guardrail: "{guardrail or "skip"}"\n\n'
            f"Team prefix: {prefix}"
        )
        raw = self._call(ASSEMBLER_SYSTEM, user)
        roster = self._parse_json(raw)
        errors = self.validate(roster) + check_role_ids(roster, prefix)
        if errors:
            if retry:
                fix_user = (
                    user
                    + "\n\nYour previous output failed validation:\n- "
                    + "\n- ".join(errors)
                    + "\nReturn corrected JSON only."
                )
                raw = self._call(ASSEMBLER_SYSTEM, fix_user)
                roster = self._parse_json(raw)
                errors = self.validate(roster) + check_role_ids(roster, prefix)
            if errors:
                raise ValidationError(errors)
        return roster

    def fallback_roster(self, blueprint_id: str, prefix: str | None = None) -> dict:
        """Deterministic un-adapted roster if the LLM keeps failing. No LLM, no I/O."""
        bp = self.blueprints[blueprint_id]
        prefix = prefix or blueprint_id.replace("_", "-")[:8]
        roles = []
        for r in bp["roles"]:
            prim = self.primitives[r["primitive"]]
            roles.append({
                "id": f"{prefix}-{r['role']}",
                "primitive": r["primitive"],
                "display_name": r.get("display_name", r["role"].title()),
                "description": r["description"].replace("{prefix}", prefix),
                "model": r.get("model", "sonnet"),
                "tools": r.get("tools") or list(prim["allowed_tools"]),
                "gated": prim["gated"],
                "identity": r["identity"].replace("{prefix}", prefix),
                "handoff_to": [f"{prefix}-{h}" for h in r.get("handoff_to", [])],
            })
        return {
            "team_prefix": prefix,
            "blueprint_id": blueprint_id,
            "roles": roles,
            "deny_rules": list(self.secret_deny_rules),
            "guardrail": None,
        }

    # ---- Keyless path: no model, no key (Phase 10, step 2) ------------------ #

    def route_offline(self, brief: str) -> RouteResult:
        """Score each blueprint by how many of its match_hints appear in the brief. One clear
        winner routes; a tie or no match returns the candidates for the user to choose from."""
        text = " ".join(brief.lower().split())
        scores = {
            bid: sum(1 for h in bp.get("match_hints", []) if h.lower() in text)
            for bid, bp in self.blueprints.items()
        }
        best = max(scores.values(), default=0)
        if best == 0:
            return RouteResult(
                ambiguous=sorted(self.blueprints),
                disambiguation="I couldn't tell from that description. Which team is closest?",
            )
        winners = sorted(bid for bid, s in scores.items() if s == best)
        if len(winners) == 1:
            return RouteResult(blueprint_id=winners[0])
        return RouteResult(ambiguous=winners, disambiguation="That fits more than one team. Which is closest?")

    def check_answers(
        self, blueprint_id: str, answers: dict[str, str], guardrail: str | None
    ) -> list[str]:
        """Answers and the guardrail may only be options the blueprint itself offers, so nothing
        free-form ever reaches an agent file through the keyless path."""
        bp = self.blueprints[blueprint_id]
        errors = []
        questions = {q["id"]: q for q in bp.get("questions", [])}
        for qid, answer in answers.items():
            if qid not in questions:
                errors.append(f"unknown question '{qid}'")
            elif answer not in questions[qid]["options"]:
                errors.append(f"'{answer}' is not an option for '{qid}'")
        if guardrail is not None and guardrail not in bp.get("problem_chips", []):
            errors.append(f"'{guardrail}' is not one of this team's guardrail choices")
        return errors

    def assemble_offline(
        self,
        blueprint_id: str,
        answers: dict[str, str],
        guardrail: str | None,
        prefix: str,
    ) -> dict:
        """The blueprint's templates, filled in deterministically. Each answer adds one plain
        sentence to the role its question `affects`; a chosen guardrail becomes roster.guardrail.
        Same roster schema and the same validate() as a model-assembled roster."""
        prefix_error = check_prefix(prefix)
        if prefix_error:
            raise ValueError(prefix_error)
        errors = self.check_answers(blueprint_id, answers, guardrail)
        if errors:
            raise ValueError("; ".join(errors))
        bp = self.blueprints[blueprint_id]
        roster = self.fallback_roster(blueprint_id, prefix)
        by_id = {r["id"]: r for r in roster["roles"]}
        for q in bp.get("questions", []):
            answer = answers.get(q["id"])
            target, _, fld = q.get("affects", "").partition(".")
            role = by_id.get(f"{prefix}-{target}")
            if answer is None or role is None or fld not in ("description", "identity"):
                continue
            role[fld] = f"{role[fld]} {answer_sentence(q, answer)}"
        if guardrail and not guardrail.lower().startswith(("nothing", "skip")):
            roster["guardrail"] = f"Watch for this recurring problem: {guardrail[:1].lower()}{guardrail[1:]}."
        problems = self.validate(roster)
        if problems:
            raise ValidationError(problems)
        return roster

    def starter_task(self, blueprint_id: str, brief: str, answers: dict[str, str]) -> str:
        """A ready-to-run job for the team: the blueprint's starter task plus the answers that
        `affect` it. Used as the default text on the GUI's Run page."""
        bp = self.blueprints[blueprint_id]
        text = bp.get("starter_task", "").replace("{brief}", " ".join(brief.split()) or "the task above")
        for q in bp.get("questions", []):
            if q.get("affects") == "starter_task" and q["id"] in answers:
                text += " " + answer_sentence(q, answers[q["id"]])
        return text

    # ---- Validator (deterministic, invariants from blueprints.json) -------- #

    def flow_roles(self, roster: dict) -> tuple[list[str], list[str]]:
        return flow_roles(roster.get("roles", []), self.tool_legs, self.primitives, self.guarded_legs)

    def validate(self, roster: dict, allow_relay_open: bool = False) -> list[str]:
        roles = roster.get("roles")
        if not isinstance(roles, list) or not roles:
            return ["roster.roles missing, not a list, or empty"]

        errors: list[str] = []
        errors += check_role_fields_present(roles)
        if errors:            # the checks below index fields directly
            return errors
        errors += check_role_ids(roster)
        errors += check_unique_role_ids(roles)
        errors += check_single_orchestrator(roles)
        errors += check_reviewer_tools_subset(roles)
        errors += check_gated_roles_have_approval_note(roles)
        errors += check_tools_within_primitive_max(roles, self.primitives, self.tool_catalog)
        errors += check_gated_matches_primitive(roles, self.primitives)
        errors += check_no_lethal_trifecta(roles, self.tool_legs, self.primitives, self.guarded_legs)
        errors += check_deny_rules_include_secrets(
            roster.get("deny_rules", []), self.secret_deny_rules
        )
        errors += check_relay_closed(roster, self.tool_legs, self.primitives, self.guarded_legs, allow_relay_open)
        return errors


# --------------------------------------------------------------------------- #
# CLI smoke test: python -m teambuild.architect "research note-taking apps"     #
# Requires an llm_call to be wired in; this stub just shows the shape.         #
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys

    print(
        "architect.py has no default LLM wiring in Phase 1 — inject an llm_call "
        "into Architect(...) from the CLI (Phase 3) to run route()/assemble().",
        file=sys.stderr,
    )
