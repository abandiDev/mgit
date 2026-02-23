# mgit

Multi-repo git workspace manager. Coordinate branches, features, and bulk operations across multiple git repositories from a single CLI. Designed for AI coding agents and human developers alike.

## Why mgit?

Modern projects often span multiple repositories — a frontend app, a backend API, shared libraries, infrastructure configs. Making a cross-cutting change means juggling branches, stashing work, and remembering which repos you touched. mgit gives you a single command layer on top of plain git repos so you can:

- **Create features that span repos** — one command enrolls repos, creates sandbox branches, and sets up isolated worktrees
- **Carry uncommitted work** into a feature without losing anything
- **Bulk commit, push, and pull** across repos scoped to a feature
- **Context-switch instantly** between features without branch gymnastics
- **Discover dirty repos** across your workspace and bring them into a feature

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
mgit feature work user-service
# user-service: materialized -> .mgit/worktrees/auth-refactor/user-service/

# If the repo has uncommitted changes, carry them into the worktree
mgit feature work api-gateway --carry
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

## Agent workflow

AI coding agents can use `mgit context` for machine-readable workspace discovery:

```bash
mgit context                          # JSON: workspace state, repos, active feature
mgit context --pretty                 # human-readable JSON

# Typical agent flow
mgit feature start my-task -r repo-a -r repo-b --materialize
# agent edits files in .mgit/worktrees/my-task/repo-a/ and repo-b/
mgit commit -m "implement feature" -f .
mgit push -f .
```

## Command reference

### Workspace

| Command | Description |
|---|---|
| `mgit init [NAME]` | Initialize a workspace (scans for repos, generates AGENT.md) |
| `mgit remove [--force]` | Remove mgit metadata (.mgit/ and AGENT.md), keeps repos |
| `mgit context [--pretty]` | Output workspace state as JSON |

### Repos

| Command | Description |
|---|---|
| `mgit repo add <url\|path> [--name ALIAS]` | Clone a remote repo or link a local one |
| `mgit repo remove <name>` | Unregister a repo (files are kept) |
| `mgit repo list` | List all registered repos |

### Features

| Command | Description |
|---|---|
| `mgit feature start <name> [-r REPO]... [-m]` | Create/enroll repos, optionally materialize worktrees (`-m`) |
| `mgit feature work [REPO] [--carry]` | Materialize a worktree for a repo in the active feature |
| `mgit feature sync` | Discover dirty repos, enroll + materialize them into the active feature |
| `mgit feature switch <name>` | Set the active feature |
| `mgit feature activate <name>` | Set active feature (alias, no side effects) |
| `mgit feature deactivate` | Clear active feature |
| `mgit feature show <name>` | Show feature details (enrolled repos, worktree status) |
| `mgit feature list` | List all features (`*` marks active) |
| `mgit feature delete <name>` | Delete a feature and clean up its worktrees |
| `mgit feature remove-repo <feature> <repo>` | Remove a repo from a feature |

### Bulk operations

| Command | Description |
|---|---|
| `mgit status` | Git status across repos |
| `mgit pull` | Pull across repos |
| `mgit push` | Push across repos |
| `mgit commit -m <msg>` | Commit across repos (stages all changes) |
| `mgit exec <cmd...>` | Run an arbitrary command in each repo |

All bulk commands support:

- `--feature / -f <name>` — scope to repos in a feature (use `.` for active feature)
- `--repos / -r <a,b,...>` — scope to specific repos
- `--fail-fast` — stop on first failure

### Conventions

- **Worktrees**: each feature gets isolated directories at `.mgit/worktrees/<feature>/<repo>/`
- **Sandbox branches**: `mgit/<feature-name>` — created locally in worktrees, never pushed as-is
- **Remote branches**: created at push time, named after the feature's target branch
- **Active feature**: set automatically by `start`/`switch`, use `-f .` as shorthand in bulk commands
- **Lazy by default**: `feature start` enrolls repos as metadata only; use `--materialize` or `feature work` to create worktrees
- **Carry**: `--carry` moves uncommitted changes from the original repo into the feature worktree

## Requirements

- Python 3.11+
- Git

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
