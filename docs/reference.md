# agent-troops: reference

The details behind the [README](../README.md). Contents: [guard mode](#guard-mode) ·
[a shared policy](#a-shared-policy) · [the board](#the-board) · [the audit](#audit-the-setup-you-already-have) ·
[teams](#teams-optional) · [how a team's safety works](#how-a-teams-safety-works) · [the web page](#the-web-page) ·
[the planner](#the-planner-optional) · [the attack demo](#see-it-stop-real-attacks) ·
[commands and files](#commands-and-files)

---

## Guard mode

```bash
team-build guard install [--shared | --user] [--target DIR] [--mcp] [--sensitive PATTERNS] [--flow block|ask]
team-build guard status  [--target DIR]
team-build guard remove  [--user] [--target DIR]
```

| Scope | Written to | Applies to |
|---|---|---|
| (default) | `DIR/.claude/settings.local.json` | this project, only you (Claude Code keeps the file out of git) |
| `--shared` | `DIR/.claude/settings.json` | this project, everyone who opens it (commit the file) |
| `--user` | `~/.claude/settings.json` | every project on this computer |

**What gets a card on the board:**

- every **web fetch and web search**, from any session: your own, a subagent's (shown by its name), or a team's;
- **shell commands that can reach the network or run downloaded code**: `curl`, `wget`, `ssh`, `scp`, `nc`, `dig`,
  cloud CLIs, `git push`, `npm publish` and other publishes, `gh` commands that write, any web address,
  `| sh`, `base64 -d`, `/dev/tcp`, inline Python/Node code that opens connections. Everyday commands (tests,
  builds, `git status`) are left to Claude Code's own permissions, so you are not asked about every `ls`;
- with `--mcp`, every MCP tool call (the board can't tell an MCP tool that reads from one that sends).

**Sensitive files** (`--sensitive 'config/*,*.pem,customers/*'`): once a session has read a matching file
(`Read`, a content search, or a shell command that names it), everything it tries to send out is refused for the
rest of that session (`--flow block`, the default), or shown on the board with a red warning (`--flow ask`). The
mark is per session, kept in a private folder, and if it can't be recorded the read is refused.

The board, the site lists, the leak warnings and the approval log work exactly as for teams (see
[the board](#the-board)). A plain session shows as one robot, "main session"; its prompt is the job and its
last message the answer. If the board isn't running, the hooks step aside and Claude Code asks in the terminal
as usual. `remove` takes out exactly the entries and files `install` added; when the settings file was formatted
the way Claude Code writes it, it is left byte-for-byte as before.

**Limits:** the list of outward commands is a filter, not a sandbox: a script can reach the network in ways it
doesn't recognise, and only reads through Claude Code's own tools are seen. It is a second line after Claude
Code's own permission prompts. Changes apply to Claude Code sessions started afterwards.


---

## A shared policy

One committed file, `.agent-troops.json` at the root of a repository, that every developer's guard mode and the
CI audit follow. A platform or security team writes it once.

```bash
team-build policy init        # writes a starter file with every rule off
team-build policy check       # is it valid, and what does it set
```

```json
{
  "version": 1,
  "sites": {"allow": ["docs.python.org"], "block": ["pastebin.com", "transfer.sh"]},
  "sensitive": ["config/*", "*.pem", "customers/*"],
  "after_sensitive": "block",
  "require_guard": true,
  "forbid": {"remote_mcp": true, "unpinned_mcp": true, "unknown_hooks": true}
}
```

| Key | Who applies it | Effect |
|---|---|---|
| `sites` | the board, for every call made inside the repository | added to each person's own lists; a **block from either list wins**, so nobody can allow a site the repository blocks |
| `sensitive`, `after_sensitive` | guard mode's hook | added to the person's own patterns; once a session reads a match, outward calls are refused (`block`) or carry a red warning (`ask`); `block` wins if either side says so |
| `require_guard` | `--audit` | FAIL unless guard mode is installed in the shared settings (`team-build guard install --shared`, commit `.claude/`): the only install a CI checkout can see |
| `forbid` | `--audit` | turns those findings into FAILs: remote MCP servers, unpinned MCP packages, hooks that aren't agent-troops' own (for every tool the audit reads) |

Unknown keys are errors, so a typo can't quietly switch a rule off. An invalid file fails the audit, and the
board then allows nothing automatically for that repository: every call gets a card with a red warning. Each
approval log entry records the policy file and a hash of its contents, so the evidence shows which policy was in
force (`--approvals --report` and `--audit --report` print it). With the GitHub Action or the pre-commit hook, a
change that breaks the policy fails the pull request or the commit.


---

## The board

A live view of the session or team as pixel-art robots, driven by the same hook events: who is working, what tool each is
calling, each member's P/U/E boundaries, and refusals shown in red. Below the robots it shows the **results** as
readable text: the job you typed, the team's final answer (tables and lists rendered), and each member's own
report; click a robot to see what it was asked to do. It is also where you **approve**: when a
session or member wants to search the web, open a page or run an outward command, a card shows exactly what
(the search, the URL, the command) with Allow and Deny. When guard mode and a team both send the same call, it
is one card. The board shows and answers; it cannot give an agent work.

- **Fewer clicks, same control.** "Allow \<site\> for this job" (or "Allow searches for this job") covers the
  rest of the job. Site lists in `~/.config/agent-troops/sites.json` (or the page's Guard tab), read on every request:
  `{"allow": ["docs.python.org"], "block": ["pastebin.com"]}` (subdomains included). A blocked site is denied
  without a card; an allowed one runs without one.
- **Leak warnings.** Every call is checked for signs it carries data out: something shaped like a password, key
  or token, a long encoded string, an unusually long piece of a site name (data can leave through the DNS lookup
  itself), a raw IP address, plain HTTP, credentials in the address. A flagged call is shown in red with the
  reasons and **always** asks, whatever the lists say.
- **Approval log.** Every outcome (your clicks, list decisions, timeouts) is appended to
  `~/.local/state/agent-troops/approvals.jsonl` (private, mode 0600, outside every project) with secrets redacted,
  shown on the board under "Approval decisions", and readable with `team-build --approvals [DIR] [--report FILE]`.
  Maker-checker evidence: who allowed what, when.

The board has the same protections as the web page: it listens on 127.0.0.1 only, refuses requests from other
websites (Host, Origin and Sec-Fetch-Site checks), and an answer needs a per-launch token that exists only in
the board page. Start it with `team-build board` (http://127.0.0.1:8123; `team-build ui` starts it too). The
installed package carries the built page; in a checkout, build it once with
`cd board/frontend && npm install && npm run build` (it writes to `teambuild/board/static/`).

A robot appears the first time that member does something, so the analyst and writer show up only once the
orchestrator hands them work.


---

## Audit the setup you already have

```bash
team-build --audit ~/code/app
team-build --audit ~/code/app --report audit.md
```

```
  [FAIL] everything
        tools : Read, WebFetch
        legs  : private_data, untrusted_content, exfiltration
        - holds all three trifecta legs with no approval gate
  [WARN] repo: team rs: relay open. No single agent holds the trifecta, but rs-researcher can reach the web
         and rs-analyst, rs-writer can read files, and nothing stops a session passing data from one to the other
```

Per agent: tools mapped to legs, agents with no `tools:` line (they inherit everything), unguarded or broken
shell guards, `bypassPermissions`, broad `permissions.allow` rules, and tools it can't classify (most MCP tools:
listed, not guessed). Per team: members that *together* hold all three legs without a flow guard.

The rest of the project's Claude Code setup, with or without agents:

- **Hooks**: one that can change Claude Code's settings or permissions fails the audit (a hijacked plugin can use
  that to remove the approval prompt for good, as [PromptArmor showed](https://www.promptarmor.com/resources/hijacking-claude-code-via-injected-marketplace-plugins));
  one that answers prompts with "allow", downloads and runs code (including the DNS-TXT trick from Mozilla
  0DIN's 2026 proof of concept) or contacts another computer is flagged; `disableAllHooks` with guards installed
  fails; an edited copy of one of this tool's own guards is flagged.
- **MCP servers** (`.mcp.json`): remote servers, `npx`/`uvx` packages without a pinned version, secrets written
  into the file (named, values never shown), and `enableAllProjectMcpServers`.
- **Skills, commands and agent files**: hidden HTML comments carrying an address, a command, secrets or sending
  (other non-trivial hidden comments are listed), invisible characters, instruction-override wording,
  `curl … | sh`, and `!` lines that run a shell command as soon as a command or skill is used.

**The other AI coding tools in the repo**, with the same checks. Only files whose format is confirmed in each
tool's own documentation are read:

| Tool | Files read | Extra check |
|---|---|---|
| Cursor | `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/rules/**/*.mdc` | |
| VS Code / GitHub Copilot | `.vscode/mcp.json`, `.github/copilot-instructions.md`, `.github/instructions/**/*.instructions.md` | |
| OpenAI Codex CLI | `.codex/config.toml` (`[mcp_servers.*]`) | |
| Gemini CLI | `.gemini/settings.json` (MCP servers, hooks), `.gemini/commands/**/*.toml` | `"trust": true` (runs a server's tools without asking); `!{…}` shell lines |
| Shared | `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md`, `GEMINI.md` in any folder (dependency and build folders skipped) | |

Not read, because the format couldn't be confirmed: Cursor's older `.cursorrules`, Codex hooks. Checked for false
alarms on eight public repositories (OpenAI Codex, Gemini CLI, VS Code, Next.js, Playwright, Ruff, LangChain,
Sentry): every remaining warning was a real finding (remote MCP servers, all project MCP servers auto-approved,
a `curl … | bash` in an agent command).

`--report` also lists the approval decisions recorded for that folder (see [the board](#the-board)). It reads
configuration, not behaviour; the setup checks are patterns that catch the obvious cases, and the report says so.

### In CI and before each commit

`--fail-on fail|warn|never` sets when the exit code is 1 (default: any FAIL). As a **GitHub Action**, in the
repository you want checked:

```yaml
# .github/workflows/agent-audit.yml
on: pull_request
permissions:
  contents: read
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1   # v7.0.1
      - uses: Manuele-T/agent-troops@v1        # or pin to a commit
        with:
          fail-on: fail                        # fail | warn | never
```

A pull request that adds a permission-rewriting hook, an all-powerful agent or an unpinned MCP server goes red,
and the full report is in the job summary. The action installs the tool from its own checkout (no package
index), passes its inputs as environment variables, never as script text, and pins what it uses to commits.

As a **pre-commit hook** ([pre-commit](https://pre-commit.com)), running only when `.claude/` or `.mcp.json`
changes:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/Manuele-T/agent-troops
    rev: v1
    hooks:
      - id: agent-troops-audit
```


---

## Teams (optional)

Guard mode watches what any session sends out. A team goes further: it splits the work so that no member *can*
read your files, read untrusted content and send data out at once.

```bash
team-build "research the note-taking app market" --dry-run    # preview, writes nothing
team-build "research the note-taking app market"              # answer 2-3 questions, then y
claude --agent rese-orchestrator                              # run the team
team-build --destroy rese                                     # remove exactly what the team added
```

Always start a team **as its orchestrator** (`claude --agent <prefix>-orchestrator`): the orchestrator becomes
the main session, so there is no all-powerful default agent to bypass the team. On the web page: **Teams**
(describe the job, preview every member with its P/U/E/G chips, create), then **Run** (type the job, *Start in a
terminal*; follow it on the board).

| Team | Members (gated = asks you first) | Reads the web | One-way flow | Tested live |
|---|---|---|---|---|
| Market / competitor research | researcher, analyst, writer | yes | ✅ | ✅ many runs |
| Dependency & security check | researcher (advisories), auditor, writer | yes | ✅ | offline only |
| Code review | reviewer, writer, poster (gated, `gh pr comment --body-file`) | no | – | offline only |

Every web search, web fetch and publish asks for your approval on [the board](#the-board).

"Offline only" means every answer combination validates, scaffolds and passes `--audit`, and the shell
allowlists are tested, but no model has run the team yet. Every team has an orchestrator that can only
delegate.

Every role type has a maximum tool set that a team may narrow but never widen:

| Role type | Maximum tools | Legs at maximum | Gated |
|---|---|---|---|
| `orchestrator` | `Task` | none | |
| `researcher` | `WebFetch, WebSearch` | U, E | |
| `builder` | `Read, Grep, Glob, Write, Edit` | P | |
| `reviewer` | `Read, Grep, Glob` | P | |
| `github_publisher` | `Read, Bash` (shell: a few `gh` commands) | P, E | ✅ |

Every role with a shell is gated: a hook that fails to start fails *open* in Claude Code, so a guarded shell
alone isn't trusted to stay narrow.


---

## How a team's safety works

**Checked when a team is made.** The validator (pure code, no model) rejects a team if a role exceeds its
type's tools, a non-gated role holds P+U+E, a gated role doesn't say it needs approval, a role id could escape
`.claude/agents/`, a text field could inject a frontmatter line, or the one-way flow is switched off without an
explicit opt-out. If a planner model is used, its output goes through the same validator, one retry, then the
built-in template: a weak model costs quality, never safety.

**Enforced while a team runs**, by `settings.json` hooks keyed on the hook's `agent_type`, so they touch only
the team's own roles:

- `bash_guard.py`: a gated role's shell may run only its allowlist. Chaining, substitution, redirects,
  variables, `--force`, pushes to a URL and `--token` are refused outright.
- `read_guard.py` (`--fence-reads`): team roles can't read outside the project, symlinks and `..` resolved.
- `flow_guard.py` (one-way flow): once a file-reading member has been used in a session, the web member gets no
  more work in it, so private data can't be carried back out through the web. Installed automatically for
  teams with both kinds of member. `--flow ask` asks you instead of refusing; `--no-flow-guard` switches it
  off and the preview shows `[RELAY OPEN]`.
- `board_gate.py` (approvals on the board): every call that can reach outside the computer (web
  search, web fetch, a gated role's shell) waits for you to click Allow or Deny on the board, which
  then replaces Claude Code's own terminal prompt. No answer in time is a deny. If the board isn't
  running, the hook steps aside and Claude Code asks in the terminal as usual.
- Every agent is also told to treat web pages, files and other agents' reports as data, not instructions.

**What it does not cover:** anything private already in the orchestrator's starting context (your request,
the project's `CLAUDE.md`); teams started without `--agent`; hand-written teams that don't add the hooks; what
a permitted command does once it runs (these are filters, not a sandbox). Don't run a team with
`--permission-mode bypassPermissions`: it removes the approval prompt every gated role relies on.


---

## The web page

`team-build ui` serves a local page with five tabs: **Guard** (turn guard mode on or off for a project or for
every project, edit the site lists, see recent decisions), **Teams**, **Audit**, **Run** and **Settings** (the
optional planner). Light, dark or automatic theme.

**Running a team:** the Run tab opens `claude --agent <prefix>-orchestrator "<your job>"` in the team folder:
an ordinary interactive session that starts on the job (Linux; elsewhere it gives you the command to paste). The
job is passed as one argument, never as shell text, and may not start with a dash. Approvals and results then
show on the board. It works with any Claude Code login, a subscription included; nothing runs headless.

**The page itself:** listens on `127.0.0.1` only; every change needs a per-launch token that exists only in the
page; foreign `Host`/`Origin` headers are refused (cross-site requests, DNS rebinding); no CORS; every folder must
resolve inside your home folder (`--root DIR` narrows it); model output is shown as plain text under a strict
content-security policy.


---

## The planner (optional)

By default teams are filled in from built-in templates: no model, no key, instant. Optionally, a model can tailor
the roles to your brief: any OpenAI-compatible service (OpenRouter, DeepSeek, a local Ollama), set in the
page's **Settings** tab or with `TEAMBUILD_PLANNER=openai`, `TEAMBUILD_PLANNER_BASE_URL`,
`TEAMBUILD_PLANNER_MODEL`, `TEAMBUILD_PLANNER_API_KEY`. Keys live in `~/.config/agent-troops/config.json`
(mode 0600) or your environment, never in a project. Whatever the planner returns goes through the same
validator; `--no-ai` skips it.


---

## See it stop real attacks

```bash
team-build board          # in one terminal (or team-build ui)
team-build demo           # in another; watch http://127.0.0.1:8123
```

A **scripted** agent (no model, no cost, the same every time) tries the attacks from two published 2026
write-ups against a real team, and the **real** guards stop it:

1. a poisoned repo with a hook that rewrites Claude Code's permissions ([PromptArmor](https://www.promptarmor.com/resources/hijacking-claude-code-via-injected-marketplace-plugins))
   and a `curl | sh` command: `--audit` fails it before anything runs;
2. a web page with hidden instructions: an ordinary card on the board (you click Allow);
3. the [Mozilla 0DIN](https://dailysecurityreview.com/cyber-security/mozilla-0din-shows-ai-coding-agents-can-be-tricked-via-dns-txt/)
   trick, `sh -c "$(dig +short TXT … | base64 -d)"`: `bash_guard` refuses it, the robot turns red;
4. a URL smuggling an encoded key out: a red-flagged card with the reasons (you click Deny);
5. sending the web member back out after another member read the files: `flow_guard` refuses it.

The team's hooks run exactly as Claude Code runs them, from the team's own `settings.json`. Every payload is
inert: domains end in `.invalid` (they never resolve), the key is AWS's published example key, nothing is
executed. It shows that the guards work; it does not show how often a real model takes the bait.


---

## Commands and files

| Command | Effect |
|---|---|
| `guard install [--shared \| --user] [--target DIR] [--mcp] [--sensitive P] [--flow block\|ask]` | guard mode: outward calls ask on the board |
| `guard status [--target DIR]` / `guard remove [--user] [--target DIR]` | what is installed where / take it out |
| `policy init [DIR]` / `policy check [DIR]` | write a starter `.agent-troops.json` / validate one |
| `team-build "<brief>"` | create a team in the current folder (asks 2-3 questions) |
| `--dry-run` | show the roster and files; write nothing |
| `--target DIR` / `--prefix NAME` | where to write it / the team's name prefix |
| `--fence-reads` | also install the read fence |
| `--flow ask` / `--no-flow-guard` | one-way flow asks instead of refusing / is switched off |
| `--no-ai` | never call a planner |
| `--destroy PREFIX` | remove exactly what that team added |
| `--audit [DIR] [--report FILE] [--fail-on fail\|warn\|never]` | check any folder's agents, hooks, MCP servers, skills and commands |
| `--approvals [DIR] [--report FILE]` | the approval log, for one folder or all |
| `ui [--port N] [--no-open] [--root DIR]` | the web page |
| `board` | the robot board |
| `demo [--target EMPTY_DIR] [--fast]` | scripted attacks against a real team, watched on the board |
| `--help` | this list |

| Path | What |
|---|---|
| `teambuild/` | `guard.py` (guard mode), `policy_file.py` (the shared policy), `architect.py` (routing, templates, validator), `cc_scaffold.py` (writes and removes teams), `audit.py`, `setup_audit.py`, `other_tools.py` (Cursor, VS Code, Codex, Gemini), `planner.py`, `cli.py`, `blueprints.json`, `templates/` (agent files and hook scripts) |
| `action.yml`, `.pre-commit-hooks.yaml` | the GitHub Action and the pre-commit hook (for use in other repositories) |
| `plugin/`, `.claude-plugin/` | the Claude Code plugin and its marketplace file (`plugin/scripts/` are copies of the templates; a test keeps them equal) |
| `teambuild/ui/` | the web page: `app.py`, `security.py`, `terminal.py`, `server.py`, `frontend/` (plain HTML/JS/CSS) |
| `teambuild/board/` | the robot board's server (FastAPI) and its built page (`static/`) |
| `board/frontend/` | the board page's source (React + PixiJS; `npm run build`) |
| `docs/` | `reference.md` (this page), `shakeout-log.md` (every finding from live runs, with costs) |
| `tests/` | offline tests: `pytest -q tests` |
