"""Static hygiene of the web page's own files (Phase 10, step 5).

The page shows text written by models and by web pages, so the guarantees are structural: nothing is
ever turned into markup, nothing inline runs (the server also sends a strict content-security
policy), and the page loads nothing from anywhere else. These are checks on the source; the page
itself was exercised in a browser pane (see docs/shakeout-log.md)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "teambuild" / "ui" / "frontend"
HTML = (FRONTEND / "index.html").read_text(encoding="utf-8")
JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
CSS = (FRONTEND / "style.css").read_text(encoding="utf-8")


def test_the_token_placeholder_appears_exactly_once_in_the_head():
    assert HTML.count('<meta name="tb-token" content="__TB_TOKEN__">') == 1
    assert HTML.index("tb-token") < HTML.index("</head>")


def test_title_is_the_product_name():
    assert re.search(r"<title>Team Builder</title>", HTML)


def test_no_inline_script_style_or_handlers():
    scripts = re.findall(r"<script\b[^>]*>", HTML)
    assert scripts and all("src=" in s for s in scripts)
    assert "<style" not in HTML.lower()
    assert not re.search(r"\sstyle\s*=", HTML, re.I)
    assert not re.search(r"\son[a-z]+\s*=", HTML, re.I)
    assert "javascript:" not in HTML.lower() + JS.lower()


def test_javascript_never_builds_markup_from_data():
    assert re.findall(r"\.(?:outerHTML|insertAdjacentHTML)\b", JS) == []
    assert not re.search(r"document\.write\s*\(|\beval\s*\(|new\s+Function\s*\(|setTimeout\(\s*['\"]", JS)
    inner = re.findall(r"[\w.]+\.innerHTML\s*=\s*[^;]+;", JS)
    assert inner == ["node.innerHTML = '';"], inner       # only the helper that empties a node
    assert not re.search(r"\.innerHTML\b(?!\s*=\s*'')", JS)
    assert not re.search(r"\.style\.cssText|setAttribute\(\s*['\"]style", JS)


def test_only_same_origin_requests_and_resources():
    assert "url(" not in CSS.replace("url(data:", "")
    assert not re.search(r"@import", CSS)
    assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", HTML)
    # every fetch goes through api() with a same-origin path
    assert re.findall(r"fetch\((?!path)", JS) == []
    assert not re.search(r"new\s+(?:WebSocket|XMLHttpRequest)\b|navigator\.sendBeacon", JS)
    urls = set(re.findall(r"https?://[^\s'\"`)]+", JS))
    assert urls <= {"https://openrouter.ai/api/v1"}, urls           # a placeholder hint, never fetched


def test_every_state_changing_call_carries_the_token_header():
    assert "X-Team-Build-Token" in JS
    # api() is the only place that calls fetch, and it adds the header for every non-GET
    assert JS.count("fetch(") == 1
    assert re.search(r"if \(method !== 'GET'\) \{[^}]*TOKEN_HEADER", JS, re.S)


def test_the_key_field_is_write_only():
    assert re.search(r"type: 'password'", JS)
    assert "api_key" in JS
    reads = re.findall(r"\w\.api_key\b(?!\s*=[^=])", JS)
    assert reads == []                                 # the page only ever assigns a key, never reads one


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_parses():
    r = subprocess.run(["node", "--check", str(FRONTEND / "app.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


THEME = (FRONTEND / "theme.js").read_text(encoding="utf-8")


def test_theme_switch_loads_before_paint_and_has_both_palettes():
    head = HTML[: HTML.index("</head>")]
    assert '<script src="/static/theme.js"></script>' in head                 # not deferred: no flash
    assert head.index("theme.js") < head.index("style.css")
    assert 'id="theme-toggle"' in HTML
    assert ':root[data-theme="dark"]' in CSS and ':root:not([data-theme="light"])' in CSS
    assert "prefers-color-scheme: dark" in CSS


def test_theme_script_is_safe_and_storage_failures_are_harmless():
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|fetch\(", THEME)
    assert THEME.count("try {") >= 2 and "localStorage" in THEME                 # private mode can't break it
    assert re.search(r"ORDER = \['auto', 'light', 'dark'\]", THEME)
