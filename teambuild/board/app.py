"""FastAPI: serves frontend/dist/ + a live event feed on 127.0.0.1:8123.

Endpoints: GET /agents (each team role and its boundaries), GET /agents/{id}/activity (its
latest tool calls), GET /events (Server-Sent Events), POST /hook (hook events, from
.claude/hooks/emit.py and the guards), and the approval routes in gate.py. Backed by
teambuild.board.hook_listener's in-memory Board. The board shows what the team does and answers
approvals; it cannot give an agent work. Every request passes security.py's guard.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from teambuild.board import policy
from teambuild.board.gate import gate, router as gate_router
from teambuild.board.hook_listener import board, router as hook_router
from teambuild.board.security import make_guard, new_token
from teambuild import approval_log

FRONTEND_DIST = Path(__file__).resolve().parent / "static"    # built from board/frontend (npm run build)
KEEPALIVE_SECONDS = 15
TOKEN_PLACEHOLDER = "__BOARD_TOKEN__"
TOKEN = new_token()          # per launch; only ever written into the page below
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(hook_router)
app.include_router(gate_router)
gate.publish = board._publish
board.on_new_job = gate.new_job
LOG_PATH: Path | None = None          # None: the user's approval log (teambuild/approval_log.py); tests point it elsewhere
recent_decisions: deque[dict] = deque(maxlen=50)


def record_decision(entry: dict) -> None:
    """Every approval outcome: redacted, appended to the approval log, shown on the board."""
    entry = {**entry, "detail": policy.redact(entry.get("detail") or "")}
    approval_log.append(entry, LOG_PATH)
    recent_decisions.append(entry)
    board._publish({"kind": "decision", "payload": entry})


gate.record = record_decision
_guard = make_guard(TOKEN)


@app.middleware("http")
async def _guard_and_headers(request: Request, call_next):
    response = await _guard(request, call_next)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


@app.get("/agents")
async def get_agents() -> list[dict]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "state": a.state,
            "current_task_id": a.current_task_id,
            "last_heartbeat_at": a.last_heartbeat_at,
            "last_ended_at": a.last_ended_at,
            "boundaries": a.boundaries,
        }
        for a in board.agents.values()
    ]


@app.get("/approvals")
async def get_approvals() -> dict:
    """The decisions made since the board started, newest last (the full history is in the log)."""
    return {"decisions": list(recent_decisions), "log": str(LOG_PATH or approval_log.log_path())}


@app.get("/run")
async def get_run() -> dict:
    """The current job: what was asked, each member's final report, and the team's final answer."""
    return board.run


@app.get("/agents/{agent_id}/activity")
async def get_agent_activity(agent_id: str) -> dict:
    a = board.agents.get(agent_id)
    if a is None or not a.activity:
        return {"available": False, "updatedAt": None, "items": []}
    return {"available": True, "updatedAt": a.activity[-1]["ts"], "items": a.activity}


@app.get("/events")
async def events() -> StreamingResponse:
    async def stream():
        q = board.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=KEEPALIVE_SECONDS)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            board.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    page = FRONTEND_DIST / "index.html"
    if not page.exists():
        return HTMLResponse("The board page is not built yet: cd board/frontend && npm install && npm run build",
                            status_code=503)
    html = page.read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, TOKEN)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
