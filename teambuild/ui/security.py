"""Request guard and path rules for the local web interface.

The server listens on 127.0.0.1 only, but "local" is not enough: any web page the user has open
can send requests to 127.0.0.1, and DNS rebinding can make a hostile hostname resolve to it. So:

  * the Host header must be one of the server's own addresses        (DNS rebinding)
  * an Origin header, when the browser sends one, must be the server's own   (cross-site requests)
  * Sec-Fetch-Site, when present, must not say cross-site
  * every request that changes anything needs the per-launch token in a header. The token is
    written into the page this server serves and nowhere else; no CORS headers are ever sent, so
    another origin cannot read it.
  * paths named by the user must resolve (symlinks and `..` included) to somewhere under the home
    folder.
"""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path
from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse

TOKEN_HEADER = "x-team-build-token"
SAFE_METHODS = {"GET", "HEAD"}
MAX_BODY_BYTES = 1_000_000
CROSS_SITE_VALUES = {"cross-site", "same-site"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def make_guard(token: str, allowed_hosts: Iterable[str]):
    """An HTTP middleware function enforcing the rules above. `allowed_hosts` are full
    Host-header values, e.g. {"127.0.0.1:8124", "localhost:8124"}."""
    hosts = {h.lower() for h in allowed_hosts}
    origins = {f"http://{h}" for h in hosts}

    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        if host not in hosts:
            return _deny(403, "unexpected Host header")
        origin = request.headers.get("origin")
        if origin is not None and origin.lower() not in origins:
            return _deny(403, "cross-origin request refused")
        if request.headers.get("sec-fetch-site", "").lower() in CROSS_SITE_VALUES:
            return _deny(403, "cross-site request refused")
        try:
            too_big = int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES
        except ValueError:
            too_big = True
        if too_big:
            return _deny(413, "request too large")
        if request.method not in SAFE_METHODS:
            sent = request.headers.get(TOKEN_HEADER, "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                return _deny(403, "missing or wrong token")
        return await call_next(request)

    return guard


# --------------------------------------------------------------------------- #
# Paths                                                                        #
# --------------------------------------------------------------------------- #

class PathError(ValueError):
    """The path is not acceptable. The message is safe to show the user."""


def resolve_under_home(
    raw: object, home: Path, *, must_exist: bool = True, allow_home: bool = False,
    label: str = "your home folder",
) -> Path:
    """Resolve a user-supplied path and insist it lands under `home`.

    `~`, relative paths (taken relative to home), `..` and symlinks are all resolved first, so a
    symlink inside home that points outside it is refused. `allow_home` says whether the home
    folder itself is acceptable (audits: yes; anything that writes or runs: no).
    """
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PathError("choose a folder")
    home = home.resolve()
    p = Path(raw.strip()).expanduser()
    if not p.is_absolute():
        p = home / p
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        raise PathError("that path could not be resolved") from None
    if resolved == home:
        if not allow_home:
            raise PathError(f"pick a project folder inside {label}, not {label} itself")
    elif not resolved.is_relative_to(home):
        raise PathError(f"that folder is outside {label}")
    if must_exist and not resolved.is_dir():
        raise PathError("that folder does not exist")
    return resolved
