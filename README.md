# Agent Troops

**Deploy AI agent teams in Claude Code, with security and traceability built in.**

Claude Code already gives you subagents and an experimental agent-teams feature. agent-troops adds task
templates, enforced least privilege and human approval on top: it creates a different agent team for each task
(market research, dependency and security checks, code review), with every agent getting only the permissions
its job needs. The researcher can browse the web but can't read your files; the analyst can read files but can't
go online; the orchestrator only coordinates. No single agent can read your data, take in outside content and
send data out at once.

Around the teams, and in any ordinary Claude Code session too:

- **Human-in-the-loop:** anything leaving the machine (a web page, a search, `curl`, `git push`) waits for a
  person's Allow or Deny on a live dashboard
- **Traceability:** you see each agent at work, and every decision is logged, with secrets redacted
- **Data loss prevention:** once an agent has read a sensitive file, nothing more can leave; requests that smuggle
  keys or encoded data out are flagged in red
- **Detective controls:** an audit of AI tool setups (Claude Code, Cursor, Copilot, Codex, Gemini), ready for CI
- **Policy-as-code:** one committed `.agent-troops.json` per repository that everyone's setup follows
- **A Claude Code plugin** that brings the approvals to any project

It's a prototype, not a product: it filters actions, it doesn't sandbox them. But it shows that least privilege,
separation of concerns and maker-checker translate naturally to agentic AI. Open source, runs on your own
computer with your own Claude Code. **[One-page overview](WHAT-IS-IT.md)** · **[Full reference](docs/reference.md)**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

---

## What it looks like

**1. A person approves every outward call.** Claude Code wants to open python.org; the call waits on the board
until someone clicks Allow, "Allow this site for this job" or Deny.

![An approval card on the board](docs/screenshots/1-approval-card.jpg)

**2. After reading a secret, nothing leaves.** A real session fetched a page, read `config/db.yml` (marked
sensitive), then tried to search the web: the search was refused automatically. This is Claude's own summary.

![The job and Claude's answer explaining the refusal](docs/screenshots/2-refused-after-reading-a-secret.jpg)

**3. Smuggling gets flagged in red.** From the built-in attack demo: a web address hiding encoded text (`d=…`,
"SecretsFromTheProject123456") and an AWS key (`k=AKIA…EXAMPLE`, Amazon's published dummy key). Both are
flagged on the card. The red robot is another agent whose disguised shell command was just refused.

![A red-flagged card and a red robot](docs/screenshots/3-leak-warning-and-blocked-robot.jpg)

**4. Five attack techniques, and the evidence.** The demo's results, each attack stopped by a different guard,
and the approval log of who allowed or denied what (the smuggled key redacted).

![Demo results and the approval log](docs/screenshots/4-attack-demo-and-approval-log.jpg)

**5. The audit catches a poisoned setup before anything runs:** an all-powerful agent, a hook that rewrites
Claude Code's permissions, an unpinned MCP server.

![The audit failing a poisoned repository](docs/screenshots/5-audit-fail.jpg)

---

## Quick start

Needs Python 3.11+, [Claude Code](https://code.claude.com/docs/en/overview) signed in, and Node.js once (to build
the dashboard). No API key, no AI model.

```bash
git clone https://github.com/Manuele-T/agent-troops && cd agent-troops
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[ui]'
cd board/frontend && npm install && npm run build && cd ../..
```

Start the dashboard and keep it running (http://127.0.0.1:8123):

```bash
team-build board
```

**Create and run a team**, from your project folder (answer 2-3 questions; `--dry-run` previews it first):

```bash
team-build "research the note-taking app market"
claude --agent rese-orchestrator                  # start the team as its orchestrator
```

**Or guard any Claude Code session**, no team needed:

```bash
team-build guard install --target ~/code/app      # this project, only you (guard remove undoes it)
cd ~/code/app && claude                           # outward calls now ask on the dashboard
```

**Or as a Claude Code plugin** (every project). The dashboard still runs from the checkout above:

```bash
claude plugin marketplace add Manuele-T/agent-troops
claude plugin install agent-troops@agent-troops
```

**Other ways in:** `team-build ui` opens a web page to turn it on and off, edit site lists and see decisions.
`team-build demo` replays five attack techniques against the real guards, free, no AI model.
`team-build --audit DIR` checks any repository.

---

## Anthropic's terms

It runs **your own, unmodified Claude Code**, interactively, and never touches your login: no credentials, no
`ANTHROPIC_*` variables, no headless `claude -p`, nothing hosted, no telemetry. It only adds hooks to Claude
Code's own settings. `tests/test_terms_compliance.py` enforces this on every run and is tested against planted
violations. Our reading of the published terms, not legal advice.

---

## What has been verified

Every live test, with what it found and what it cost, is in [docs/shakeout-log.md](docs/shakeout-log.md).

- **Live, plain Claude Code sessions:** web calls became cards and ran after Allow; Deny on the board beat
  another hook's "allow"; everyday commands got no card; after a sensitive read, the next web call was refused;
  no terminal prompts. The same with the plugin.
- **Live, teams:** the research team end to end (~$0.50 a run), with its guards blocking as designed.
- **Install:** a fresh clone served the dashboard; the plugin installed into a clean Claude Code configuration.
- **Platforms:** Linux; one CI run also passed on macOS; Windows not supported yet.
- **Not verified:** the plugin's optional settings; two of the three teams with a real model.

Run the offline tests with `pytest -q tests`. MIT licensed. Role types adapted from
[Autonomous-Builder](https://github.com/Manuele-T/Autonomous-Builder).
