"""Workspace class: find root, init, load/save config, scan repos."""

from __future__ import annotations

from pathlib import Path

from mgit.core import config, git
from mgit.models.types import RepoInfo
from mgit.utils.errors import (
    RepoExistsError,
    RepoNotFoundError,
    WorkspaceExistsError,
    WorkspaceNotFoundError,
)

MGIT_DIR = ".mgit"
CONFIG_FILE = "config.toml"
FEATURES_DIR = "features"
WORKTREES_DIR = "worktrees"
CHECKPOINTS_DIR = "checkpoints"
SKILLS_DIR = "skills"
ACTIVE_FILE = "active"

AGENT_MD_TEMPLATE = """\
# mgit Workspace

This is an mgit multi-repo workspace. Use `mgit` commands to manage repos and features.

## Quick Reference

Run `mgit context` for full workspace state as JSON (`mgit context -f .` for a
deep read of the active feature, including live per-repo git facts).

### Feature Workflow
```
mgit feature start <name> -r <repo1> -r <repo2>    # Enroll specific repos (metadata only)
mgit feature start <name> -r <repo1> --materialize  # Enroll and create worktrees immediately
mgit feature materialize <repo>                     # Materialize a worktree on demand
mgit feature materialize <repo> --carry             # Materialize and carry uncommitted changes
mgit feature sync                                   # Discover dirty repos, enroll + materialize them
mgit repo setup <repo> "npm install"                # Set per-repo setup command
mgit setup -f .                                     # Run setup commands across feature repos
mgit status -f .                                    # Status across feature repos
mgit commit -m "message" -f .                       # Commit across materialized repos (auto-journaled)
mgit push -f .                                      # Push feature branches to remote (auto-journaled)
mgit feature switch <name>                          # Set active feature
```

### Working Memory (the agent ritual)
Every feature keeps a persistent working memory: a plan file plus an
append-only journal that mgit populates automatically at every lifecycle step
(enroll, materialize, sync, commit, push, fork). A fresh session recovers
everything with one call.

```
mgit feature brief                                  # FIRST CALL of a session: memory + journal + live git facts
mgit feature plan --goal "..." --status "..."       # Keep the plan current
mgit feature plan --add-next "..." --done 1         # Manage next steps
mgit feature note "..." --type decision             # Record decisions as you make them
mgit feature note "..." --type handoff              # Leave a handoff brief before ending a session
mgit feature log -n 30                              # Read further back; --commits interleaves git commits
```

Before ending a session: update `plan --status` and check off finished steps.
The memory survives context-window resets — the next session re-orients from it.
A generated CLAUDE.md/AGENTS.md with this brief sits at `.mgit/worktrees/<feature>/`
(an ancestor of every worktree), so harness sessions launched inside a worktree
load it automatically. Never edit those files by hand.

### Branching Features
```
mgit feature fork <child>                           # Fork the active feature: pinned base SHAs, inherited memory
mgit feature fork <child> --carry-wip -m            # Also carry uncommitted work into the child
mgit feature tree                                   # Ancestry view of all features
```
A fork enrolls the parent's repos, pins each repo's current sandbox tip as the
child's branch point, and copies the parent's memory. The child pushes to its
own remote branch by default (pass --branch to fold back into the parent's).

### Checkpoints (cross-repo save-points)
```
mgit checkpoint save --label "before risky refactor"  # Pin HEAD + snapshot WIP in every repo (non-destructive)
mgit checkpoint list                                   # What can I roll back to?
mgit checkpoint restore <cp-id>                        # Restore code + memory; auto-saves a safety backup first
```
Checkpoint before risky multi-repo changes. Restore runs `git clean -fd` in
worktrees — un-gitignored artifacts are removed (the automatic safety
checkpoint preserves everything tracked or untracked-but-not-ignored).

### Delivery
```
mgit feature publish                                # Push + open linked PRs/MRs (gh/glab auto-detected per repo)
mgit feature checks                                 # CI/review/merge status of the feature's PRs in one call
```
PR bodies are generated from the working memory and cross-link sibling PRs;
re-publishing is idempotent (pushes new commits, updates existing PRs).

### Learned Skills (durable memory across features)
Working memory is per-feature and transient. `mgit skill` distills the *durable*
lessons out of the journals — recurring decisions, conventions, handoffs — into
reviewable skills that, once approved, appear in every feature's ambient brief.
```
mgit skill distill                                  # Mine durable skills from steering across all features
mgit skill drafts                                   # Drafts awaiting review
mgit skill show <slug>                              # Read a draft's full SKILL.md
mgit skill approve <slug> [--run-verify]            # Promote a draft; --run-verify runs its check first
mgit skill reject <slug> --reason "..."             # Tombstone it (never re-proposed)
mgit skill list                                     # Active skills (also shown in the ambient brief)
mgit skill doctor                                   # Backlog: drafts, parked candidates, unverified skills
```
Distillation calls the local `claude` CLI once (configure under `[skills]` in
`.mgit/config.toml`: `claude_bin`, `model`, `allow_auto_verify`). A model-written
`verify_command` never runs during distill unless `allow_auto_verify=true`; by
default it runs only at `mgit skill approve --run-verify`, under your eye.
Nothing becomes an active skill without explicit `approve`.

### Agent Contract
- Exit codes: 0 success | 1 bulk partial failure | 2 domain error | 3 usage error | 4 internal error
- `--json` on context/brief/plan/log/tree/checkpoint/bulk commands emits one
  envelope: `{"mgit_schema": 1, "ok": bool, "command": str, "data": {...}, "error": null|{...}}`
- Errors never print tracebacks; with `--json` they arrive as typed error objects
- Prompts refuse non-TTY sessions with instructions (pass `--carry`/`--no-carry`)

### Conventions
- `feature start` enrolls repos as metadata — pass `--materialize` to create worktrees immediately
- `feature materialize <repo>` materializes a single repo's worktree on demand
- `feature sync` finds dirty repos in the workspace and enrolls them into the active feature
- `repo setup` configures a per-repo setup command (e.g. `npm install`) that runs after worktree materialization
- Bulk commands (`status`, `pull`, `push`, `commit`, `exec`, `setup`) skip unmaterialized repos
- Each feature gets isolated worktree directories at `.mgit/worktrees/<feature>/<repo>/`
- Sandbox branches: `mgit/<feature-name>` (local only, created automatically)
- Remote branches: created at push time, named after the feature
- Scope bulk commands with `-f <feature>` or `-f .` (active feature)
- Multiple agents can work on different features concurrently (each in its own worktree)
- One agent per feature: the plan file is last-writer-wins (plan_updated
  journal events carry the full state, so overwrites are recoverable); fork a
  variant instead of sharing one feature

### Parallel Sessions (IMPORTANT)
Feature resolution priority: `-f <name>` > `$MGIT_FEATURE` > the worktree you
are inside > the workspace-global active pointer. The active pointer is a
single-session convenience — another session's `feature start/switch/fork`
moves it. To stay isolated:
- Run each session from inside its feature's worktree directory
  (`.mgit/worktrees/<feature>/...`) — commands resolve to that feature
  automatically; or
- `export MGIT_FEATURE=<name>` once per session; or pass `-f <name>` explicitly
- Use `--no-activate` on `feature start`/`fork` in sessions that must not
  move the shared pointer
- Git is the source of truth for code state — mgit never stores dirty flags or
  ahead counts, it computes them live
"""


class Workspace:
    """Represents an mgit workspace."""

    def __init__(self, root: Path):
        self.root = root
        self.mgit_dir = root / MGIT_DIR
        self.config_path = self.mgit_dir / CONFIG_FILE
        self.features_dir = self.mgit_dir / FEATURES_DIR
        self.worktrees_dir = self.mgit_dir / WORKTREES_DIR
        self.checkpoints_dir = self.mgit_dir / CHECKPOINTS_DIR
        self.skills_dir = self.mgit_dir / SKILLS_DIR
        self.name: str = root.name
        self.repos: dict[str, RepoInfo] = {}

    @classmethod
    def find(cls, start: Path | None = None) -> Workspace:
        """Walk up from start (default: cwd) to find a workspace root."""
        current = (start or Path.cwd()).resolve()
        while True:
            if (current / MGIT_DIR).is_dir():
                ws = cls(current)
                ws.load()
                return ws
            parent = current.parent
            if parent == current:
                raise WorkspaceNotFoundError(
                    "No mgit workspace found. Run 'mgit init' to create one."
                )
            current = parent

    @classmethod
    def init(
        cls,
        path: Path,
        name: str | None = None,
        scan: bool = True,
        auto_add: bool = False,
    ) -> tuple[Workspace, list[RepoInfo]]:
        """Initialize a new workspace.

        Args:
            path: Directory to initialize in (created if it doesn't exist).
            name: Workspace name (defaults to directory name).
            scan: Whether to scan for existing git repos.
            auto_add: If True, automatically add all found repos.

        Returns:
            Tuple of (workspace, found_repos). Caller decides which to register.
        """
        path = path.resolve()
        if (path / MGIT_DIR).is_dir():
            raise WorkspaceExistsError(
                f"Workspace already exists at {path}"
            )

        path.mkdir(parents=True, exist_ok=True)
        ws = cls(path)
        ws.name = name or path.name
        ws.mgit_dir.mkdir()
        ws.features_dir.mkdir()
        ws.worktrees_dir.mkdir()
        ws.save()

        # Generate AGENT.md (+ AGENTS.md, the cross-tool standard) in workspace root
        ws.write_agent_md()

        found_repos: list[RepoInfo] = []
        if scan:
            found_repos = ws.scan_repos()
            if auto_add:
                for repo in found_repos:
                    ws.repos[repo.name] = repo
                ws.save()

        return ws, found_repos

    def scan_repos(self) -> list[RepoInfo]:
        """Scan immediate subdirectories for git repos."""
        found = []
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # Follow symlinks for the git check
            actual = child.resolve()
            # The child must BE a repo root, not merely sit inside one — a
            # workspace nested in some outer git repo must not register every
            # plain subdirectory as a "repo" of the outer repository
            if git.repo_root(actual) != actual:
                continue
            branch = git.get_current_branch(actual)
            url = git.get_remote_url(actual)
            found.append(RepoInfo(
                name=child.name,
                path=child.name,
                url=url,
                default_branch=branch,
            ))
        return found

    def load(self) -> None:
        """Load workspace config from disk."""
        if not self.config_path.exists():
            return
        data = config.read_toml(self.config_path)
        self.name = data.get("workspace", {}).get("name", self.root.name)
        self.repos = config.dict_to_repos(data.get("repos", {}))

    def save(self) -> None:
        """Save workspace config to disk."""
        data = config.workspace_config_to_dict(self.name, self.repos)
        config.write_toml(self.config_path, data)

    def add_repo(self, repo: RepoInfo) -> None:
        """Register a repo in the workspace."""
        if repo.name in self.repos:
            raise RepoExistsError(f"Repo '{repo.name}' already exists in workspace")
        self.repos[repo.name] = repo
        self.save()

    def remove_repo(self, name: str) -> RepoInfo:
        """Unregister a repo from the workspace."""
        if name not in self.repos:
            raise RepoNotFoundError(f"Repo '{name}' not found in workspace")
        repo = self.repos.pop(name)
        self.save()
        return repo

    def get_repo(self, name: str) -> RepoInfo:
        """Get a registered repo by name."""
        if name not in self.repos:
            raise RepoNotFoundError(f"Repo '{name}' not found in workspace")
        return self.repos[name]

    def repo_path(self, name: str) -> Path:
        """Get the absolute path to a repo."""
        repo = self.get_repo(name)
        p = self.root / repo.path
        # Resolve symlinks for git operations
        return p.resolve() if p.is_symlink() else p

    def worktree_path(self, feature_name: str, repo_name: str) -> Path:
        """Get the worktree path for a repo within a feature."""
        return self.worktrees_dir / feature_name / repo_name

    # --- Active feature tracking ---

    def get_active_feature(self) -> str | None:
        """Read the active feature name from .mgit/active, or None."""
        active_path = self.mgit_dir / ACTIVE_FILE
        if active_path.exists():
            text = active_path.read_text().strip()
            return text if text else None
        return None

    def set_active_feature(self, name: str) -> None:
        """Write the active feature name to .mgit/active."""
        active_path = self.mgit_dir / ACTIVE_FILE
        active_path.write_text(name)

    def clear_active_feature(self) -> None:
        """Delete .mgit/active."""
        active_path = self.mgit_dir / ACTIVE_FILE
        if active_path.exists():
            active_path.unlink()

    def write_agent_md(self, backup: bool = False) -> list[str]:
        """Write AGENT.md and AGENTS.md from the current template.

        With backup=True, a locally modified AGENT.md is copied to
        AGENT.md.bak before being overwritten (used by 'mgit upgrade').

        Returns the names of the files written.
        """
        written = []
        for fname in ("AGENT.md", "AGENTS.md"):
            path = self.root / fname
            if backup and path.exists() and path.read_text() != AGENT_MD_TEMPLATE:
                (self.root / f"{fname}.bak").write_text(path.read_text())
            path.write_text(AGENT_MD_TEMPLATE)
            written.append(fname)
        return written

    def remove(self) -> None:
        """Remove the mgit workspace metadata (.mgit/, AGENT.md, AGENTS.md).

        Only removes mgit's own files — repos and other user files are untouched.
        """
        import shutil

        if self.mgit_dir.exists():
            shutil.rmtree(self.mgit_dir)

        for fname in ("AGENT.md", "AGENTS.md"):
            agent_md = self.root / fname
            if agent_md.exists():
                agent_md.unlink()

    def detect_feature_from_cwd(self, cwd: Path | None = None) -> str | None:
        """Feature whose worktree tree contains cwd, or None.

        Being inside .mgit/worktrees/<feature>/... unambiguously identifies
        the feature this session is working on.
        """
        target = (cwd or Path.cwd()).resolve()
        try:
            rel = target.relative_to(self.worktrees_dir.resolve())
        except ValueError:
            return None
        if not rel.parts:
            return None
        name = rel.parts[0]
        if (self.features_dir / f"{name}.toml").exists():
            return name
        return None

    def resolve_feature(self, explicit: str | None = None) -> str | None:
        """Resolve which feature a command applies to.

        Priority: explicit -f value > $MGIT_FEATURE > the worktree cwd is
        inside > the workspace-global active pointer. The active pointer is a
        single-session convenience: parallel sessions on different features
        stay isolated as long as each runs inside its feature's worktree or
        pins MGIT_FEATURE.
        """
        import os

        if explicit and explicit != ".":
            return explicit
        env = os.environ.get("MGIT_FEATURE")
        if env:
            return env
        return self.detect_feature_from_cwd() or self.get_active_feature()

    def detect_repo_from_cwd(self, cwd: Path | None = None) -> str | None:
        """Determine which registered repo contains cwd (deepest match).

        Also recognizes feature worktrees: inside
        .mgit/worktrees/<feature>/<repo>/... the repo is <repo>.

        Returns the repo name, or None if cwd is not inside any registered repo.
        """
        target = (cwd or Path.cwd()).resolve()

        try:
            rel = target.relative_to(self.worktrees_dir.resolve())
            if len(rel.parts) >= 2 and rel.parts[1] in self.repos:
                return rel.parts[1]
        except ValueError:
            pass
        best_match: str | None = None
        best_depth = -1

        for name, repo_info in self.repos.items():
            repo_abs = (self.root / repo_info.path).resolve()
            try:
                target.relative_to(repo_abs)  # raises unless cwd is inside this repo
                # Prefer the deepest match — the most specific repo path.
                if best_match is None or len(repo_abs.parts) > best_depth:
                    best_match = name
                    best_depth = len(repo_abs.parts)
            except ValueError:
                continue

        return best_match
