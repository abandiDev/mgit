# mgit

Multi-repo git workspace manager. Coordinate branches, features, and bulk operations across multiple git repositories from a single CLI.

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

# Create a cross-repo feature
mgit feature create auth-refactor \
  -r service-a:feature/auth-refactor \
  -r service-b:feature/auth-refactor

# Switch all feature repos to their branches
mgit feature switch auth-refactor

# Check status across all repos
mgit status

# Bulk operations
mgit pull
mgit commit -m "WIP: auth changes"
mgit push
```

## Command reference

| Command | Description |
|---|---|
| `mgit init [NAME]` | Initialize a workspace (optionally in a new directory) |
| `mgit repo add <url\|path>` | Clone a remote repo or link a local one |
| `mgit repo remove <name>` | Unregister a repo (files are kept) |
| `mgit repo list` | List all registered repos |
| `mgit feature create <name> -r repo:branch ...` | Create a cross-repo feature |
| `mgit feature delete <name>` | Delete a feature |
| `mgit feature list` | List all features |
| `mgit feature show <name>` | Show feature details |
| `mgit feature switch <name>` | Switch repos to feature branches |
| `mgit feature add-repo <feature> <repo:branch>` | Add a repo to a feature |
| `mgit feature remove-repo <feature> <repo>` | Remove a repo from a feature |
| `mgit status` | Git status across all repos |
| `mgit pull` | Pull across all repos |
| `mgit push` | Push across all repos |
| `mgit commit -m <msg>` | Commit across all repos |
| `mgit exec <cmd...>` | Run an arbitrary command in each repo |

All bulk commands (`status`, `pull`, `push`, `commit`, `exec`) support:

- `--feature / -f <name>` &mdash; scope to repos in a feature
- `--repos / -r <a,b,...>` &mdash; scope to specific repos
- `--fail-fast` &mdash; stop on first failure

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
