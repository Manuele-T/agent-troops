# Hook template copied into target repos as .claude/hooks/emit.py.
#
# Reads the Claude Code hook payload from stdin and forwards it to the local board, if
# one is running. The board being absent (or slow) must never break or slow a team, so
# every failure is swallowed and this always exits 0. Never writes to stdout — a
# PreToolUse hook's stdout can be interpreted by Claude Code.
import sys
import urllib.request

BOARD_URL = "http://127.0.0.1:8123/hook"
TIMEOUT_SECONDS = 0.3

try:
    payload = sys.stdin.buffer.read()
    req = urllib.request.Request(
        BOARD_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).close()
except Exception:
    pass

sys.exit(0)
