"""Approvals on the board: POST /gate (from .claude/hooks/board_gate.py) waits for Allow or Deny.

The hook POSTs one outward call (a web search or fetch, a shell command, in guard mode an MCP tool). Before asking a
person, the board applies policy.py, in this order:

  1. the site is on the block list                  -> deny, no card
  2. the call has leak warnings                     -> always a card, the warnings shown in red
  3. the site is allowed (for this job, or listed)  -> allow, no card
     web searches allowed for this job               -> allow, no card
  4. anything else                                  -> a card

A card holds the request open until a person clicks, or until WAIT_SECONDS pass (then: deny). The
answer is the hook's whole reply: {"decision": "allow"|"deny"}. "Allow for this job" on a card
allows that call, remembers the site (or web searches) until the next job starts, and settles any
other waiting card it now covers that has no warnings. Every outcome is passed to `record`
(the approval log).

Who may do what (the request guard in security.py applies to every route):
  * POST /gate                       any local process (the hook sends no Origin); web pages refused
  * GET  /gate                       the pending list, for the board page
  * POST /gate/{id}/allow|deny|allow-job   only the board page: it needs the per-launch token that is
                                     written into the page this server serves and nowhere else

The same call can arrive twice: a team's gate and guard mode (or guard mode installed both in a project
and as the plugin) each send it. While the first is waiting, the second waits on the same card; once it
is decided, a copy arriving within DUPLICATE_SECONDS gets the same answer. One card, one log entry.

Guard mode may send its own warnings with a call (for example "this session has read files marked
sensitive"): they are added to the board's, so they can only make a call harder to allow, never easier.

Pending requests live in memory only. A restart drops them, and their hooks then get no answer:
the hook treats a dropped connection as deny.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from teambuild import policy_file
from teambuild.board import policy

router = APIRouter()

WAIT_SECONDS = 560            # below board_gate.py's own 570 s wait, so the hook always gets an answer
MAX_PENDING = 50
DETAIL_MAX = 2000
GATED_TOOLS = {"WebFetch", "WebSearch", "Bash"}
MCP_TOOL_RE = re.compile(r"mcp__[A-Za-z0-9_.-]{1,120}")
ROLE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}")     # team roles, "main", any subagent's name
DUPLICATE_SECONDS = 5
EXTRA_WARNINGS_MAX = 5
DECISIONS = ("allow", "deny", "allow-job")


@dataclass
class Pending:
    id: str
    agent: str
    tool: str
    detail: str
    created: float
    site: str | None = None
    warnings: list[str] = field(default_factory=list)
    cwd: str | None = None
    session: str | None = None
    policy: str | None = None                 # the repository policy in force (path and hash), for the log
    repo_allow: set[str] = field(default_factory=set)
    repo_block: set[str] = field(default_factory=set)
    decision: str | None = None
    decided_event: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict:
        return {"id": self.id, "agent": self.agent, "tool": self.tool, "detail": self.detail,
                "created": self.created, "site": self.site, "warnings": self.warnings,
                "job_allowable": self.tool in ("WebFetch", "WebSearch") and (self.site or self.tool == "WebSearch")
                                 and not self.warnings}


class Gate:
    def __init__(self) -> None:
        self.pending: dict[str, Pending] = {}
        self.publish = lambda event: None        # set by the app: pushes an SSE frame to the page
        self.record = lambda entry: None         # set by the app: the approval log
        self.sites_path: Path = policy.sites_path()
        self.job_sites: set[str] = set()         # "allow for this job": sites
        self.job_searches = False                # "allow for this job": web searches
        self.recent: dict[tuple, tuple[str, float]] = {}   # same call sent twice -> (decision, when)

    def new_job(self) -> None:
        """A new job started: forget what was allowed for the last one."""
        self.job_sites.clear()
        self.job_searches = False
        self.recent.clear()

    def snapshot(self) -> list[dict]:
        return [p.public() for p in sorted(self.pending.values(), key=lambda p: p.created)]

    def _changed(self) -> None:
        self.publish({"kind": "gate", "payload": {"pending": self.snapshot()}})

    def _log(self, p: Pending, decision: str, how: str) -> None:
        self.record({"ts": time.time(), "decision": decision, "how": how, "agent": p.agent, "tool": p.tool,
                     "detail": p.detail, "site": p.site, "warnings": p.warnings, "cwd": p.cwd, "policy": p.policy})

    def _allowed_without_asking(self, p: Pending) -> str | None:
        """Why this call may run without a card, or None."""
        if p.warnings:
            return None
        if p.tool == "WebSearch" and self.job_searches:
            return "web searches allowed for this job"
        if p.site and any(p.site == s or p.site.endswith("." + s) for s in self.job_sites):
            return "site allowed for this job"
        listed = policy.listed(p.site, self.sites_path, p.repo_allow, p.repo_block)
        if listed == "allow":
            return "site on your allow list" if policy.listed(p.site, self.sites_path) == "allow" else \
                "site on the repository's allow list"
        return None

    async def ask(self, agent: str, tool: str, detail: str, cwd: str | None = None,
                  wait: float = WAIT_SECONDS, session: str | None = None, extra: list[str] | None = None) -> str:
        key = (session, agent, tool, detail)
        now = time.time()
        self.recent = {k: v for k, v in self.recent.items() if now - v[1] < DUPLICATE_SECONDS}
        twin = next((q for q in self.pending.values() if q.decision is None and (q.session, q.agent, q.tool, q.detail) == key), None)
        if twin is not None:
            try:
                await asyncio.wait_for(twin.decided_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            return twin.decision or "deny"
        if key in self.recent:
            return self.recent[key][0]
        decision = await self._ask(agent, tool, detail, cwd, wait, session, extra or [])
        self.recent[key] = (decision, time.time())
        return decision

    async def _ask(self, agent: str, tool: str, detail: str, cwd: str | None, wait: float,
                   session: str | None, extra: list[str]) -> str:
        site, warnings = policy.inspect(tool, detail)
        p = Pending(id=secrets.token_hex(8), agent=agent, tool=tool, detail=detail, created=time.time(),
                    site=site, warnings=warnings + [w for w in extra if w not in warnings], cwd=cwd, session=session)
        repo = policy_file.for_folder(cwd)
        if repo is not None:
            p.policy = repo.label()
            if repo.valid:
                p.repo_allow, p.repo_block = repo.sites("allow"), repo.sites("block")
            else:
                p.warnings.append(f"this repository's {policy_file.NAME} is invalid, so nothing is allowed "
                                  f"automatically ({'; '.join(repo.errors)[:200]})")
        if policy.listed(site, self.sites_path, p.repo_allow, p.repo_block) == "block":
            mine = policy.listed(site, self.sites_path) == "block"
            self._log(p, "deny", "site on your block list" if mine else "site on the repository's block list")
            return "deny"
        why = self._allowed_without_asking(p)
        if why:
            self._log(p, "allow", why)
            return "allow"
        if len(self.pending) >= MAX_PENDING:
            self._log(p, "deny", "too many requests waiting")
            return "deny"
        self.pending[p.id] = p
        self._changed()
        try:
            await asyncio.wait_for(p.decided_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        finally:
            self.pending.pop(p.id, None)
            self._changed()
        if p.decision is None:
            self._log(p, "deny", "no answer in time")
            return "deny"
        return p.decision

    def decide(self, request_id: str, decision: str) -> bool:
        p = self.pending.get(request_id)
        if p is None or p.decision is not None or decision not in DECISIONS:
            return False
        if decision == "allow-job":
            if not p.public()["job_allowable"]:
                return False
            if p.tool == "WebSearch":
                self.job_searches = True
            else:
                self.job_sites.add(p.site)
            how = "clicked Allow for this job"
            decision = "allow"
        else:
            how = f"clicked {decision.capitalize()}"
        self._settle(p, decision, how)
        for other in list(self.pending.values()):          # cards the new allowance now covers
            if other.decision is None and (why := self._allowed_without_asking(other)):
                self._settle(other, "allow", why)
        return True

    def _settle(self, p: Pending, decision: str, how: str) -> None:
        p.decision = decision
        self._log(p, decision, how)
        p.decided_event.set()


gate = Gate()


@router.post("/gate")
async def ask(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        return {"decision": "deny"}
    if not isinstance(body, dict):
        return {"decision": "deny"}
    agent, tool, detail = body.get("agent_type"), body.get("tool_name"), body.get("detail")
    if not (isinstance(agent, str) and ROLE_RE.fullmatch(agent) and isinstance(tool, str)
            and (tool in GATED_TOOLS or MCP_TOOL_RE.fullmatch(tool))):
        return {"decision": "deny"}
    detail = detail[:DETAIL_MAX] if isinstance(detail, str) else ""
    cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else None
    session = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    raw = body.get("warnings") if isinstance(body.get("warnings"), list) else []
    extra = [w[:300] for w in raw[:EXTRA_WARNINGS_MAX] if isinstance(w, str) and w.strip()]
    return {"decision": await gate.ask(agent, tool, detail, cwd, session=session, extra=extra)}


@router.get("/gate")
async def pending() -> dict:
    return {"pending": gate.snapshot()}


@router.post("/gate/{request_id}/{decision}")
async def decide(request_id: str, decision: str) -> dict:
    if decision not in DECISIONS:
        raise HTTPException(404, "unknown decision")
    if not gate.decide(request_id, decision):
        raise HTTPException(404, "that request is no longer waiting")
    return {"ok": True}
