"""team-build entry point: four-turn terminal flow.

Wires architect.route/assemble/validate + cc_scaffold together. The planner (a model, if one is
configured) lives in planner.py; with none configured, or with --no-ai, everything runs on the
built-in templates: no model, no key. `team-build ui` starts the local web interface.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from teambuild.architect import TRIFECTA, Architect, role_leg_set
from teambuild.cc_scaffold import scaffold, destroy
from teambuild.planner import (
    PlannerError, assemble_with_fallback, load_config, make_llm_call, route_with_fallback,
)

BLUEPRINTS_PATH = Path(__file__).parent / "blueprints.json"


# --------------------------------------------------------------------------- #
# Terminal helpers                                                             #
# --------------------------------------------------------------------------- #

def pick(prompt: str, options: list[str]) -> str:
    print(f"\n{prompt}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("> ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        print("  enter a number from the list")


def print_roster(roster: dict, arch: Architect) -> None:
    print(f"\n  team_prefix : {roster.get('team_prefix')}")
    print(f"  blueprint   : {roster.get('blueprint_id')}")
    if roster.get("guardrail"):
        print(f"  guardrail   : {roster['guardrail']}")
    print("  roles:")
    for r in roster.get("roles", []):
        legs = role_leg_set(r, arch.tool_legs, arch.primitives, arch.guarded_legs)
        flags = []
        if legs >= set(TRIFECTA):
            flags.append("TRIFECTA")
        if r.get("gated"):
            flags.append("GATED")
        flag = f" [{', '.join(flags)}]" if flags else ""
        tools = ", ".join(r.get("tools", [])) or "none"
        print(f"    - {r.get('id')} ({r.get('primitive')}, {r.get('model')}){flag}")
        print(f"        tools: {tools}")
        guard = arch.primitives.get(r.get("primitive"), {}).get("bash_allow")
        note = "  (Bash limited by bash_guard.py)" if guard and "Bash" in r.get("tools", []) else ""
        print(f"        legs : {', '.join(l for l in TRIFECTA if l in legs) or 'none'}{note}")
    print(f"  deny_rules  : {len(roster.get('deny_rules', []))} rule(s)")
    private, outbound = arch.flow_roles(roster)
    if private and outbound:
        mode = roster.get("flow_guard", "block")
        if mode == "off":
            print(f"  one-way flow: OFF [RELAY OPEN] ({', '.join(outbound)} can still reach the web after "
                  f"{', '.join(private)} read files)")
        else:
            verb = "refused" if mode == "block" else "asks you first"
            print(f"  one-way flow: on ({mode}): after any of {', '.join(private)} has been used, "
                  f"work for {', '.join(outbound)} is {verb}")


# --------------------------------------------------------------------------- #
# Subcommands: --audit, board                                                  #
# --------------------------------------------------------------------------- #

FAIL_ON = ("fail", "warn", "never")


def audit_exit_code(result, fail_on: str = "fail") -> int:
    """1 when the result reaches the --fail-on level (fail: a FAIL; warn: a WARN or FAIL; never: never)."""
    if fail_on == "never":
        return 0
    if result.failed:
        return 1
    return 1 if fail_on == "warn" and result.overall == "WARN" else 0


def run_audit(args: list[str]) -> None:
    """`team-build --audit [dir] [--report FILE] [--fail-on fail|warn|never]` — offline; the exit code is
    1 when the result reaches --fail-on (default: a FAIL), so a CI job or pre-commit hook can stop on it."""
    from teambuild.approval_log import read as read_approvals
    from teambuild.audit import audit, format_text, render_report

    report_path = None
    if "--report" in args:
        i = args.index("--report")
        if i + 1 >= len(args):
            sys.exit("usage: team-build --audit [dir] [--report FILE]")
        report_path = Path(args[i + 1])
        del args[i:i + 2]
    fail_on = "fail"
    if "--fail-on" in args:
        i = args.index("--fail-on")
        if i + 1 >= len(args) or args[i + 1] not in FAIL_ON:
            sys.exit("usage: team-build --audit [dir] [--report FILE] [--fail-on fail|warn|never]")
        fail_on = args[i + 1]
        del args[i:i + 2]
    positional = [a for a in args if not a.startswith("--")]
    target = Path(positional[0]) if positional else Path.cwd()
    if not target.is_dir():
        sys.exit(f"not a directory: {target}")
    result = audit(target)
    print(format_text(result))
    if report_path:
        approvals = read_approvals(target, limit=500)
        report_path.write_text(render_report(result, approvals=approvals), encoding="utf-8")
        print(f"\nreport written to {report_path}")
    sys.exit(audit_exit_code(result, fail_on))


def run_approvals(args: list[str]) -> None:
    """`team-build --approvals [dir] [--report FILE]` — the approval log, for one folder or all."""
    from teambuild.approval_log import format_text, log_path, read, render_markdown

    report_path = None
    if "--report" in args:
        i = args.index("--report")
        if i + 1 >= len(args):
            sys.exit("usage: team-build --approvals [dir] [--report FILE]")
        report_path = Path(args[i + 1])
        del args[i:i + 2]
    positional = [a for a in args if not a.startswith("--")]
    folder = Path(positional[0]) if positional else None
    if folder is not None and not folder.is_dir():
        sys.exit(f"not a directory: {folder}")
    entries = read(folder)
    print(f"approval log: {log_path()}")
    print(format_text(entries))
    if report_path:
        report_path.write_text("# Approval decisions\n\n" + render_markdown(entries, folder.resolve() if folder else None),
                               encoding="utf-8")
        print(f"\nreport written to {report_path}")


def run_board() -> None:
    """Launch the board (teambuild/board/app.py) via uvicorn in a subprocess, on 127.0.0.1:8123."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        sys.exit("The board needs fastapi and uvicorn: pip install 'agent-troops[ui]'")
    if not (Path(__file__).parent / "board" / "static" / "index.html").exists():
        print("The board page is not built (a checkout without the build): "
              "cd board/frontend && npm install && npm run build", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "teambuild.board.app:app",
             "--host", "127.0.0.1", "--port", "8123"],
            check=True,
        )
    except KeyboardInterrupt:
        pass


# --------------------------------------------------------------------------- #
# Main: four-turn flow                                                         #
# --------------------------------------------------------------------------- #

def run_ui(args: list[str]) -> None:
    """`team-build ui [--port N] [--no-open] [--root DIR]` — the local web interface (127.0.0.1 only).
    `--root` limits every folder the page can list, create in, audit or run to DIR (default: home)."""
    try:
        from teambuild.ui.server import serve
    except ImportError:
        sys.exit("The web interface needs fastapi and uvicorn: pip install 'teambuild[ui]'")
    usage = "usage: team-build ui [--port N] [--no-open] [--root DIR]"
    port, root = 8124, None
    if "--port" in args:
        i = args.index("--port")
        if i + 1 >= len(args) or not args[i + 1].isdigit():
            sys.exit(usage)
        port = int(args[i + 1])
    if "--root" in args:
        i = args.index("--root")
        if i + 1 >= len(args) or not Path(args[i + 1]).expanduser().is_dir():
            sys.exit(usage + "\n--root must be an existing folder")
        root = Path(args[i + 1]).expanduser().resolve()
    serve(port=port, open_browser="--no-open" not in args, root=root)


def run_policy(args: list[str]) -> int:
    """`team-build policy init|check [DIR]`: write a starter .agent-troops.json, or check one."""
    from teambuild import policy_file
    usage = "usage: team-build policy init|check [DIR]"
    if not args or args[0] not in ("init", "check"):
        print(usage)
        return 2
    folder = Path(args[1]).expanduser() if len(args) > 1 else Path.cwd()
    if not folder.is_dir():
        print(f"not a folder: {folder}")
        return 1
    if args[0] == "init":
        try:
            path = policy_file.write_template(folder)
        except FileExistsError as e:
            print(f"error: {e}")
            return 1
        print(f"wrote {path}: every rule off. Edit it, commit it; see docs/reference.md, \"A shared policy\".")
        return 0
    path = folder / policy_file.NAME
    if not path.is_file():
        print(f"no {policy_file.NAME} in {folder}")
        return 1
    policy = policy_file.load(path)
    print(policy_file.describe(policy))
    return 0 if policy.valid else 1


HELP = """\
usage:
  team-build guard install [--shared | --user] [--target DIR] [--mcp] [--sensitive PATTERNS] [--flow block|ask]
  team-build guard status [--target DIR]
  team-build guard remove [--user] [--target DIR]
  team-build policy init|check [DIR]
  team-build "<brief>" [--dry-run] [--target DIR] [--prefix NAME] [--fence-reads]
                       [--flow block|ask] [--no-flow-guard] [--no-ai]
  team-build --destroy PREFIX [--target DIR] [--dry-run]
  team-build --audit [DIR] [--report FILE] [--fail-on fail|warn|never]
  team-build --approvals [DIR] [--report FILE]
  team-build ui [--port N] [--no-open] [--root DIR]
  team-build board
  team-build demo [--target EMPTY_DIR] [--fast]

See docs/reference.md for what each option does."""


def main() -> None:
    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help", "help"):
        print(HELP)
        return

    if args and args[0] == "ui":       # before load_dotenv: the UI server's environment is what `claude` inherits
        run_ui(args[1:])
        return

    if args and args[0] == "guard":
        from teambuild.guard import main as run_guard
        sys.exit(run_guard(args[1:]))

    if args and args[0] == "policy":
        sys.exit(run_policy(args[1:]))

    if args and args[0] == "board":
        run_board()
        return

    if args and args[0] == "demo":
        from teambuild.demo import main as run_demo
        run_demo(args[1:])
        return

    if args and args[0] == "--audit":
        run_audit(args[1:])
        return

    if args and args[0] == "--approvals":
        run_approvals(args[1:])
        return

    load_dotenv()
    dry_run = "--dry-run" in args
    fence_reads = "--fence-reads" in args
    no_ai = "--no-ai" in args
    no_flow_guard = "--no-flow-guard" in args
    args = [a for a in args if a not in ("--dry-run", "--fence-reads", "--no-ai", "--no-flow-guard")]
    flow_mode = "block"
    if "--flow" in args:
        i = args.index("--flow")
        if i + 1 >= len(args) or args[i + 1] not in ("block", "ask"):
            sys.exit("usage: --flow block|ask   (block = refuse, ask = ask you first)")
        flow_mode = args[i + 1]
        del args[i:i + 2]
    if no_flow_guard:
        flow_mode = "off"

    target_dir = Path.cwd()
    if "--target" in args:
        i = args.index("--target")
        target_dir = Path(args[i + 1])
        del args[i:i + 2]

    if args and args[0] == "--destroy":
        if len(args) < 2:
            sys.exit("usage: team-build --destroy <prefix> [--target <dir>] [--dry-run]")
        for line in destroy(args[1], target_dir, dry_run=dry_run):
            print(line)
        return

    prefix: str | None = None
    if "--prefix" in args:
        i = args.index("--prefix")
        prefix = args[i + 1]
        del args[i:i + 2]

    brief = args[0] if args else input("What do you want your team to do?\n> ").strip()
    if not brief:
        sys.exit("empty brief")

    llm_call = None
    if not no_ai:
        try:
            llm_call = make_llm_call(load_config())
        except PlannerError as e:
            print(f"Planner not usable ({e}).", file=sys.stderr)
        if llm_call is None:
            print("No planner configured: using the built-in templates (no model, no key needed). "
                  "See docs/reference.md, \"The planner\".", file=sys.stderr)
    arch = Architect(BLUEPRINTS_PATH, llm_call=llm_call)

    # Turn 2 — route (silent when confident, one disambiguation otherwise)
    route, _source = route_with_fallback(arch, brief)
    if route.needs_disambiguation:
        blueprint_id = pick(route.disambiguation or "Which is closest?", route.ambiguous)
    else:
        assert route.blueprint_id is not None
        blueprint_id = route.blueprint_id

    bp = arch.blueprints[blueprint_id]
    prefix = prefix or blueprint_id.split("_")[0][:4]

    # Turn 3 — the blueprint's own questions (no LLM)
    answers: dict[str, str] = {}
    for q in bp.get("questions", []):
        answers[q["id"]] = pick(q["text"], q["options"])

    # Turn 4 — optional guardrail chip (no LLM)
    guardrail: str | None = None
    chips = bp.get("problem_chips", [])
    if chips:
        chosen = pick("Anything that usually goes wrong the team should watch for?", chips)
        if not chosen.lower().startswith(("nothing", "skip")):
            guardrail = chosen

    # assemble (model, 1 retry inside) -> deterministic offline roster on failure or with no model
    try:
        roster, _source, note = assemble_with_fallback(arch, brief, blueprint_id, answers, guardrail, prefix)
    except ValueError as e:
        sys.exit(f"Cannot build that team: {e}")
    if note:
        print(f"\n{note}", file=sys.stderr)

    # dry-run preview -> confirm -> scaffold
    roster["flow_guard"] = flow_mode
    print_roster(roster, arch)
    if dry_run:
        for line in scaffold(roster, target_dir, dry_run=True, fence_reads=fence_reads):
            print(line)
        return
    if input("\nScaffold this team? [y/N] ").strip().lower() != "y":
        print("Aborted; nothing written.")
        return
    for line in scaffold(roster, target_dir, dry_run=False, fence_reads=fence_reads):
        print(line)
    if fence_reads:
        print("\nRead fence on: Read/Grep/Glob now refuse paths outside this project in EVERY "
              "session here, yours included. `--destroy` switches it off again.")
    orchestrator = next(r["id"] for r in roster["roles"] if r["primitive"] == "orchestrator")
    print(f"\nStart the team with the orchestrator as the main session:\n"
          f"  claude --agent {orchestrator}")


if __name__ == "__main__":
    main()