---
name: board
description: Explains the agent-troops guard, when the user asks why a web call or command is waiting for approval, where the approval board is, how to start it, or how to turn the guard off.
---

The agent-troops guard is on in this session. What it does:

- Every web fetch and web search, and every shell command that can reach the network or run downloaded
  code (curl, ssh, `git push`, a URL, `| sh` and similar), waits for Allow or Deny on the agent-troops board
  at http://127.0.0.1:8123. Everyday commands (tests, builds, `git status`) are left to Claude Code's own
  permissions.
- The board shows red warnings when a call looks like it carries data out (a key or token, a long encoded
  string, a very long site name, a raw IP address). Every decision is written to the approval log.
- If the user set "Sensitive files" in the plugin's options, a session that has read a matching file can
  no longer send anything out (or each attempt gets a red warning, depending on the option).
- If the board is not running, the guard gives no answer and Claude Code asks as usual.

When the user asks about it:

- **Start the board:** from a checkout of https://github.com/Manuele-T/agent-troops with its virtual
  environment active, run `team-build board`, then open http://127.0.0.1:8123.
- **See past decisions:** `team-build --approvals`.
- **Change the options:** `/config`, the agent-troops rows.
- **Turn it off:** `/plugin`, then disable or uninstall agent-troops.

Never click, approve or call the board's decision endpoints yourself: approvals are for the person. If a
call was denied on the board, do not retry it another way; tell the user what was denied and ask.
