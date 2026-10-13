"""Request guard for the board. Same rules as the Team Builder page (teambuild/ui/security.py).

The board listens on 127.0.0.1 only, but any web page the user has open can send requests to
127.0.0.1, and DNS rebinding can make a hostile hostname resolve to it. Since the board can now
answer approvals, every request must pass:

  * the Host header names this computer (127.0.0.1 or localhost)     (DNS rebinding)
  * on anything that changes state (POST): an Origin header, when the browser sends one, is the
    board's own, and Sec-Fetch-Site does not say cross-site or same-site    (cross-site requests).
    Reads stay open to navigation, so the Team Builder page's Board link (another port, which
    browsers call "same-site") still opens the board; another site cannot read the responses.
  * an approval decision (POST /gate/<id>/allow|deny) carries the per-launch token, which is written
    into the board page this server serves and nowhere else. No CORS headers are ever sent, so
    another origin cannot read it.

Hook senders (emit.py, the guards, board_gate.py) are local processes with no Origin header: they
pass the first three rules and never need the token.
"""
from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

TOKEN_HEADER = "x-board-token"
SAFE_METHODS = {"GET", "HEAD"}
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost"}
CROSS_SITE_VALUES = {"cross-site", "same-site"}
MAX_BODY_BYTES = 1_000_000


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _is_decision(request: Request) -> bool:
    parts = request.url.path.strip("/").split("/")
    return request.method == "POST" and len(parts) == 3 and parts[0] == "gate"


def make_guard(token: str):
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if hostname not in LOCAL_HOSTNAMES:
            return _deny(403, "unexpected Host header")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin.lower() != f"http://{host}":
                return _deny(403, "cross-origin request refused")
            if request.headers.get("sec-fetch-site", "").lower() in CROSS_SITE_VALUES:
                return _deny(403, "cross-site request refused")
        try:
            too_big = int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES
        except ValueError:
            too_big = True
        if too_big:
            return _deny(413, "request too large")
        if _is_decision(request):
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return _deny(403, "missing or wrong token")
        return await call_next(request)

    return guard
