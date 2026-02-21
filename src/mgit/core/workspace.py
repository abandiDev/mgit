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


class Workspace:
    """Represents an mgit workspace."""

    def __init__(self, root: Path):
        self.root = root
        self.mgit_dir = root / MGIT_DIR
        self.config_path = self.mgit_dir / CONFIG_FILE
        self.features_dir = self.mgit_dir / FEATURES_DIR
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
        ws.save()

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
            if not git.is_git_repo(actual):
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
