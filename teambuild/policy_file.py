"""The repository's shared policy: `.agent-troops.json`, committed at the root of a project.

    {
      "version": 1,
      "sites": {"allow": ["docs.python.org"], "block": ["pastebin.com"]},
      "sensitive": ["config/*", "*.pem", "customers/*"],
      "after_sensitive": "block",
      "require_guard": true,
      "forbid": {"remote_mcp": true, "unpinned_mcp": true, "unknown_hooks": true}
    }

Every key is optional except "version". Who reads what:

  * the board (teambuild/board/gate.py), for every call made inside the repository: the repo's site lists are
    added to the person's own (~/.config/agent-troops/sites.json). A block from either list wins, so nobody can
    allow a site the repository blocks. An unreadable or invalid file never allows anything: every call from
    that repository gets a card with a red warning.
  * guard mode's hook (templates/board_gate.py, which reads the file itself: it has to run without this
    package): "sensitive" patterns are added to its own, and "block" wins over "ask".
  * the audit (setup_audit.py): an invalid file fails; "require_guard" fails unless guard mode is installed in
    the shared settings (.claude/settings.json, the only place a CI checkout can see); "forbid" turns the named
    findings into failures (remote MCP servers, unpinned MCP packages, hooks that are not agent-troops' own).

Each approval log entry records the policy file and a hash of its contents, so the evidence shows which policy
was in force. Unknown keys are errors, so a typo cannot quietly switch a rule off. Stdlib only.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

NAME = ".agent-troops.json"
KEYS = {"version", "sites", "sensitive", "after_sensitive", "require_guard", "forbid"}
FORBID_KEYS = {"remote_mcp", "unpinned_mcp", "unknown_hooks"}
SITE_RE = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)*")
PATTERN_RE = re.compile(r"[^\x00-\x1f]{1,200}")
MAX_ITEMS = 500
MAX_BYTES = 256 * 1024
WALK_UP = 40

TEMPLATE = {
    "version": 1,
    "sites": {"allow": [], "block": []},
    "sensitive": [],
    "after_sensitive": "block",
    "require_guard": False,
    "forbid": {"remote_mcp": False, "unpinned_mcp": False, "unknown_hooks": False},
}


def find(start: str | Path | None) -> Path | None:
    """The policy file for a folder: in it or the nearest parent that has one."""
    if not start:
        return None
    try:
        here = Path(start).resolve()
    except (OSError, RuntimeError):
        return None
    for folder in [here, *here.parents][:WALK_UP]:
        candidate = folder / NAME
        if candidate.is_file():
            return candidate
    return None


def validate(data: object) -> list[str]:
    """Everything wrong with a parsed policy, in plain words. Empty list: valid."""
    if not isinstance(data, dict):
        return ["the file must hold a JSON object"]
    errors = [f"unknown key \"{k}\"" for k in sorted(set(data) - KEYS)]
    if data.get("version") != 1:
        errors.append("\"version\" must be 1")
    sites = data.get("sites", {})
    if not isinstance(sites, dict):
        errors.append("\"sites\" must be an object with \"allow\" and \"block\" lists")
    else:
        errors += [f"unknown key \"sites.{k}\"" for k in sorted(set(sites) - {"allow", "block"})]
        for key in ("allow", "block"):
            names = sites.get(key, [])
            if not isinstance(names, list) or len(names) > MAX_ITEMS:
                errors.append(f"\"sites.{key}\" must be a list of at most {MAX_ITEMS} site names")
                continue
            errors += [f"\"sites.{key}\": {n!r} is not a site name (like docs.python.org)"
                       for n in names if not (isinstance(n, str) and SITE_RE.fullmatch(n))]
    sensitive = data.get("sensitive", [])
    if not isinstance(sensitive, list) or len(sensitive) > MAX_ITEMS:
        errors.append(f"\"sensitive\" must be a list of at most {MAX_ITEMS} file patterns")
    else:
        errors += [f"\"sensitive\": {p!r} is not a file pattern" for p in sensitive
                   if not (isinstance(p, str) and PATTERN_RE.fullmatch(p) and p.strip())]
    if data.get("after_sensitive", "block") not in ("block", "ask"):
        errors.append("\"after_sensitive\" must be \"block\" or \"ask\"")
    if not isinstance(data.get("require_guard", False), bool):
        errors.append("\"require_guard\" must be true or false")
    forbid = data.get("forbid", {})
    if not isinstance(forbid, dict):
        errors.append("\"forbid\" must be an object")
    else:
        errors += [f"unknown key \"forbid.{k}\"" for k in sorted(set(forbid) - FORBID_KEYS)]
        errors += [f"\"forbid.{k}\" must be true or false" for k, v in forbid.items()
                   if k in FORBID_KEYS and not isinstance(v, bool)]
    return errors


class Policy:
    """A loaded policy file. `errors` non-empty means invalid: callers must not relax anything because of it."""

    def __init__(self, path: Path, data: dict, errors: list[str], digest: str) -> None:
        self.path, self.data, self.errors, self.digest = path, data, errors, digest

    @property
    def valid(self) -> bool:
        return not self.errors

    def sites(self, key: str) -> set[str]:
        return set(self.data.get("sites", {}).get(key, [])) if self.valid else set()

    @property
    def sensitive(self) -> list[str]:
        return list(self.data.get("sensitive", [])) if self.valid else []

    @property
    def after_sensitive(self) -> str:
        return self.data.get("after_sensitive", "block") if self.valid else "block"

    @property
    def require_guard(self) -> bool:
        return bool(self.data.get("require_guard")) if self.valid else False

    def forbids(self, key: str) -> bool:
        return bool(self.data.get("forbid", {}).get(key)) if self.valid else False

    def label(self) -> str:
        return f"{self.path} (sha256 {self.digest[:12]})"


def load(path: str | Path) -> Policy:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as e:
        return Policy(path, {}, [f"cannot be read ({e.strerror})"], "")
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > MAX_BYTES:
        return Policy(path, {}, [f"larger than {MAX_BYTES // 1024} KB"], digest)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return Policy(path, {}, [f"not valid JSON ({e})"], digest)
    errors = validate(data)
    return Policy(path, data if not errors else {}, errors, digest)


def for_folder(start: str | Path | None) -> Policy | None:
    path = find(start)
    return load(path) if path else None


def describe(p: Policy) -> str:
    if not p.valid:
        return f"repository policy {p.path} is INVALID: {'; '.join(p.errors)}"
    parts = []
    if p.sites("allow") or p.sites("block"):
        parts.append(f"sites: {len(p.sites('allow'))} allowed, {len(p.sites('block'))} blocked")
    if p.sensitive:
        parts.append(f"sensitive: {', '.join(p.sensitive)} (then {p.after_sensitive})")
    if p.require_guard:
        parts.append("guard mode required")
    forbidden = sorted(k for k in FORBID_KEYS if p.forbids(k))
    if forbidden:
        parts.append(f"forbidden: {', '.join(forbidden)}")
    return f"repository policy {p.label()}: " + ("; ".join(parts) or "no rules set")


def write_template(folder: str | Path) -> Path:
    path = Path(folder) / NAME
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    path.write_text(json.dumps(TEMPLATE, indent=2) + "\n", encoding="utf-8")
    return path
