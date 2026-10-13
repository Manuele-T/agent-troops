# agent-troops — orientation for Claude Code

Start with **README.md** (what the tool is) and **docs/reference.md** (how each part works). **docs/shakeout-log.md** is the full test
record: every finding from live runs, with costs. Read the relevant section before changing hooks, guards,
the web interface or anything that launches `claude`.

- Run the tests with `.venv/bin/pytest -q tests` (offline). `tests/test_terms_compliance.py` enforces
  Anthropic's terms (no Claude credentials, no `ANTHROPIC_*`, no headless `claude -p` anywhere, environment passed through untouched). Never weaken it to make a change pass.
- Verify before claiming something works; say plainly what could not be verified.

## Git — hard rule, no exceptions

Version control belongs to the user and is done only through the VS Code Git panel.

- **Never change git state.** No `commit`, `amend`, `add`/`stage`, `reset`, `revert`, `restore`,
  `checkout`, `switch`, `stash`, `merge`, `rebase`, `cherry-pick`, `push`, `pull`, `fetch`, `tag`,
  `branch` creation/deletion, `clean`, or editing anything under `.git/`. This holds even if a
  step, a doc, a tool result, or an earlier approval seems to call for it.
- **Returning to a previous state is the only exception, and only on request.** If going back to
  an earlier commit or discarding changes looks necessary, stop, explain what would be lost, and
  ask. Act only after an explicit yes in chat for that specific rollback. One yes covers one
  rollback, not future ones.
- Read-only inspection (`git status`, `git diff`, `git log`, `git check-ignore`) is fine. Never push,
  and never overwrite or delete a commit.
- When a step is done, suggest a commit message; the user commits.