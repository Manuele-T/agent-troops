"""The local web interface: a small FastAPI app.

`create_app()` is a factory so tests can give it a fake home folder, allowed hosts and a stub
`claude`. `team-build ui` (server.py) wires the real values and binds 127.0.0.1 only.

Every endpoint that changes anything needs the per-launch token (security.py). Blocking work
(scaffolding, audits, the planner) is done in plain `def` endpoints, which FastAPI runs in a thread
pool. Teams run in a terminal (terminal.py), never headless; approvals and results are on the board.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from teambuild import planner
from teambuild.architect import (
    TRIFECTA, Architect, ROLE_ID_RE, check_prefix, check_role_ids, role_leg_set,
)
from teambuild import approval_log
from teambuild import guard as guard_mode
from teambuild.audit import _board_gated_roles, _load_settings, audit, format_text, render_report
from teambuild.cc_scaffold import MANIFEST_NAME, destroy, scaffold
from teambuild.planner import PlannerConfig, PlannerError
from teambuild.ui.terminal import TerminalError, find_terminal, open_team
from teambuild.ui.security import PathError, make_guard, new_token, resolve_under_home

HERE = Path(__file__).resolve().parent
FRONTEND = HERE / "frontend"
BLUEPRINTS_PATH = HERE.parent / "blueprints.json"
TOKEN_PLACEHOLDER = "__TB_TOKEN__"
VERSION = "0.1.0"
MAX_FOLDER_ENTRIES = 500

LEGS_HELP = {
    "private_data": {"letter": "P", "label": "Private data",
                     "text": "Can read files on your computer."},
    "untrusted_content": {"letter": "U", "label": "Untrusted content",
                          "text": "Reads web pages or messages that someone else wrote."},
    "exfiltration": {"letter": "E", "label": "Can send out",
                     "text": "Can send information off your computer."},
    "gated": {"letter": "G", "label": "Needs your approval",
              "text": "Stops and asks you before it acts."},
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


# --------------------------------------------------------------------------- #
# Request bodies                                                               #
# --------------------------------------------------------------------------- #

class PreviewBody(BaseModel):
    brief: str = Field("", max_length=4000)
    blueprint_id: str | None = None
    answers: dict[str, str] = Field(default_factory=dict)
    guardrail: str | None = None
    prefix: str | None = None
    target: str | None = None
    fence_reads: bool = False
    use_planner: bool = False
    flow_mode: Literal["block", "ask"] = "block"      # "off" is CLI-only (--no-flow-guard)


class CreateBody(BaseModel):
    roster: dict[str, Any]
    target: str
    fence_reads: bool = False
    overwrite: bool = False


class DestroyBody(BaseModel):
    prefix: str
    target: str
    dry_run: bool = False


class AuditBody(BaseModel):
    target: str


class SettingsBody(BaseModel):
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None        # None = keep the stored key, "" = remove it
    default_folder: str | None = None


class TerminalBody(BaseModel):
    folder: str
    team: str
    job: str | None = Field(None, max_length=20000)


class GuardInstallBody(BaseModel):
    folder: str
    scope: Literal["local", "shared", "user"] = "local"
    mcp: bool = False
    sensitive: list[str] = Field(default_factory=list, max_length=50)
    flow: Literal["block", "ask"] = "block"


class GuardRemoveBody(BaseModel):
    folder: str
    scope: Literal["project", "user"] = "project"


class SitesBody(BaseModel):
    allow: list[str] = Field(default_factory=list, max_length=guard_mode.SITES_MAX)
    block: list[str] = Field(default_factory=list, max_length=guard_mode.SITES_MAX)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _bad(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def describe_role(role: dict, arch: Architect) -> dict[str, Any]:
    """What a role is allowed to do, in the terms the page shows."""
    prim = arch.primitives.get(role.get("primitive"), {})
    tools = list(role.get("tools") or [])
    legs = role_leg_set(role, arch.tool_legs, arch.primitives, arch.guarded_legs)
    return {
        "id": role.get("id"),
        "display_name": role.get("display_name"),
        "primitive": role.get("primitive"),
        "model": role.get("model"),
        "tools": tools,
        "legs": [leg for leg in TRIFECTA if leg in legs],
        "gated": bool(role.get("gated")),
        "guarded": bool(prim.get("bash_allow")) and "Bash" in tools,
        "trifecta": legs >= set(TRIFECTA),
        "description": role.get("description"),
        "identity": role.get("identity"),
        "boundaries": list(prim.get("boundary_text", [])),
    }


def _claude_version(exe: str) -> str | None:
    try:
        p = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=8)
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _flow_view(arch: Architect, roster: dict) -> dict[str, Any] | None:
    """The one-way-flow rule for this team, if it has one (Phase 12)."""
    private, outbound = arch.flow_roles(roster)
    if not (private and outbound):
        return None
    return {"private": private, "outbound": outbound, "mode": roster.get("flow_guard", "block")}


def _audit_json(result) -> dict[str, Any]:
    return {
        "target": result.target,
        "overall": result.overall,
        "failed": result.failed,
        "roles": [
            {"id": r.id, "path": r.path, "tools": r.tools, "legs": [l for l in TRIFECTA if l in r.legs],
             "gated": r.gated, "guarded": r.guarded, "verdict": r.verdict, "notes": r.notes}
            for r in result.roles
        ],
        "repo_notes": [{"level": lvl, "text": text} for lvl, text in result.repo_notes],
        "skipped": result.skipped,
    }


def _read_manifest(folder: Path) -> dict:
    try:
        data = json.loads((folder / ".claude" / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_teams(folder: Path) -> list[dict[str, Any]]:
    """Teams runnable from `folder`: those in the tool's manifest, else any `<prefix>-orchestrator`."""
    agents_dir = folder / ".claude" / "agents"
    if not agents_dir.is_dir():
        return []
    present = {p.stem for p in agents_dir.glob("*.md")}
    found: dict[str, dict[str, Any]] = {}
    for prefix, team in (_read_manifest(folder).get("teams") or {}).items():
        if isinstance(team, dict) and f"{prefix}-orchestrator" in present:
            found[prefix] = {"prefix": prefix, "blueprint_id": team.get("blueprint_id"), "managed": True}
    for stem in sorted(present):
        if stem.endswith("-orchestrator") and ROLE_ID_RE.fullmatch(stem):
            prefix = stem[: -len("-orchestrator")]
            found.setdefault(prefix, {"prefix": prefix, "blueprint_id": None, "managed": False})
    result = audit(folder)
    board_gated = (_board_gated_roles(_load_settings(folder / ".claude"))
                   if (folder / ".claude" / "hooks" / "board_gate.py").is_file() else set())
    for team in found.values():
        p = team["prefix"]
        roles = [r for r in result.roles if r.id.startswith(p + "-")]
        team["orchestrator"] = f"{p}-orchestrator"
        team["roles"] = [r.id for r in roles]
        team["gated_roles"] = [r.id for r in roles if r.gated]
        team["board_gated"] = sorted(r.id for r in roles if r.id in board_gated)
        team["one_way_flow"] = any(lvl == "INFO" and text.startswith(f"team {p}: one-way flow on")
                                   for lvl, text in result.repo_notes)
        team["relay_open"] = any(lvl in ("WARN", "FAIL") and text.startswith(f"team {p}:")
                                 for lvl, text in result.repo_notes)
        team_notes = [lvl for lvl, text in result.repo_notes if text.startswith(f"team {p}:")]
        team["verdict"] = ("FAIL" if any(r.verdict == "FAIL" for r in roles) or "FAIL" in team_notes
                           else "WARN" if any(r.verdict == "WARN" for r in roles) or "WARN" in team_notes
                           else "PASS")
    return sorted(found.values(), key=lambda t: t["prefix"])


# --------------------------------------------------------------------------- #
# App                                                                          #
# --------------------------------------------------------------------------- #

def create_app(
    *,
    token: str | None = None,
    home: Path | None = None,
    allowed_hosts: set[str] | None = None,
    find_claude=None,
    config_file: Path | None = None,
    board_port: int = 8123,
    home_label: str = "your home folder",
    on_shutdown=None,
    terminal_opener=None,
    guard_home: Path | None = None,
    sites_file: Path | None = None,
    approvals_file: Path | None = None,
) -> FastAPI:
    token = token or new_token()
    home = (home or Path.home()).resolve()
    find_claude = find_claude or (lambda: shutil.which("claude"))
    cfg_path = config_file or planner.config_path()
    opener = terminal_opener or open_team

    def under_home(raw: object, **kw: Any) -> Path:
        try:
            return resolve_under_home(raw, home, label=home_label, **kw)
        except PathError as exc:
            raise _bad(400, str(exc)) from None

    def make_arch(use_planner: bool) -> tuple[Architect, PlannerConfig]:
        cfg = planner.load_config(path=cfg_path)
        llm = None
        if use_planner and cfg.provider != "none":
            try:
                llm = planner.make_llm_call(cfg)
            except PlannerError as exc:
                raise _bad(400, f"The planner is not usable: {exc}") from None
        return Architect(BLUEPRINTS_PATH, llm_call=llm), cfg

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if on_shutdown:      # runs before uvicorn re-raises SIGINT/SIGTERM, so cleanup is not skipped
            on_shutdown()

    app = FastAPI(title="Team Builder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token

    guard = make_guard(token, allowed_hosts or {"127.0.0.1:8124", "localhost:8124"})

    @app.middleware("http")
    async def _guard_and_headers(request: Request, call_next):
        response = await guard(request, call_next)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    # ---- page --------------------------------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace(TOKEN_PLACEHOLDER, token))

    # ---- read-only info ------------------------------------------------------ #

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        exe = find_claude()
        cfg = planner.load_config(path=cfg_path)
        return {
            "version": VERSION,
            "home": str(home),
            "home_label": home_label,
            "claude": {"found": bool(exe), "version": _claude_version(exe) if exe else None},
            "planner": cfg.public(),
            "board": {"url": f"http://127.0.0.1:{board_port}/", "running": _port_open(board_port)},
            "default_folder": planner.read_setting("default_folder", None, cfg_path),
            "install_url": "https://code.claude.com/docs/en/overview",
        }

    @app.get("/api/blueprints")
    def blueprints() -> dict[str, Any]:
        arch = Architect(BLUEPRINTS_PATH)
        out = []
        for bid, bp in arch.blueprints.items():
            roles = []
            for r in bp["roles"]:
                prim = arch.primitives[r["primitive"]]
                view = describe_role({
                    "id": r["role"], "display_name": r.get("display_name", r["role"].title()),
                    "primitive": r["primitive"], "model": r.get("model", "sonnet"),
                    "tools": r.get("tools") or list(prim["allowed_tools"]), "gated": prim["gated"],
                    "description": r["description"].replace("{prefix}", "the team"),
                    "identity": r["identity"].replace("{prefix}", "the team"),
                }, arch)
                roles.append(view)
            out.append({
                "id": bid, "title": bp["title"], "roles": roles,
                "questions": [{"id": q["id"], "text": q["text"], "options": q["options"]}
                              for q in bp.get("questions", [])],
                "problem_chips": bp.get("problem_chips", []),
                "match_hints": bp.get("match_hints", []),
            })
        return {"blueprints": out, "legs": LEGS_HELP}

    @app.get("/api/folders")
    def folders(path: str = "", hidden: bool = False) -> dict[str, Any]:
        if path:
            here = under_home(path, allow_home=True)
        else:
            saved = planner.read_setting("default_folder", None, cfg_path)
            try:
                here = resolve_under_home(saved, home, allow_home=True, label=home_label) if saved else home
            except PathError:
                here = home
        entries = []
        try:
            children = sorted(here.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            raise _bad(400, "that folder cannot be opened") from None
        for child in children:
            if len(entries) >= MAX_FOLDER_ENTRIES:
                break
            if not hidden and child.name.startswith("."):
                continue
            try:
                real = child.resolve()
                if not child.is_dir() or not (real == home or real.is_relative_to(home)):
                    continue   # files, and links that lead outside home, are not offered
                entries.append({"name": child.name, "path": str(real),
                                "has_team": (real / ".claude" / "agents").is_dir()})
            except OSError:
                continue
        return {
            "path": str(here), "home": str(home),
            "parent": None if here == home else str(here.parent),
            "has_team": (here / ".claude" / "agents").is_dir(), "entries": entries,
        }

    @app.get("/api/teams")
    def teams(path: str) -> dict[str, Any]:
        folder = under_home(path, allow_home=False)
        return {"path": str(folder), "teams": discover_teams(folder)}

    # ---- settings ------------------------------------------------------------ #

    def settings_view() -> dict[str, Any]:
        cfg = planner.load_config(path=cfg_path)
        return {
            "planner": cfg.public(),
            "providers": list(planner.PROVIDERS),
            "default_folder": planner.read_setting("default_folder", None, cfg_path),
            "env_overrides": cfg.source in ("env", "deepseek-env"),
        }

    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return settings_view()

    @app.post("/api/settings")
    def post_settings(body: SettingsBody) -> dict[str, Any]:
        stored = planner.load_config(env={}, path=cfg_path)      # the file only, not the environment
        merged = PlannerConfig(
            provider=body.provider if body.provider is not None else stored.provider,
            base_url=(body.base_url if body.base_url is not None else stored.base_url).strip(),
            model=(body.model if body.model is not None else stored.model).strip(),
            api_key=stored.api_key if body.api_key is None else body.api_key.strip(),
        )
        problems = planner.check_config(merged)
        if problems:
            raise _bad(400, "; ".join(problems))
        update: dict[str, Any] = {"provider": merged.provider, "base_url": merged.base_url, "model": merged.model}
        if body.api_key is not None:
            update["api_key"] = merged.api_key
        planner.save_planner_config(update, cfg_path)
        if body.default_folder is not None:
            value = ""
            if body.default_folder.strip():
                value = str(under_home(body.default_folder, allow_home=True))
            planner.write_setting("default_folder", value, cfg_path)
        return settings_view()

    # ---- teams --------------------------------------------------------------- #

    @app.post("/api/preview")
    def preview(body: PreviewBody) -> dict[str, Any]:
        arch, cfg = make_arch(body.use_planner)
        target: Path | None = None
        if body.target:
            target = under_home(body.target, allow_home=False)
        blueprint_id = body.blueprint_id
        route_source = None
        if blueprint_id is None:
            if not body.brief.strip():
                raise _bad(400, "describe what you want, or pick a team")
            route, route_source = planner.route_with_fallback(arch, body.brief)
            if route.needs_disambiguation:
                return {"needs_choice": True, "candidates": route.ambiguous,
                        "question": route.disambiguation, "route_source": route_source}
            blueprint_id = route.blueprint_id
        if blueprint_id not in arch.blueprints:
            raise _bad(400, "unknown team")
        prefix = body.prefix or blueprint_id.split("_")[0][:4]
        try:
            roster, source, note = planner.assemble_with_fallback(
                arch, body.brief or "your task", blueprint_id, body.answers, body.guardrail, prefix)
        except ValueError as exc:
            raise _bad(400, str(exc)) from None
        roster["flow_guard"] = body.flow_mode
        return _preview_payload(arch, roster, source, note, target, body.fence_reads,
                                body.brief, body.answers, route_source)

    def _preview_payload(arch: Architect, roster: dict, source: str, note: str | None, target: Path | None,
                         fence_reads: bool, brief: str, answers: dict[str, str], route_source: str | None) -> dict:
        errors = arch.validate(roster)
        files = scaffold(roster, target or Path("/nonexistent/preview"), dry_run=True,
                         primitives=arch.primitives, fence_reads=fence_reads)
        orchestrator = next((r["id"] for r in roster["roles"] if r.get("primitive") == "orchestrator"), None)
        return {
            "needs_choice": False,
            "blueprint_id": roster.get("blueprint_id"),
            "roster": roster, "source": source, "note": note, "route_source": route_source,
            "errors": errors,
            "roles": [describe_role(r, arch) for r in roster["roles"]],
            "guardrail": roster.get("guardrail"),
            "files": [line.replace("[dry-run] ", "", 1) for line in files],
            "orchestrator": orchestrator,
            "flow": _flow_view(arch, roster),
            "starter_task": arch.starter_task(roster.get("blueprint_id"), brief or "[describe your topic here]", answers),
        }

    @app.post("/api/create")
    def create(body: CreateBody) -> Any:
        arch = Architect(BLUEPRINTS_PATH)
        target = under_home(body.target, allow_home=False)
        roster = body.roster
        errors = arch.validate(roster)
        if not errors:
            errors = check_role_ids(roster)
        if not errors and roster.get("blueprint_id") not in arch.blueprints:
            errors = ["unknown blueprint"]
        if errors:
            raise _bad(400, "This team is not allowed: " + "; ".join(errors))
        existing = [r["id"] for r in roster["roles"]
                    if (target / ".claude" / "agents" / f"{r['id']}.md").exists()]
        if existing and not body.overwrite:
            return JSONResponse({"detail": "some of these agent files already exist here",
                                 "exists": existing}, status_code=409)
        try:
            actions = scaffold(roster, target, primitives=arch.primitives, fence_reads=body.fence_reads)
        except OSError as exc:
            raise _bad(500, f"could not write the team ({exc.strerror})") from None
        orchestrator = next(r["id"] for r in roster["roles"] if r["primitive"] == "orchestrator")
        return {"actions": actions, "orchestrator": orchestrator, "folder": str(target),
                "run_command": f"claude --agent {orchestrator}",
                "audit": _audit_json(audit(target))}

    @app.post("/api/destroy")
    def destroy_team(body: DestroyBody) -> dict[str, Any]:
        problem = check_prefix(body.prefix)
        if problem:
            raise _bad(400, problem)
        target = under_home(body.target, allow_home=False)
        team = (_read_manifest(target).get("teams") or {}).get(body.prefix)
        if not team:
            raise _bad(404, "this tool did not create a team with that name in that folder")
        return {"actions": destroy(body.prefix, target, dry_run=body.dry_run), "dry_run": body.dry_run}

    @app.post("/api/audit")
    def audit_folder(body: AuditBody) -> dict[str, Any]:
        target = under_home(body.target, allow_home=True)
        result = audit(target)
        payload = _audit_json(result)
        payload["report"] = render_report(result, approvals=approval_log.read(target, limit=500))
        payload["text"] = format_text(result)
        return payload

    # ---- running a team: in a terminal, followed on the board ---------------- #

    @app.get("/api/run-mode")
    def run_mode() -> dict[str, Any]:
        """Whether the page can open a terminal here (Linux), or only gives the command to copy."""
        term = find_terminal()
        return {"terminal": {"auto_open": bool(term), "name": term[0] if term else None}}

    @app.post("/api/terminal")
    def open_terminal(body: TerminalBody) -> dict[str, Any]:
        folder = under_home(body.folder, allow_home=False)
        exe = find_claude()
        if not exe:
            raise _bad(400, "`claude` was not found on your PATH")
        try:
            return opener(folder, body.team, exe, body.job)
        except TerminalError as exc:
            raise _bad(400, str(exc)) from None

    # ---- guard mode ---------------------------------------------------------- #

    def guard_view(folder: Path) -> dict[str, Any]:
        s = guard_mode.status(folder, home=guard_home, port=board_port)
        return {**s, "text": guard_mode.format_status(s)}

    @app.get("/api/guard")
    def guard_status(path: str) -> dict[str, Any]:
        return guard_view(under_home(path, allow_home=False))

    @app.post("/api/guard/install")
    def guard_install(body: GuardInstallBody) -> dict[str, Any]:
        folder = under_home(body.folder, allow_home=False)
        try:
            lines = guard_mode.install(folder, body.scope, body.mcp, [p.strip() for p in body.sensitive if p.strip()],
                                       body.flow, home=guard_home)
        except guard_mode.GuardError as exc:
            raise _bad(400, str(exc)) from None
        return {"actions": lines, **guard_view(folder)}

    @app.post("/api/guard/remove")
    def guard_remove(body: GuardRemoveBody) -> dict[str, Any]:
        folder = under_home(body.folder, allow_home=False)
        lines = guard_mode.remove(folder, "user" if body.scope == "user" else None, home=guard_home)
        return {"actions": lines, **guard_view(folder)}

    @app.get("/api/sites")
    def get_sites() -> dict[str, Any]:
        return {**guard_mode.read_sites(sites_file), "path": str(sites_file or guard_mode.sites_path())}

    @app.post("/api/sites")
    def post_sites(body: SitesBody) -> dict[str, Any]:
        try:
            saved = guard_mode.write_sites(body.allow, body.block, sites_file)
        except guard_mode.GuardError as exc:
            raise _bad(400, str(exc)) from None
        return {**saved, "path": str(sites_file or guard_mode.sites_path())}

    @app.get("/api/approvals")
    def approvals(path: str = "", limit: int = 50) -> dict[str, Any]:
        folder = under_home(path, allow_home=False) if path else None
        entries = approval_log.read(folder, limit=max(1, min(limit, 500)), path=approvals_file)
        return {"entries": entries[::-1], "log": str(approvals_file or approval_log.log_path())}

    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
    return app
