# mgit

Multi-repo git workspace manager. Coordinate branches, features, and bulk operations across multiple git repositories from a single CLI. Designed for AI coding agents and human developers alike.

## Why mgit?

Modern projects often span multiple repositories — a frontend app, a backend API, shared libraries, infrastructure configs. Making a cross-cutting change means juggling branches, stashing work, and remembering which repos you touched. mgit gives you a single command layer on top of plain git repos so you can:

- **Create features that span repos** — one command enrolls repos, creates sandbox branches, and sets up isolated worktrees
- **Carry uncommitted work** into a feature without losing anything
- **Bulk commit, push, and pull** across repos scoped to a feature
- **Context-switch instantly** between features without branch gymnastics
- **Discover dirty repos** across your workspace and bring them into a feature
- **Keep a working memory per feature** — an auto-populated journal + plan file that an AI agent (or you, next Monday) recovers with one `mgit feature brief` call
- **Fork features like branches, across all repos at once** — try two approaches as sibling variants with pinned base SHAs and inherited memory
- **Checkpoint and restore a whole feature** — non-destructive cross-repo save-points covering committed *and* uncommitted work
- **Distill durable skills** out of what you learned across features — you review them, then every future agent session loads them automatically

## Installation

```bash
pip install git+https://github.com/abandiDev/mgit.git
```

or with [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/abandiDev/mgit.git
```

## Example: cross-repo auth refactor

Imagine you maintain a microservices project with three repos: `user-service`, `api-gateway`, and `shared-lib`. You need to refactor the auth logic across all three.

### 1. Set up the workspace

```bash
# Create a workspace directory and initialize mgit
mkdir ~/my-project && cd ~/my-project
mgit init

# Add your repos (clone from remote or link local paths)
mgit repo add https://github.com/org/user-service.git
mgit repo add https://github.com/org/api-gateway.git
mgit repo add https://github.com/org/shared-lib.git

mgit repo list
#   user-service    ~/my-project/user-service
#   api-gateway     ~/my-project/api-gateway
#   shared-lib      ~/my-project/shared-lib
```

### 2. Start a feature

```bash
# Enroll specific repos in a feature (lazy mode — no worktrees yet)
mgit feature start auth-refactor -r user-service -r api-gateway -r shared-lib
# Feature 'auth-refactor': 3 newly enrolled, 3 total.
#   + user-service
#   + api-gateway
#   + shared-lib
# Active feature: auth-refactor

# Or, materialize worktrees immediately with --materialize
mgit feature start auth-refactor -r user-service -r api-gateway --materialize
# Worktrees created at .mgit/worktrees/auth-refactor/<repo>/
```

Each worktree gets a sandbox branch (`mgit/auth-refactor`) checked out. Your original repos stay on `main` — untouched.

### 3. Work in worktrees

```bash
# Materialize a worktree on demand (if you didn't use --materialize)
mgit feature materialize user-service
# user-service: materialized -> .mgit/worktrees/auth-refactor/user-service/

# If the repo has uncommitted changes, carry them into the worktree
mgit feature materialize api-gateway --carry
# api-gateway: materialized -> .mgit/worktrees/auth-refactor/api-gateway/ (changes carried)

# Edit files in the worktree directories
cd .mgit/worktrees/auth-refactor/user-service/
# ... make your changes ...
```

### 4. Bulk operations

```bash
# Status across all feature repos (use -f . for the active feature)
mgit status -f .

# Commit across all materialized repos
mgit commit -m "refactor auth token handling" -f .

# Push feature branches to remote
mgit push -f .
```

### 5. Context-switch to another task

```bash
# Start a different feature — your auth-refactor worktrees stay intact
mgit feature start hotfix-login -r user-service --materialize

# Switch back anytime
mgit feature switch auth-refactor
```

### 6. Sync dirty repos into a feature

You've been editing `shared-lib` outside the feature. Sync discovers it and brings it in:

```bash
# Make changes in shared-lib (outside the feature)
echo "fix" >> ~/my-project/shared-lib/auth.py

# Sync detects dirty repos, enrolls them, and carries changes
mgit feature sync
# Synced 1 repo(s) into feature 'auth-refactor':
#   + shared-lib (changes carried)
```

### 7. Working memory

Every feature keeps a persistent working memory: a plan file plus an append-only
journal that mgit populates automatically at every lifecycle step (enroll,
materialize, sync, commit, push, fork). A fresh session — human or agent —
re-orients with one call:

```bash
mgit feature brief
# # Feature: auth-refactor
# ## Working memory (updated 2h ago)
# Goal: Refactor auth token handling
# Status: shared-lib done, migrating services
# Next steps:
#   1. swap token lib in api-gateway
# ## Repos
# - shared-lib -> auth-refactor (materialized, clean, 3 ahead of main, last: 4f2a1c9 v2 tokens)
# ...
# ## Recent journal
# - ... agent/decision: keep v1 shim until gateway migrates

mgit feature plan --goal "..." --status "..." --add-next "..." --done 1
mgit feature note "keep v1 shim until gateway migrates" --type decision
mgit feature log -n 30 --commits      # journal interleaved with git commits across repos
```

Live git facts (dirty, ahead counts, last commit) are computed on demand — git
stays the source of truth; mgit never stores state that can lie.

### 8. Fork a feature to try a variant

```bash
mgit feature fork auth-refactor-jwt --carry-wip
# Forked 'auth-refactor' -> 'auth-refactor-jwt' (3 repos).
#   + shared-lib @ 4f2a1c9bcd12 +wip
# ...

mgit feature tree
#   auth-refactor [3/3 repos]
# *   └─ auth-refactor-jwt [0/3 repos]
```

Each repo's branch point is pinned at fork time, so lazy worktrees branch from
the fork point even after the parent advances. The child inherits the parent's
memory (with a `branched_from` journal entry) and pushes to its own remote
branch by default.

### 9. Checkpoint before risky work

```bash
mgit checkpoint save --label "before dropping v1 tokens"
mgit checkpoint list
mgit checkpoint restore cp-0001       # always auto-saves a safety backup first
```

A checkpoint pins every materialized repo's HEAD and snapshots uncommitted work
as commit objects (via a temp index — your working tree is never touched),
anchored by `refs/mgit/checkpoint/...` so gc can't eat them and push never
publishes them. Restore brings back committed state, re-materializes the WIP as
dirty files, and rewinds the memory file to match.

### 10. Publish: linked PRs across repos

```bash
mgit feature publish
#   [+] shared-lib: pushed; PR created: https://github.com/org/shared-lib/pull/12
#   [+] api-gateway: pushed; MR created: https://gitlab.com/org/api-gateway/-/merge_requests/7

mgit feature checks
#   shared-lib    open  checks: 3 ok / 0 failed / 1 pending  review: APPROVED
#   api-gateway   open  checks: 1 ok / 0 failed / 0 pending
```

`publish` pushes each repo's branch and opens a PR (GitHub via `gh`) or MR
(GitLab via `glab`) — the forge is auto-detected per repo from its remote URL,
so mixed workspaces work. PR bodies are generated from the feature's working
memory (goal, status, recent decisions) and cross-link the sibling PRs.
Re-running is idempotent: new commits are pushed and existing PRs updated, never
duplicated. Authentication belongs to `gh`/`glab` — mgit never touches tokens.

### 11. Distill durable skills from what you learned

Working memory is per-feature: it dies with the feature. `mgit skill` graduates
the *durable* lessons out of it, so a convention learned in `auth-refactor`
reaches next month's feature without anyone remembering to mention it.

```bash
mgit skill distill                    # mine steering across every feature
#   pin-fork-base-shas: draft (.mgit/skills/drafts/pin-fork-base-shas)
#   prefer-shim-until-migrated: park (prose lesson, first occurrence (n=2 rule))
#   rename-authctx-var: drop (classified one-off)

mgit skill drafts                     # what is awaiting review
mgit skill show pin-fork-base-shas    # read the full SKILL.md
mgit skill approve pin-fork-base-shas --run-verify
mgit skill reject rename-authctx-var --reason "specific to one repo"
mgit skill doctor                     # active / drafts / parked backlog
```

Nothing reaches an agent unreviewed. A candidate is **dropped** if the distiller
classifies it a one-off, **parked** if it is a first-occurrence prose lesson (the
n=2 rule — it must recur to earn a draft), and promoted to a **draft** only when
it recurs, states an explicit rule, updates an existing skill, or ships a
`verify_command`. Drafts go live only via `mgit skill approve`; `mgit skill
reject` tombstones a slug so it is never proposed again. Approved skills are
advertised in the ambient brief that every worktree session already loads.

Distillation shells out to the `claude` CLI in headless mode (configurable under
`[skills]` in `.mgit/config.toml`), and journal text is scrubbed for secret
shapes before it leaves the machine. A draft's `verify_command` is written by an
LLM from journal content, so it is **never executed during distill** — it runs
under your eye at `mgit skill approve --run-verify`, unless you opt in to
`allow-auto-verify`.

## Agent workflow

mgit is built agent-first. Four layers, from ambient to programmatic:

**Ambient (zero config).** mgit generates `CLAUDE.md` and `AGENTS.md` at
`.mgit/worktrees/<feature>/` — an ancestor of every worktree, outside every
repo. Claude Code, Codex, OpenCode, and other harnesses that walk parent
directories load the feature's memory automatically when a session starts
inside a worktree. The files are regenerated on every mgit operation; never
edit them by hand.

**The ritual.** `AGENT.md`/`AGENTS.md` at the workspace root (generated by
`mgit init`, refreshed by `mgit upgrade`) teaches agents the loop: `brief`
first, `note` decisions as they happen, `plan --status`/`--done` before the
session ends, `commit -f .` (auto-journaled). Memory survives context-window
resets by construction.

**Durable skills.** `mgit skill distill` mines the journals for lessons that
outlive the feature, gates them behind the n=2 rule and your explicit approval,
then advertises the approved set in the ambient brief above. Working memory
carries a feature; skills carry the workspace.

**Machine contract.**

```bash
mgit context                          # JSON: workspace state, repos, features, memory
mgit context -f .                     # deep read of one feature incl. live git facts
mgit feature brief --json             # everything above as one envelope
mgit status --json -f .               # all bulk ops take --json
```

- Envelope: `{"mgit_schema": 1, "ok": bool, "command": str, "data": {...}, "error": null|{...}}`
- Exit codes: `0` success · `1` bulk partial failure · `2` domain error · `3` usage error · `4` internal error
- No tracebacks, ever; errors are typed objects under `--json`
- Prompts refuse non-TTY sessions with instructions instead of hanging

```bash
# Typical agent flow
mgit feature brief                    # re-orient (or rely on the ambient CLAUDE.md)
mgit feature start my-task -r repo-a -r repo-b --materialize --no-carry
# agent edits files in .mgit/worktrees/my-task/repo-a/ and repo-b/
mgit checkpoint save --label "before refactor"
mgit commit -m "implement feature" -f .
mgit feature plan --status "done, awaiting review" 
mgit push -f .
```

## Command reference

### Workspace

| Command | Description |
|---|---|
| `mgit init [NAME]` | Initialize a workspace (scans for repos, generates AGENT.md/AGENTS.md) |
| `mgit upgrade [--fix]` | Refresh generated files + health-check for legacy artifacts; `--fix` applies the safe repairs |
| `mgit remove [--force]` | Remove mgit metadata (.mgit/, AGENT.md, AGENTS.md), keeps repos |
| `mgit context [--pretty] [-f <name\|.>]` | Workspace state as JSON; `-f` for a deep single-feature read |

### Repos

| Command | Description |
|---|---|
| `mgit repo add <url\|path> [--name ALIAS]` | Clone a remote repo or link a local one |
| `mgit repo remove <name>` | Unregister a repo (files are kept) |
| `mgit repo list` | List all registered repos |
| `mgit repo setup <name> [CMD] [--clear]` | Show, set, or clear a repo's post-materialization setup command |

### Features

| Command | Description |
|---|---|
| `mgit feature start <name> [-r REPO]... [-m]` | Create/enroll repos, optionally materialize worktrees (`-m`) |
| `mgit feature materialize [REPO] [--carry]` | Materialize a worktree for a repo in the active feature |
| `mgit feature sync` | Discover dirty repos, enroll + materialize them into the active feature |
| `mgit feature fork <child> [--from PARENT] [--carry-wip]` | Fork a feature: pinned base SHAs, inherited memory |
| `mgit feature tree [--json]` | Feature ancestry view (`*` marks active) |
| `mgit feature switch <name>` | Set the active feature |
| `mgit feature activate <name>` | Set active feature (alias, no side effects) |
| `mgit feature deactivate` | Clear active feature |
| `mgit feature show <name>` | Show feature details (enrolled repos, worktree status) |
| `mgit feature list` | List all features (`*` marks active) |
| `mgit feature publish [--title T] [--draft] [--json]` | Push + open linked PRs/MRs (gh/glab auto-detected per repo) |
| `mgit feature checks [--json]` | CI/review/merge status of the feature's PRs across repos |
| `mgit feature delete <name> [--force]` | Delete a feature: worktrees, sandbox branches, memory, checkpoints. Refuses if a worktree holds uncommitted or unmerged work |
| `mgit feature remove-repo <feature> <repo> [--force]` | Remove a repo from a feature — **destroys its worktree and sandbox branch**; same refusal as `delete` |

### Working memory

| Command | Description |
|---|---|
| `mgit feature brief [-f NAME] [--json] [--full] [--refresh]` | One-call re-orientation: plan + journal tail + live git facts |
| `mgit feature note TEXT [--type note\|decision\|convention\|question\|handoff]` | Append an agent-authored journal entry |
| `mgit feature plan [--goal] [--status] [--next]... [--add-next]... [--done N]... [--ask]... [--resolve N]...` | Read/update the structured plan. `--done`/`--resolve` are repeatable and index the list as you last saw it |
| `mgit feature log [-n N] [--type TYPE] [--commits] [--json]` | Read the journal; `--commits` interleaves git commits across repos |

### Checkpoints

| Command | Description |
|---|---|
| `mgit checkpoint save [--label TEXT] [-f NAME] [--json]` | Pin HEAD + snapshot WIP per materialized repo (non-destructive) |
| `mgit checkpoint restore <id> [-f NAME]` | Restore code + memory; always auto-saves a safety backup first |
| `mgit checkpoint list / show <id> / delete <id>` | Inspect or drop checkpoints (delete unpins refs so gc can reclaim) |

### Skills

| Command | Description |
|---|---|
| `mgit skill distill [-f NAME] [--dry-run] [--json]` | Mine durable skills from journals; `--dry-run` prints the prompt and calls nothing |
| `mgit skill drafts [--json]` | List drafts awaiting review |
| `mgit skill show <slug> [--json]` | Print a draft's full SKILL.md |
| `mgit skill approve <slug> [--run-verify] [--json]` | Promote a draft into the active set; `--run-verify` refuses approval if its `verify_command` fails |
| `mgit skill reject <slug> --reason TEXT [--json]` | Tombstone a draft so it is never re-proposed |
| `mgit skill list [--json]` | List active skills advertised in the ambient brief |
| `mgit skill doctor [--json]` | Health report: active skills, drafts, parked backlog |

### Bulk operations

| Command | Description |
|---|---|
| `mgit status` | Git status across repos |
| `mgit pull` | Pull across repos |
| `mgit push` | Push across repos |
| `mgit commit -m <msg>` | Commit across repos (stages all changes) |
| `mgit exec <cmd...>` | Run an arbitrary command in each repo |
| `mgit setup` | Run each repo's configured setup command (skips repos with none, or with no worktree) |

All bulk commands support:

- `--feature / -f <name>` — scope to repos in a feature (use `.` for active feature)
- `--repos / -r <a,b,...>` — scope to specific repos
- `--fail-fast` — stop on first failure

### Conventions

- **Worktrees**: each feature gets isolated directories at `.mgit/worktrees/<feature>/<repo>/`
- **Sandbox branches**: `mgit/<feature-name>` — created locally in worktrees, never pushed as-is
- **Remote branches**: created at push time, named after the feature's target branch
- **Active feature**: set automatically by `start`/`switch`/`fork` (skip with `--no-activate`), use `-f .` as shorthand in bulk commands
- **Feature resolution** (parallel-session safe): `-f <name>` > `$MGIT_FEATURE` > the worktree cwd is inside > the active pointer — sessions running inside their feature's worktree never cross-talk through the shared active file
- **Lazy by default**: `feature start` enrolls repos as metadata only; use `--materialize` or `feature materialize` to create worktrees
- **Carry**: `--carry` moves uncommitted changes from the original repo into the feature worktree
- **Memory sidecar**: plan + journal live at `.mgit/features/<feature>/` (plain TOML/JSONL); the journal is append-only
- **Forks**: lineage (`parent`, pinned `fork_base` SHAs) lives in the feature file, never in branch names; forked children push to their own remote branch by default
- **Checkpoints**: manifests at `.mgit/checkpoints/<feature>/`, snapshots pinned by `refs/mgit/checkpoint/...` (gc-immune, never pushed)
- **Nothing destroys work silently**: `feature delete` and `feature remove-repo` refuse when a worktree has uncommitted changes or unmerged sandbox commits. Pass `--force` and mgit first pins everything — tracked, staged, and untracked — to `refs/mgit/rescue/<feature>/<repo>` in the origin repo. That ref is gc-immune, survives the worktree, and is the one thing `mgit upgrade --fix` will never delete. Recover with `git checkout <ref> -- .`, release with `git update-ref -d <ref>`
- **Agent files are shared**: `AGENTS.md` is a cross-tool standard (Next.js and others write to it). mgit owns only the region between `<!-- BEGIN:mgit -->` and `<!-- END:mgit -->`, merging into whatever else is in the file and never overwriting it. `mgit remove` strips that block and leaves the rest
- **Worktrees start without ignored files**: a materialized worktree has no `node_modules` or `.venv`. When mgit sees a dependency manifest and no setup command, it says so and suggests `mgit repo setup <repo> "<install cmd>"`
- **One agent per feature**: the plan file is last-writer-wins (the journal preserves overwritten history); fork a variant instead of sharing a feature
- **Skills**: everything under `.mgit/skills/` — `drafts/` awaiting review, `active/` advertised in the ambient brief, plus `parked.jsonl` (n=1 candidates), `tombstones.jsonl` (rejected, never re-proposed), `ledger.jsonl` (decisions). Only `mgit skill approve` makes a skill visible to agents
- **Upgrading from an older mgit**: behavior fixes apply the moment the binary is upgraded — on-disk state is additive and lazy, old feature files load unchanged. Run `mgit upgrade` once per workspace: it refreshes mgit's block in AGENT.md/AGENTS.md and the ambient briefs, then health-checks for state older versions left behind (bogus repo registrations, orphaned sandbox branches/refs of deleted features, mixed target branches, orphaned sidecars); `mgit upgrade --fix` applies the mechanical repairs and reports the rest with manual remedies. Behavior changes: usage errors now exit `3` (was `1`); carry prompts refuse non-TTY sessions instead of hanging; `feature delete`/`remove-repo` refuse to destroy unsaved work without `--force`; and an `AGENTS.md` you or another tool wrote is merged into rather than overwritten

## Requirements

- Python 3.11+
- Git
- `gh` / `glab` — only for `mgit feature publish` and `mgit feature checks`
- `claude` — only for `mgit skill distill`

## Development

```bash
git clone https://github.com/abandiDev/mgit.git
cd mgit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
