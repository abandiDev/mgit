# mgit

Multi-repo git workspace manager. Coordinate branches, features, and bulk operations across multiple git repositories from a single CLI. Designed for AI coding agents and human developers alike.

## Installation

```bash
pip install git+https://github.com/abhilash-j-a/mgit.git
```

or with [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/abhilash-j-a/mgit.git
```

## Quick start

```bash
# Initialize a workspace (scans for existing repos)
cd ~/projects
mgit init my-workspace
cd my-workspace

# Add repos
mgit repo add https://github.com/org/service-a.git
mgit repo add https://github.com/org/service-b.git

# Start a cross-repo feature (creates sandbox branches automatically)
mgit feature start auth-refactor -r service-a -r service-b

# Edit files across repos...

# Bulk operations scoped to the feature
mgit status -f auth-refactor
mgit commit -m "refactor auth" -f auth-refactor
mgit push -f auth-refactor   # pushes as origin/auth-refactor

# Context-switch to another task (auto-stashes current work)
mgit feature start other-task -r service-a

# Switch back (auto-restores stashed changes)
mgit feature switch auth-refactor
```

## Agent workflow

AI coding agents can use `mgit context` for machine-readable workspace discovery:

```bash
mgit context                                               # JSON: workspace state
mgit feature start auth-refactor -r service-a -r service-b # create + enroll + sandbox
# agent edits files across service-a and service-b...
mgit commit -m "refactor auth" -f auth-refactor            # bulk commit
mgit push -f auth-refactor                                 # pushes as origin/auth-refactor
# need to context-switch?
mgit feature start other-task -r service-a                 # auto-stashes auth-refactor changes
# work on other-task...
mgit feature switch auth-refactor                          # auto-restores stashed changes
```

## Command reference

| Command | Description |
|---|---|
| `mgit init [NAME]` | Initialize a workspace (generates AGENT.md) |
| `mgit repo add <url\|path>` | Clone a remote repo or link a local one |
| `mgit repo remove <name>` | Unregister a repo (files are kept) |
| `mgit repo list` | List all registered repos |
| `mgit context [--pretty]` | Output workspace state as JSON |
| `mgit feature start <name> [-r REPO]...` | Create/enroll repos + switch to sandbox branches |
| `mgit feature create <name> [-d DESC]` | Create an empty feature |
| `mgit feature switch <name>` | Switch repos to feature sandbox branches (auto-stash/unstash) |
| `mgit feature activate <name>` | Set active feature without switching branches |
| `mgit feature deactivate` | Clear active feature |
| `mgit feature show <name>` | Show feature details |
| `mgit feature list` | List all features (`*` marks active) |
| `mgit feature delete <name>` | Delete a feature |
| `mgit feature remove-repo <feature> <repo>` | Remove a repo from a feature |
| `mgit status` | Git status across all repos |
| `mgit pull` | Pull across all repos |
| `mgit push` | Push across all repos |
| `mgit commit -m <msg>` | Commit across all repos |
| `mgit exec <cmd...>` | Run an arbitrary command in each repo |

All bulk commands (`status`, `pull`, `push`, `commit`, `exec`) support:

- `--feature / -f <name>` &mdash; scope to repos in a feature (use `.` for active feature)
- `--repos / -r <a,b,...>` &mdash; scope to specific repos
- `--fail-fast` &mdash; stop on first failure

### Conventions

- **Sandbox branches**: `mgit/<feature-name>` — created locally, never pushed as-is
- **Remote branches**: created at push time, named after the feature's target branch
- **Auto-stash**: dirty changes are stashed when switching features and restored when switching back
- **Active feature**: set automatically by `start`/`switch`, use `-f .` as shorthand in bulk commands

## Requirements

- Python 3.11+
- Git

## Development

```bash
git clone https://github.com/abhilash-j-a/mgit.git
cd mgit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
