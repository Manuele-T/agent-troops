"""What the board decides on its own before asking a person: site lists and leak warnings.

Site lists live in ~/.config/agent-troops/sites.json (read on every request, so edits apply at once):

    {"allow": ["docs.python.org", "notion.com"], "block": ["pastebin.com"]}

A name also covers its subdomains ("notion.com" covers "www.notion.com"). A blocked site is denied
without a card; an allowed site, or one allowed "for this job" on the board, runs without a card.

Leak warnings: an outward call is checked for signs that it carries data out — something shaped like a
password, key or token; a long encoded-looking string; a very long piece of a site name (data can leave
through the DNS lookup itself, as in Mozilla 0DIN's 2026 proof of concept against Claude Code); a raw IP
address; plain HTTP; credentials in the URL. These are heuristics: they can miss things and can flag
harmless addresses. A flagged call is never allowed automatically: it always gets a card, with the
reasons shown, even for a site on the allow list.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key"),
    (re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}"), "an API key (sk-...)"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"), "a GitHub token"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "a Google API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a login token (JWT)"),
    (re.compile(r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)\s*[=:]"),
     "a password or key name with a value"),
]
TOKEN_RE = re.compile(r"[A-Za-z0-9+/_=-]{32,}")
LONG_LABEL = 40
LONG_URL = 400


def _encoded_looking(text: str) -> bool:
    for m in TOKEN_RE.finditer(text):
        t = m.group(0)
        mixed = any(c.isupper() for c in t) and any(c.islower() for c in t) and any(c.isdigit() for c in t)
        if mixed or len(t) >= 64:
            return True
    return False


def secret_warnings(text: str) -> list[str]:
    found = [f"looks like it contains {what}" for rx, what in SECRET_PATTERNS if rx.search(text)]
    if _encoded_looking(text):
        found.append("contains a long encoded-looking string (data can be smuggled out this way)")
    return found


def site_of(url: str) -> str | None:
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def url_warnings(url: str) -> list[str]:
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return ["the address could not be read"]
    out: list[str] = []
    if parts.scheme not in ("https", "http"):
        out.append("not a web address (http/https)")
    elif parts.scheme == "http":
        out.append("plain HTTP, not HTTPS")
    if parts.username or parts.password:
        out.append("has a user name or password inside the address")
    try:
        ipaddress.ip_address(host)
        out.append("goes to a raw IP address, not a site name")
    except ValueError:
        if any(len(label) > LONG_LABEL for label in host.split(".")):
            out.append("a part of the site name is unusually long (data can leave through the DNS lookup)")
    if len(url) > LONG_URL:
        out.append("very long address")
    out += secret_warnings(unquote(url))
    return out


def inspect(tool: str, detail: str) -> tuple[str | None, list[str]]:
    """(site, warnings) for one outward call."""
    if tool == "WebFetch":
        return site_of(detail), url_warnings(detail)
    return None, secret_warnings(detail)


def sites_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "agent-troops" / "sites.json"


def _covers(site: str, names: set[str]) -> bool:
    return any(site == n or site.endswith("." + n) for n in names)


def load_lists(path: Path) -> tuple[set[str], set[str]]:
    """(allow, block). A missing or unreadable file means no lists."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), set()
    if not isinstance(data, dict):
        return set(), set()

    def names(key: str) -> set[str]:
        raw = data.get(key)
        return {str(n).strip().lower().rstrip(".") for n in raw if isinstance(n, str) and n.strip()} if isinstance(raw, list) else set()

    return names("allow"), names("block")


def listed(site: str | None, path: Path, extra_allow: set[str] | frozenset = frozenset(),
           extra_block: set[str] | frozenset = frozenset()) -> str | None:
    """"block" | "allow" | None, from the person's lists plus a repository policy's. Block wins when a site
    matches both, whichever list it comes from: nobody can allow what the repository blocks."""
    if not site:
        return None
    allow, block = load_lists(path)
    allow, block = allow | set(extra_allow), block | set(extra_block)
    if _covers(site, block):
        return "block"
    if _covers(site, allow):
        return "allow"
    return None


def redact(text: str) -> str:
    """The text with anything shaped like a secret, or a long encoded string, replaced. For the log:
    the card shows the call as it is, so the person can judge it, but the log never keeps a secret."""
    for rx, what in SECRET_PATTERNS:
        text = rx.sub(f"[redacted: {what}]", text)
    def token(m: re.Match) -> str:
        t = m.group(0)
        mixed = any(c.isupper() for c in t) and any(c.islower() for c in t) and any(c.isdigit() for c in t)
        return f"[redacted: {len(t)} characters]" if mixed or len(t) >= 64 else t
    return TOKEN_RE.sub(token, text)
