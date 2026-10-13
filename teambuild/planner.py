"""Pluggable planner: one interface, `llm_call(system, user) -> str`, two providers.
(The third, "the user's own `claude`", was removed in Phase 11: it ran
`claude -p` on whatever login the user had, usually a subscription).

    none     (default) no model, no key: the keyless path in architect.py does everything.
    openai   any OpenAI-compatible chat endpoint: base URL + key + model. OpenRouter is the
             recommended one; DeepSeek and a local Ollama work too. DEEPSEEK_API_KEY alone
             still works (it selects DeepSeek's endpoint), so the old CLI setup is unchanged.

Neither provider touches Claude Code. Picking an Anthropic model through OpenRouter is OpenRouter's
service, under OpenRouter's terms. A stored provider of "claude" (from Phase 10) now reads as "none".

A planner only ever proposes; whatever it returns still goes through Architect.validate(), one
retry, then the deterministic offline roster. A weak or broken model costs quality, never safety.

Secrets: the only key this module handles is a NON-Anthropic planner key. It lives in the
environment or in ~/.config/agent-troops/config.json (mode 0600), never in a project folder,
never in a log or error message (every message is scrubbed of it), and is sent only to the
endpoint the user chose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from teambuild.architect import Architect, LlmCall, RouteResult, ValidationError, check_prefix

PROVIDERS = ("none", "openai")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUEST_TIMEOUT = 90


class PlannerError(Exception):
    """A provider failed. The message never contains the API key."""


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class PlannerConfig:
    provider: str = "none"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)   # repr=False: never printed by accident
    source: str = "default"                        # default | file | env | deepseek-env

    def public(self) -> dict[str, Any]:
        """What may be shown to the UI: everything except the key itself."""
        return {
            "provider": self.provider, "base_url": self.base_url, "model": self.model,
            "has_key": bool(self.api_key), "source": self.source,
        }


def config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("TEAMBUILD_CONFIG")
    if explicit:
        return Path(explicit)
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "agent-troops" / "config.json"


def read_config_file(path: Path | None = None) -> dict[str, Any]:
    path = path or config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(env: dict[str, str] | None = None, path: Path | None = None) -> PlannerConfig:
    """Environment beats the config file; with neither, DEEPSEEK_API_KEY alone picks DeepSeek."""
    env = os.environ if env is None else env
    file_planner = read_config_file(path or config_path(env)).get("planner")
    file_planner = file_planner if isinstance(file_planner, dict) else {}
    cfg = PlannerConfig()
    provider = None
    if file_planner:
        provider = file_planner.get("provider")
        cfg.base_url = str(file_planner.get("base_url") or "")
        cfg.model = str(file_planner.get("model") or "")
        cfg.api_key = str(file_planner.get("api_key") or "")
        cfg.source = "file"
    if env.get("TEAMBUILD_PLANNER"):
        provider = env["TEAMBUILD_PLANNER"].strip().lower()
        cfg.source = "env"
    for var, attr in (("TEAMBUILD_PLANNER_BASE_URL", "base_url"), ("TEAMBUILD_PLANNER_MODEL", "model"),
                      ("TEAMBUILD_PLANNER_API_KEY", "api_key")):
        if env.get(var):
            setattr(cfg, attr, env[var].strip())
            cfg.source = "env"
    if provider is None and env.get("DEEPSEEK_API_KEY"):
        provider, cfg.source = "openai", "deepseek-env"
        cfg.base_url = cfg.base_url or DEEPSEEK_BASE_URL
        cfg.model = cfg.model or DEEPSEEK_DEFAULT_MODEL
        cfg.api_key = cfg.api_key or env["DEEPSEEK_API_KEY"]
    cfg.provider = provider if provider in PROVIDERS else "none"
    return cfg


def check_config(cfg: PlannerConfig) -> list[str]:
    """Problems that would make the provider unusable or unsafe (empty list = fine)."""
    if cfg.provider not in PROVIDERS:
        return [f"provider must be one of {', '.join(PROVIDERS)}"]
    if cfg.provider != "openai":
        return []
    errors = []
    parsed = urlparse(cfg.base_url)
    if not parsed.hostname or parsed.scheme not in ("http", "https"):
        errors.append("base URL must start with https:// (http:// only for a local model)")
    elif parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        errors.append("plain http:// is only allowed for localhost; use https:// so the key is not sent in the clear")
    if parsed.username or parsed.password:
        errors.append("base URL must not contain a user name or password")
    if not cfg.model:
        errors.append("model name is required")
    local = parsed.hostname in LOCAL_HOSTS
    if not cfg.api_key and not local:
        errors.append("an API key is required for a hosted endpoint")
    return errors


def save_planner_config(update: dict[str, Any], path: Path | None = None) -> None:
    """Write the planner section to the config file: mode 0600 in a 0700 directory, atomically.
    `api_key` in `update`: a string sets it, "" clears it, absent keeps the stored one."""
    path = path or config_path()
    data = read_config_file(path)
    planner = dict(data.get("planner") or {})
    for k in ("provider", "base_url", "model"):
        if k in update:
            planner[k] = update[k]
    if "api_key" in update:
        if update["api_key"]:
            planner["api_key"] = update["api_key"]
        else:
            planner.pop("api_key", None)
    data["planner"] = planner
    write_config_file(data, path)


def write_config_file(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_setting(key: str, default: Any = None, path: Path | None = None) -> Any:
    """Non-secret UI settings (e.g. default_folder) live beside the planner config."""
    return (read_config_file(path).get("ui") or {}).get(key, default)


def write_setting(key: str, value: Any, path: Path | None = None) -> None:
    path = path or config_path()
    data = read_config_file(path)
    ui = dict(data.get("ui") or {})
    if value in (None, ""):
        ui.pop(key, None)
    else:
        ui[key] = value
    data["ui"] = ui
    write_config_file(data, path)


# --------------------------------------------------------------------------- #
# Providers                                                                    #
# --------------------------------------------------------------------------- #

def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "[key hidden]") if secret else text


def make_openai_call(cfg: PlannerConfig, timeout: int = REQUEST_TIMEOUT) -> LlmCall:
    problems = check_config(cfg)
    if problems:
        raise PlannerError("; ".join(problems))
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    is_deepseek = (urlparse(cfg.base_url).hostname or "").endswith("deepseek.com")
    key = cfg.api_key

    def _call(system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.2,
            "stream": False,
        }
        if is_deepseek:
            body["thinking"] = {"type": "disabled"}   # we want fast structured JSON, not reasoning
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"},
        )
        if key:
            # unredirected: a redirect must never carry the key to another host
            req.add_unredirected_header("Authorization", f"Bearer {key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as exc:
            raise PlannerError(_scrub(f"planner endpoint answered HTTP {exc.code}", key)) from None
        except urllib.error.URLError as exc:
            raise PlannerError(_scrub(f"could not reach the planner endpoint ({exc.reason})", key)) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise PlannerError("planner endpoint returned an unexpected response") from None
        except TimeoutError:
            raise PlannerError("planner endpoint timed out") from None

    return _call


def make_llm_call(cfg: PlannerConfig | None = None) -> LlmCall | None:
    """The configured provider's llm_call, or None for "no model" (the keyless path)."""
    cfg = cfg or load_config()
    if cfg.provider == "openai":
        return make_openai_call(cfg)
    return None


# --------------------------------------------------------------------------- #
# Using a planner safely                                                       #
# --------------------------------------------------------------------------- #

def route_with_fallback(arch: Architect, brief: str) -> tuple[RouteResult, str]:
    """(result, source): the model's routing if there is a model and it behaves, else keyword routing."""
    if arch.has_llm:
        try:
            result = arch.route(brief)
        except Exception:   # any provider misbehaviour degrades to the keyless router
            result = None
        known = set(arch.blueprints)
        if result is not None:
            if result.matched and result.blueprint_id in known:
                return result, "planner"
            if result.needs_disambiguation and set(result.ambiguous) <= known:
                return result, "planner"
    return arch.route_offline(brief), "offline"


def assemble_with_fallback(
    arch: Architect,
    brief: str,
    blueprint_id: str,
    answers: dict[str, str],
    guardrail: str | None,
    prefix: str,
) -> tuple[dict, str, str | None]:
    """(roster, source, note). Model assembly -> validate -> one retry (inside arch.assemble) ->
    the deterministic offline roster. Bad answers or prefix are the caller's error and raise
    ValueError; a misbehaving model never does."""
    problem = check_prefix(prefix)
    if problem:
        raise ValueError(problem)
    errors = arch.check_answers(blueprint_id, answers, guardrail)
    if errors:
        raise ValueError("; ".join(errors))
    if arch.has_llm:
        try:
            roster = arch.assemble(blueprint_id, brief, answers, guardrail, prefix)
            return roster, "planner", None
        except (ValidationError, PlannerError, ValueError, KeyError, TypeError, OSError) as exc:
            note = f"The planner's answer was not usable ({type(exc).__name__}); used the built-in template instead."
    else:
        note = None
    return arch.assemble_offline(blueprint_id, answers, guardrail, prefix), "offline", note
