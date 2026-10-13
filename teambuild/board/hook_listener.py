"""POST /hook -> map Claude Code hook payload -> board state + SSE (emit.py is the sender).

Which agent made a call: current Claude Code hooks (see the hooks reference at
code.claude.com/docs/en/hooks, "Common input fields") include `agent_id` and `agent_type`
directly on every hook payload fired inside a subagent — `agent_id` is unique per subagent
invocation, `agent_type` is the subagent's own name. For a team-build-scaffolded role,
`agent_type` IS the role id (e.g. "rs-analyst"), since that's what we write as the agent
.md's `name:` frontmatter (teambuild/templates/agent_md.py). That field
turns out to be reliable enough on its own; no Task-call correlation or transcript_path
parsing needed. Payloads with no agent_type come from a plain session (guard mode: `team-build guard
install`, or the plugin): they become one robot, "main" (shown as "main session"); its prompt is the job
and its last message the answer. When emit.py is installed twice (a team and guard mode), each event
arrives twice; a copy of an event seen in the last few seconds is dropped.

`claude --agent <orchestrator>` runs a role as the MAIN session. Its payloads carry
`agent_type` but no `agent_id` (verified live), so a synthetic `main:<agent_type>` id is used
and the orchestrator gets a robot of its own. It never fires SubagentStop; `Stop` marks it done.

Beyond raw hook events the board understands `GuardBlocked`, POSTed by .claude/hooks/
bash_guard.py and read_guard.py when they refuse a call: the role turns red ("blocked") until
its next call and the refusal shows in its activity feed.

Results: `UserPromptSubmit` from the orchestrator starts a new run (the job you typed);
`SubagentStop` and `Stop` carry `last_assistant_message`, the member's final report and the team's
final answer, kept on the run so the board can show them as text (GET /run). The orchestrator's
delegation prompt (an Agent/Task call's `prompt`) is kept per member as what it was asked to do.

Two things seen live on Claude Code 2.1.286 (docs/shakeout-log.md): a member run in the background
reports back as a `<task-notification>` message, which also fires UserPromptSubmit. That is not a new
job; its <result> is filed as that member's report, matched through the delegation's tool_use_id.
And Claude Code runs small internal helpers under the main agent's own name, so a SubagentStop
naming the orchestrator is not a team report.

Each role also gets `boundaries` (tools, trifecta legs, gated, guarded) computed from the
`.claude/agents/` of the repo named by the payload's `cwd`, via teambuild.audit. That import is
optional: without teambuild installed the board still works, just without boundary badges.

Board state is in-memory only (one process, no persistence — restart clears it). That's
fine for a dev-time visualizer; nothing in `teambuild/` depends on it surviving a restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from collections import deque
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

ACTIVITY_MAX_ITEMS = 20
RAW_LOG_MAX = 200
TEXT_TRUNCATE = 300
RESULT_MAX_CHARS = 50_000     # one report or answer; longer text is cut, with a note
REPORTS_MAX = 50
DELEGATION_TOOLS = ("Agent", "Task")
MAIN = "main"                 # a session with no agent name (guard mode); board_gate.py uses the same name
DUPLICATE_SECONDS = 5
NOTIFICATION_RE = re.compile(r"^\s*<task-notification>")
TAG_RE = {name: re.compile(rf"<{name}>(.*?)</{name}>", re.S) for name in ("tool-use-id", "status", "result")}

TOOL_FAMILY = {
    "Read": "reading", "Grep": "reading", "Glob": "reading",
    "Write": "coding", "Edit": "coding",
    "Bash": "acting",
    "WebFetch": "researching", "WebSearch": "researching",
    "Task": "delegating",
}


def _truncate(text: str, n: int = TEXT_TRUNCATE) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _summarize_tool_input(tool_name: str | None, tool_input: dict | None) -> str | None:
    if not tool_input:
        return None
    if tool_name == "Bash" and "command" in tool_input:
        return _truncate(str(tool_input["command"]))
    if tool_name in ("Read", "Write", "Edit") and "file_path" in tool_input:
        return _truncate(str(tool_input["file_path"]))
    if tool_name == "Grep" and "pattern" in tool_input:
        return _truncate(str(tool_input["pattern"]))
    if tool_name == "WebSearch" and "query" in tool_input:
        return _truncate(str(tool_input["query"]))
    if tool_name == "WebFetch" and "url" in tool_input:
        return _truncate(str(tool_input["url"]))
    try:
        return _truncate(json.dumps(tool_input))
    except TypeError:
        return None


def _summarize_tool_response(tool_response: Any) -> str | None:
    if tool_response is None:
        return None
    if isinstance(tool_response, str):
        return _truncate(tool_response)
    try:
        return _truncate(json.dumps(tool_response))
    except TypeError:
        return _truncate(str(tool_response))


_BOUNDARIES_CACHE: dict[str, dict[str, dict]] = {}


def boundaries_for(cwd: str | None, role_id: str) -> dict | None:
    """Static facts about a role from its repo's agent files. None if unavailable."""
    if not cwd:
        return None
    if cwd not in _BOUNDARIES_CACHE:
        roles: dict[str, dict] = {}
        try:
            from teambuild.audit import audit

            for r in audit(cwd).roles:
                roles[r.id] = {
                    "tools": r.tools,
                    "legs": sorted(r.legs),
                    "gated": r.gated,
                    "guarded": r.guarded,
                    "verdict": r.verdict,
                }
        except Exception:
            roles = {}
        _BOUNDARIES_CACHE[cwd] = roles
    return _BOUNDARIES_CACHE[cwd].get(role_id)


@dataclass
class AgentState:
    id: str
    name: str
    state: str = "idle"  # idle | working | done | blocked | error
    current_task_id: str | None = None
    last_heartbeat_at: float | None = None
    last_ended_at: float | None = None
    activity: list[dict] = field(default_factory=list)  # ring buffer, newest last
    boundaries: dict | None = None


def _result_text(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > RESULT_MAX_CHARS:
        text = text[:RESULT_MAX_CHARS] + "\n\n[cut: the full text is longer than the board shows]"
    return text


def _new_run(job: str | None = None) -> dict:
    return {"job": job, "started": time.time(), "asked": {}, "reports": [], "final": None, "final_at": None}


class Board:
    def __init__(self) -> None:
        self.agents: dict[str, AgentState] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._next_event_id = 1
        self.raw_log: deque[dict] = deque(maxlen=RAW_LOG_MAX)
        self.run: dict = _new_run()
        self.delegations: dict[str, str] = {}      # Agent/Task tool_use_id -> the member it went to
        self.on_new_job = lambda: None             # set by the app: approvals "for this job" end here
        self._seen: dict[str, float] = {}          # recent event fingerprints: the same event sent twice

    def _publish_run(self) -> None:
        self._publish({"kind": "run", "payload": self.run})

    def _add_report(self, agent: str, text: str, now: float) -> None:
        if any(r["agent"] == agent and r["text"] == text for r in self.run["reports"]):
            return
        self.run["reports"] = (self.run["reports"] + [{"agent": agent, "text": text, "ts": now}])[-REPORTS_MAX:]
        self._publish_run()

    def _background_result(self, prompt: str) -> None:
        """A background member finished: file its <result> as that member's report."""
        tags = {k: (m.group(1).strip() if (m := r.search(prompt)) else "") for k, r in TAG_RE.items()}
        agent = self.delegations.get(tags["tool-use-id"])
        if agent and tags["result"] and tags["status"] in ("", "completed"):
            self._add_report(agent, _result_text(tags["result"]), time.time())

    def _agent(self, role_id: str) -> AgentState:
        a = self.agents.get(role_id)
        if a is None:
            a = AgentState(id=role_id, name="main session" if role_id == MAIN else role_id)
            self.agents[role_id] = a
        return a

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: dict) -> None:
        event.setdefault("id", self._next_event_id)
        self._next_event_id += 1
        for q in list(self._subscribers):
            q.put_nowait(event)

    def _duplicate(self, payload: dict) -> bool:
        now = time.time()
        self._seen = {k: t for k, t in self._seen.items() if now - t < DUPLICATE_SECONDS}
        fp = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        if fp in self._seen:
            return True
        self._seen[fp] = now
        return False

    def handle_hook(self, payload: dict) -> None:
        if self._duplicate(payload):
            return
        self.raw_log.append(payload)
        # Logged to see the real shape of events, the who-and-what first, since the line is cut short.
        # Kept permanently: cheap, and the only way to debug a misattributed event later.
        head = {k: payload.get(k) for k in ("hook_event_name", "agent_type", "agent_id", "tool_name", "tool_use_id")}
        print(f"[board] {json.dumps(head)} {json.dumps(payload)[:400]}", file=sys.stderr)

        kind = payload.get("hook_event_name")
        agent_type = payload.get("agent_type") or MAIN     # a plain session (guard mode)
        agent_id = payload.get("agent_id") or f"main:{agent_type}"  # --agent main session

        if kind == "UserPromptSubmit":
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and NOTIFICATION_RE.match(prompt):
                self._background_result(prompt)
            else:
                self.run = _new_run(_result_text(prompt))
                self.on_new_job()
                self._publish_run()
            return

        role = self._agent(agent_type)
        now = time.time()
        if role.boundaries is None:
            role.boundaries = boundaries_for(payload.get("cwd"), agent_type)

        if kind == "PreToolUse":
            tool_name = payload.get("tool_name")
            tool_input = payload.get("tool_input") or {}
            if tool_name in DELEGATION_TOOLS and isinstance(tool_input, dict):
                target = tool_input.get("subagent_type")
                if isinstance(target, str) and target:
                    self.run["asked"][target] = _result_text(tool_input.get("prompt"))
                    if isinstance(payload.get("tool_use_id"), str):
                        self.delegations[payload["tool_use_id"]] = target
                    self._publish_run()
            is_new_invocation = role.current_task_id != agent_id
            role.current_task_id = agent_id
            role.state = "working"
            role.last_heartbeat_at = now
            role.activity.append({
                "ts": now, "kind": "tool_call", "label": tool_name,
                "text": _summarize_tool_input(tool_name, payload.get("tool_input")),
            })
            del role.activity[:-ACTIVITY_MAX_ITEMS]
            self._publish({
                "kind": "claimed" if is_new_invocation else "heartbeat",
                "taskId": agent_id,
                "payload": {"assignee": agent_type},
            })
            self._publish({
                "kind": "activity",
                "payload": {"assignee": agent_type, "fine_state": "working",
                            "activity": TOOL_FAMILY.get(tool_name or "", "acting")},
            })

        elif kind == "PostToolUse":
            tool_name = payload.get("tool_name")
            role.last_heartbeat_at = now
            role.activity.append({
                "ts": now, "kind": "tool_result", "label": tool_name,
                "text": _summarize_tool_response(payload.get("tool_response")),
            })
            del role.activity[:-ACTIVITY_MAX_ITEMS]
            self._publish({
                "kind": "heartbeat",
                "taskId": role.current_task_id or agent_id,
                "payload": {"assignee": agent_type},
            })

        elif kind == "GuardBlocked":
            role.state = "blocked"
            role.last_heartbeat_at = now
            role.activity.append({
                "ts": now, "kind": "blocked", "label": payload.get("tool_name"),
                "text": _truncate(f"{payload.get('guard', 'guard')}: {payload.get('reason', 'refused')}"),
            })
            del role.activity[:-ACTIVITY_MAX_ITEMS]
            self._publish({
                "kind": "blocked",
                "taskId": role.current_task_id or agent_id,
                "payload": {"assignee": agent_type},
            })

        elif kind in ("SubagentStop", "Stop"):
            text = _result_text(payload.get("last_assistant_message"))
            if text and kind == "Stop":
                self.run["final"], self.run["final_at"] = text, now
                self._publish_run()
            elif text and agent_type != MAIN and not agent_type.endswith("-orchestrator"):   # not Claude Code's own helpers
                self._add_report(agent_type, text, now)
            role.state = "done"
            role.last_ended_at = now
            finished_task_id = role.current_task_id or agent_id
            role.current_task_id = None
            self._publish({
                "kind": "completed",
                "taskId": finished_task_id,
                "payload": {"assignee": agent_type},
            })


board = Board()


@router.post("/hook")
async def receive_hook(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {"ok": False}
    if isinstance(payload, dict):
        board.handle_hook(payload)
    return {"ok": True}
