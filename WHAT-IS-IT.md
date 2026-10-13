# What is agent-troops?

**A four-eyes check for AI coding assistants.** When Claude Code wants to send something off your computer (open
a web page, search the web, run `curl`, `git push`), it has to wait until a person clicks **Allow** on a small
local dashboard. Every decision is written to a log.

## The problem

AI coding assistants can read your files, read web pages and send data out. Put those three together and a
web page or a README with hidden instructions can trick the assistant into leaking your code or secrets. This is
known as the "lethal trifecta" and as prompt injection, OWASP's top risk for AI applications. Claude Code already
asks before many actions, but the prompts appear in a terminal, are easy to click through, leave no record, and
can't be governed centrally.

## What it does

| Part | In one line |
|---|---|
| **Guard mode** | One command (or a Claude Code plugin). From then on every outward call shows on the board for Allow/Deny, with red warnings when it looks like it carries a secret out. |
| **Sensitive files** | Mark files such as `config/*` or `*.pem`. Once a session has read one, nothing more may leave the computer from that session. |
| **Approval log** | Who allowed what, and when, with secrets blanked out. |
| **Audit** | Checks a repository's AI setup (Claude Code, Cursor, VS Code/Copilot, Codex, Gemini) for known attack patterns, such as a hook that switches approvals off. Runs offline, in CI or before each commit. |
| **Shared policy** | One file in the repository: sites nobody may contact, sensitive files, required protections. Everyone's dashboard and the audit follow it. |
| **Teams** (optional) | Ready-made assistant teams where no single member can read files, read the web *and* send data out. |

## Why a company would care

It applies controls that corporate risk and audit teams already use to a new kind of tool:

- **Maker-checker:** the assistant proposes, a person approves.
- **Least privilege and segregation of duties:** the teams, the sensitive-file rule.
- **Preventive and detective controls:** block lists and refusals; the audit in CI.
- **Evidence:** the approval log and the audit reports.

## What it is not

A prototype, not a certified product. It checks configuration and filters actions; it is not a sandbox, and a
determined program can reach the network in ways it doesn't recognise. It works with your own Claude Code and
your own login: no keys, no hosting, nothing leaves your computer.

## Try it

Ten minutes: [README, Quick start](README.md#quick-start). No cost to look around: `team-build demo` replays five
published attack techniques against the real guards, with no AI model involved.
