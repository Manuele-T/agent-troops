# Shakeout log: every live test, with what it found and what it cost

Every claim in the README that something works was checked here first, in a real Claude Code session unless
it says otherwise. Costs are what the runs used, in API terms, of the owner's plan.

The early history (2026-09-26 to 2026-10-05) is condensed below to its findings; the full original text,
run by run, is in git history (any commit up to `58d8479`). Sections from "The board as the main tool"
onwards are kept as written. Features named in the early history that were later removed: the web build &
deploy team, the email team, the examples folder, the first plugin packaging, `team-build replay`, the
injection harness, and automated (headless) runs.

---

## Early history, condensed (2026-09-26 to 2026-10-05, Claude Code 2.1.233 then 2.1.286)

### Generating teams (Phases 3-4)

- **Prompt shakeout, 10 briefs** through the planner (DeepSeek): 10/10 valid rosters after two fixes. The
  validator now requires every role field (a missing `tools` key had crashed the scaffolder), and role ids
  are always prefixed (`rese-orchestrator`), so two teams in one repo can't collide.
- **The biggest early bug: a Skill with the same name bypassed the Agent's tool list.** Each role was written
  both as an Agent (with a `tools:` allowlist) and as a Skill. Claude Code picked the Skill, which runs in the
  unrestricted main context: the "holds nothing, only delegates" orchestrator made 21 web calls itself. Fix:
  no Skills at all; one agent file per role. Confirmed live in five later runs (zero unscoped web calls).
- **Getting the orchestrator to delegate, by name, in order** took three more fixes (stronger "MUST BE USED"
  description, an `Agent(...)` list of its siblings, explicit pipeline wording). Full chain
  orchestrator → researcher → analyst → writer completed twice in a row (run5, run6). About $25 over these
  runs.
- **Gated roles hold:** the deploy role, run directly, made zero tool calls and asked for approval first.

### The board (Phase 5)

- Built on the owner's earlier "Hermes" control-surface frontend (HTTP + Server-Sent Events, PixiJS robots).
  The hook payload's `agent_type` is the role id, confirmed on real payloads; the board animates each role
  separately. Its task-queue buttons, inherited from the other project, were later removed (2026-10-05).

### What the scoping can and can't promise (Phases 6-8)

- **Delegation is not guaranteed in a plain session:** two identical runs, one used the team, the other did
  the whole job in the main session (run7/run8, $5.60). Fix: start a team **as its orchestrator**
  (`claude --agent <prefix>-orchestrator`). Then the main session holds only `Task`, so nothing can bypass
  the team, and the `Agent(...)` sibling list is actually enforced (it is ignored in a plain session; probed
  live). run9/run10 confirmed the chain this way (~$1.20 each).
- **The lethal trifecta rule (R7):** the first researcher could read files, read the web and send data out.
  Role maxima were narrowed and the validator now refuses any non-gated role holding all three legs.
- **Handoffs:** an analyst given only a one-line task went searching outside the project for the researcher's
  notes (13 reads, including Claude Code's own transcripts). Fix: the orchestrator pastes the previous
  member's full output into the next task; members never search for each other's notes; every agent treats
  web pages, files and other agents' reports as data, not instructions. run11: zero searches.

### Guards and the audit (Phase 9)

- **Hooks in an agent's own frontmatter did not fire** in headless tests on 2.1.233; hooks in
  `settings.json` did, and carry `agent_type`. All guards are therefore settings-level hooks, keyed on the
  role. Main-session events carry `agent_type` but no `agent_id`.
- **`bash_guard.py`** (a role's shell limited to an allowlist; chaining, redirects, `--force` refused):
  blocked `ls /`, `echo > file`, `&&` chains and `git push --force` live, as main session and as subagent.
- **Claude Code's own `blockReadsOutsideWorkingDirectories` had no effect** on 2.1.233, so `read_guard.py`
  (`--fence-reads`) does it as a hook; live: outside reads blocked. A role asked to edit its own guard was
  refused by Claude Code ("sensitive file").
- **Prompt injection tests:** every model refused every planted injection we could build safely, a plain
  session included, so "plain session leaks, team doesn't" could not be shown. What was measured instead,
  independent of any model: capability. The plain session holds all three legs; no team role does.
  (Two of my own test mistakes are recorded in the original: a non-run counted as a pass, and test names so
  obviously malicious that they measured the model's suspicion, not the structure.)
- **`team-build --audit`** fails a repo, with a reason, for: read + web in one agent, no `tools:` line,
  unguarded shell, `bypassPermissions`, a guard whose script is missing. About $3.3 in this phase.

### The web page (Phases 10-11)

- `team-build ui`: create, audit and run teams from a local page (127.0.0.1 only, per-launch token, folders
  limited to the home folder). Found in the browser: a CSS slip and a folder picker that never loaded; an XSS
  probe rendered as plain text. Two shutdown bugs (an orphaned board, a ten-minute wait on a held request)
  fixed.
- **Anthropic's terms:** the page first ran teams headless (`claude -p`). Anthropic's docs describe
  `claude -p` as the Agent SDK's command-line form, which should use API-key authentication, so from Phase 11
  headless runs were allowed only behind a login check (API key or cloud provider), and subscription users
  ran teams in a terminal. **On 2026-10-07 the headless mode was removed entirely** (see the end of this
  log): the tool now never runs `claude -p`, and the compliance test bans its flags everywhere.

### One-way flow (Phase 12)

- **`flow_guard.py`**: once a file-reading member has been used in a session, the web member gets no more
  work, so private data can't be relayed out through the web. Live, interactive (owner, 2.1.233/2.1.286):
  the forced "analyst, then researcher" chain was refused, also after `--continue`; ask mode showed Claude
  Code's own prompt with the guard's reason. A few cents.

### Owner's decisions to remove things (2026-10-01 to 10-05)

The web build & deploy team, the email team and its mail connector, four smaller teams, the examples folder,
the first plugin packaging, `team-build replay` and the injection harness were removed to keep the project
lean. Three teams remain: research, dependency check, code review. Tests that used the deploy role as their
gated example moved to the code-review poster (same mechanism).

---

## The board as the main tool: approvals and results (2026-10-05, Claude Code 2.1.286)

The board's leftovers from its original project (task queue, spawn/note/unblock/reassign/cancel, Hermes approval
panel, cost display) were removed, with the backend's `/tasks` and `/act`.

**Approvals on the board.** `board_gate.py` (PreToolUse, matcher `WebFetch|WebSearch|Bash`, timeout 600) is
installed for every role whose tools can reach outside the machine. It POSTs the call to the board's `/gate`,
which holds it until Allow/Deny (token-protected, Host/Origin/Sec-Fetch-Site checked) or 560 s (deny). No board
running: the hook gives no decision, so Claude Code's own permissions apply unchanged.

Live, interactive session, research team, driven through a pseudo-terminal, cards answered through `/gate`:
- 5 web calls (4 fetches to sites with no allow rule, 1 search), then 9 in a later run: each appeared as a card,
  ran after Allow, and Claude Code showed **no** terminal prompt. So a PreToolUse `allow` replaces the prompt.
- The owner's `settings.local.json` already allowed `WebSearch` and some domains; the fetched domains were not
  in it. Not changed by the test.

**Results on the board.** Hooks now include `UserPromptSubmit` (the job). `Stop` and `SubagentStop` carry
`last_assistant_message` (docs: "use this instead of reading the transcript file"), shown as Markdown on the
board; the orchestrator's delegation prompt is shown per member as "Asked to". Found live:
- The orchestrator ran the researcher **in the background**. Its result came back as a `<task-notification>`
  message, which also fires `UserPromptSubmit`; the first version took it for a new job. Now it is filed as the
  member's report, matched through the delegation's `tool_use_id`.
- Claude Code runs small internal helpers (one returned "(No obvious follow-up)") whose `SubagentStop` carries the
  **main agent's** `agent_type`. Reports under the orchestrator's name are now ignored.
- In these runs the orchestrator used only the researcher and wrote the table itself, skipping analyst and
  writer. A model choice, not a board problem; noted because the board makes it visible.

Cost: three research runs, about $0.50 each of the owner's plan. Not tested live: Deny and the timeout (offline only).

## Smarter approvals, approval log, wider audit (2026-10-06)

**Approval cards.** Site allow/block lists (`~/.config/agent-troops/sites.json`), "allow for this job" (site or
web searches, until the next job), and leak warnings (secret-shaped values, long encoded strings, long DNS labels
as in Mozilla 0DIN's DNS-TXT proof of concept, raw IPs, plain HTTP, credentials in URLs). A flagged call is never
allowed automatically. Hook detail raised from 500 to 2,000 characters so a long smuggling URL is not cut before a
person sees it (teams made before this carry the older hook; the audit flags the difference).

**Approval log.** `~/.local/state/agent-troops/approvals.jsonl` (0700/0600), every outcome with secrets redacted;
`team-build --approvals`; included in `--audit --report`. Found while building: the first test run wrote 13 test
decisions into the real log because the board tests did not point the log elsewhere; fixed (tests use a temporary
log) and the owner told.

**Wider audit** (`teambuild/setup_audit.py`): hooks (settings/permission writes = FAIL, after PromptArmor's
hijacked-plugin write-up; allow-answering, download-and-run, outbound network = WARN; `disableAllHooks` with guards
= FAIL; edited guard copies = WARN), `.mcp.json` (remote, unpinned npx/uvx, literal secrets, auto-approve),
skills/commands/agent files (hidden comments, invisible Unicode, override wording, `!` shell lines).
Checked on the owner's real projects: no false alarms; the one WARN was a team made the day before with the
previous `board_gate.py`.

Checked in the browser on a test board (scratch log and lists): a normal and a flagged card, "Allow for this
job", Deny, the decisions list, and the redacted log line. Not checked live with Claude Code (the hook interface
is unchanged since the 2026-10-05 live runs).

## `team-build demo` (2026-10-06)

A scripted agent against the real guards: the team's PreToolUse hooks are run from its own settings.json as
argument lists with the hook event on stdin (deny/blocked wins), the same contract as Claude Code. Payloads inert
(`.invalid` domains, AWS's example key, nothing executed). Run against the real board: audit FAIL on the poisoned
repo (3 findings), page card allowed, 0DIN `dig | base64 -d | sh` refused by bash_guard (metacharacters; the poster
turned red), smuggling URL flagged and denied on the board, relay refused by flow_guard. The run's two clicks went
into the owner's real approval log (cwd: the demo folder).

Found while building: for a command bash_guard refuses, board_gate still showed a card (Claude Code runs matching
hooks in parallel and waits for all, so the refused call would also have waited for a pointless click).
board_gate now applies the role's bash_guard allowlist first and asks only about commands bash_guard would allow.

## Guard mode, the plugin, the Guard tab (2026-10-06, Claude Code 2.1.289)

**Guard mode** (`team-build guard install|status|remove`, `teambuild/guard.py`): `board_gate.py --all` judges every
session's calls, with or without `agent_type` (a plain session is "main" on the board). Web fetch/search always;
Bash only when `outward_reason()` matches (network programs, publishing sub-commands, `gh` writes, URLs, `| sh`,
`base64 -d`, `/dev/tcp`, inline network code); MCP with `--mcp`; `--sensitive` marks a session that read a
matching file and then refuses (`--flow block`) or warns (`--flow ask`). Three scopes (local, shared, user);
`guard.json` records what was added, never a copy of the settings; remove restores a Claude-Code-formatted
settings file byte for byte (tested).

**Board:** events without `agent_type` are now the "main" robot (they were ignored); the same event or gate
request arriving twice (team + guard, or guard in two scopes) counts once: one card, one answer, one log line.
The gate accepts any subagent name (`Explore`, `plugin:agent`) and `mcp__*` tools, and extra warnings from the hook
(they can only stop an automatic allow).

**Plugin** (`plugin/`, `.claude-plugin/marketplace.json`): the same two scripts (copies, kept equal by a test),
`hooks/hooks.json`, a `board` skill, and three `userConfig` options read by the hook as `CLAUDE_PLUGIN_OPTION_*`.
`claude plugin validate --strict` passes for the plugin and the marketplace. Facts checked in the docs first:
marketplace format and install commands, `${CLAUDE_PLUGIN_ROOT}`, userConfig → `CLAUDE_PLUGIN_OPTION_<KEY>` in hook
processes, plugin hooks run alongside settings hooks ("a plugin's copy of the same handler stays separate", hence
the board-side de-duplication). Not documented: how an allow from one hook and a deny from another combine.

**Found while building:** the audit merged `settings.json` and `settings.local.json` shallowly, so any hook in the
local file hid every hook in the shared one (a team's guards looked missing once guard mode was installed
locally). Fixed: objects merge, lists add up, as Claude Code combines them.

**Guard tab** on the page: folder, scope, options, install/remove, board status, site-list editor (writes the file
the board reads), recent decisions. Checked in the browser on a sandboxed page (fake home, scratch lists and log),
desktop and phone width.

**Live check** (after the owner logged in again; the first attempt failed with "Login expired"). A pty-driven
plain interactive session (`claude --model sonnet "<job>"`, clean environment, the owner's own login), guard mode
installed locally with `--sensitive 'config/*'`, plus a second project hook answering "allow" to every WebFetch;
cards answered through the board's API with its page token; board log and lists in a scratch folder:

1. WebFetch example.com: card ("main"), Allow, ran.
2. WebFetch example.org: card, Deny, refused, although the other hook said "allow". **A deny from one hook beats
   an allow from another** (the docs don't say; now seen live).
3. `curl -sI https://example.com`: card, Allow, ran.
4. `ls`: no card, ran.
5. Read `config/db.yml`: ran; the session was marked.
6. WebSearch: refused by board_gate with no card ("this session has read files marked sensitive (config/*)"); the
   model reported it and did not retry.

Job and final answer on the board; 0 permission prompts in the terminal (the owner's default mode is auto).
Plugin: a fresh folder, no guard installed, `--plugin-dir plugin/`: WebFetch and `curl` cards, both ran after
Allow; `echo` no card; 0 terminal prompts. Not checked live: plugin options set through `/config` (how booleans
arrive in `CLAUDE_PLUGIN_OPTION_*`), installing from the GitHub marketplace. Cost: two short Sonnet sessions,
roughly $0.10 of the owner's plan.

## `--audit` in CI: GitHub Action and pre-commit (2026-10-06)

`--fail-on fail|warn|never`; `action.yml` (composite: setup-python pinned to a commit, installs the tool from the
action's own checkout, inputs passed as environment variables, report into the job summary, `result` output);
`.pre-commit-hooks.yaml` (`files: ^(\.claude/|\.mcp\.json$)`); `.github/workflows/ci.yml` (tests, then the action
via `uses: ./` on `tests/fixtures/ci/clean`, must pass, and `.../poisoned`, must fail with result FAIL).

Checked here: the action's audit script run with bash against both fixtures (exit code, `result`, summary);
a fresh venv `pip install` of the repo, as the action does it (poisoned exit 1, clean exit 0); the full suite
from a copy of exactly the files git would commit, in a fresh venv with the pinned test versions, with and
without `claude` on the PATH (782 passed). Not run yet: the workflow on a real GitHub runner (needs a push),
`pre-commit try-repo` (pre-commit is not installed here), the `@v1` tag (the owner's to create).

Slip, recorded: the first rehearsal copied the whole working folder (`cp -r`) into the scratch folder, including
the git-ignored secrets file. It was not opened or printed, and the copy was deleted at once; the second rehearsal
copied only `git ls-files -co --exclude-standard`.

## Other AI coding tools in the audit (2026-10-06)

`teambuild/other_tools.py`. File names and formats confirmed in each tool's docs first: Cursor (`.cursor/mcp.json`
"mcpServers", `.cursor/hooks.json` "hooks": {event: [{command}]}, `.cursor/rules/*.mdc`), VS Code / Copilot
(`.vscode/mcp.json` "servers", `.github/copilot-instructions.md`, `.github/instructions/**/*.instructions.md`),
Codex CLI (`.codex/config.toml` `[mcp_servers.*]` with `http_headers`; AGENTS.md / AGENTS.override.md), Gemini CLI
(`.gemini/settings.json` "mcpServers" with url/httpUrl and "trust", "hooks" in Claude Code's shape;
`.gemini/commands/*.toml` with `!{...}`; GEMINI.md). Dropped as unconfirmed: `.cursorrules`, Codex hooks.
setup_audit.py's checks were split into reusable judges (hook, MCP server, instruction file) so every tool gets
the same rules.

False-alarm check: sparse shallow clones (config and instruction files only, read, never run) of openai/codex,
google-gemini/gemini-cli, microsoft/vscode, vercel/next.js, microsoft/playwright, astral-sh/ruff,
langchain-ai/langchain, getsentry/sentry, and the owner's own projects. First pass: hidden HTML comments flagged in
three repos were harmless markers and maintainer notes, and "No `eval()`" in LangChain's AGENTS.md matched the
download-and-run pattern. Fixed: a hidden comment is WARN only when it carries an address, a command, secrets,
sending or override wording (single-token markers ignored, other comments INFO); `eval` needs an argument. Second
pass: every WARN is real (remote MCP servers in LangChain and Sentry, Sentry's enableAllProjectMcpServers and its
`curl … | bash` agent command); the owner's projects clean except the known stale team hook.

## Easy install: package, board inside it, Windows hooks, CI matrix, release (2026-10-06)

Distribution renamed `agent-troops` (free on PyPI on 2026-10-06; the import package stays `teambuild`, the command
stays `team-build`). The board's server moved from `board/backend/` to `teambuild/board/`; its page now builds into
`teambuild/board/static/` (git-ignored) and ships in the wheel, so `team-build board` works without a checkout.
Hook commands use `python` on Windows, `python3` elsewhere; user-scope guard paths are written with forward
slashes. CI: tests on Linux, macOS and Windows × Python 3.11-3.13 (macOS and Windows not failing the build until
they have passed once), a job building the page and the wheel and serving the board from a fresh install, the
action job. `release.yml`: a `v*` tag matching the version builds, tests and publishes by PyPI trusted publishing
(no stored token; the trusted publisher is the owner's to set up on pypi.org). Every action pinned to a commit.

Checked here: `python -m build` in a fresh venv (sdist: no hidden files; wheel: only `teambuild/`, with the 21 page
files); the wheel installed in a fresh venv away from the checkout served the board page and its assets, and ran
the audit and guard status. The old `teambuild` distribution was uninstalled from the project venv so only
`agent-troops` provides `team-build`. Not run: the workflows on GitHub, anything on macOS or Windows, the PyPI
publish, the macOS "open a terminal" path (needs a Mac; the page still gives the command to copy).

## First CI runs on GitHub (2026-10-06)

Run #2 (commit b6398f8, 2m 42s): Linux tests (3.11-3.13) passed, macOS tests (3.11-3.13) passed, Windows tests
(3.11-3.13) failed (logs not yet read), package job passed (wheel built, installed fresh, board page served), action
job passed (clean fixture PASS/NO AGENTS; poisoned fixture FAIL with the permission-rewriting hook named in the
job summary, as intended). The repository is private, where macOS minutes count about ten times and Windows about
twice, so a push cost an estimated 80 of the 2,000 free monthly minutes. Changed: every push now runs Linux only
(3.11 and 3.13), the package and the action (about 5-7 minutes); macOS and Windows moved to `platforms.yml`, run
by hand.

## CI workflows removed (2026-10-06)

The owner's billing page showed $0.66 gross, $0 billed (all within the free allowance) after the two runs above.
To keep the private repository free of Actions usage, `.github/workflows/` (ci.yml, platforms.yml, release.yml) was
removed. The results of run #2 stand as recorded above. `action.yml` and `.pre-commit-hooks.yaml` stay: they run
in the repositories that use them, not here. Publishing to PyPI, if wanted, is now a manual build and upload.

## A shared policy: .agent-troops.json (2026-10-06)

`teambuild/policy_file.py` (find from a folder upwards, load, strict validation: unknown keys and wrong types are
errors, a hash of the file), `team-build policy init|check`. The board adds a repository's site lists to the
person's own for calls whose cwd is inside it (block wins from either side), records "policy": path + sha256 in
every approval log entry, and turns an invalid file into a red warning on every card (no automatic allows). Guard
mode's hook reads the file itself (it runs without the package): sensitive patterns added, block wins when both
sides set a mode, an invalid file ignored there (the board's warning covers it). The audit: invalid file = FAIL;
`require_guard` = FAIL unless board_gate.py --all is in the shared .claude/settings.json (a local install is
invisible to CI, tested); `forbid` turns remote/unpinned MCP and non-agent-troops hooks into FAILs, for every
tool's configs; sensitive patterns with no shared guard = WARN. `guard status` shows the policy.
27 offline tests. Not live-checked with Claude Code: the hook and board code paths are the ones live-checked
earlier, with the policy feeding the same lists and patterns.

## Leaner (2026-10-07)

Owner's decision. Removed the automated-run mode (a second way to run a team from the web page, headless with
`claude -p`, only for API-key or cloud logins): `teambuild/ui/runs.py`, `teambuild/ui/approval_gate.py`,
`teambuild/claude_auth.py`, their two test files and the Run tab's run view, about 1,800 lines. Teams run in a
terminal, followed on the board, as before. The compliance test got stricter: its permit-checked exception and
its one label exemption are gone, and any headless flag anywhere in `teambuild/` or `plugin/` now fails it. The
page's guard no longer has a token-exempt endpoint. This log's early history (above) was condensed; the README
was shortened, with the reference detail moved to `docs/reference.md`. Checked: the full test suite, and the
Run tab in the browser (team listed, terminal command built, no console errors).
