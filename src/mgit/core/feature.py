"""FeatureManager: CRUD operations and worktree-based workflow for cross-repo features."""

from __future__ import annotations

import shutil
from pathlib import Path

from mgit.core import config, git
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import FeatureInfo
from mgit.utils.errors import (
    FeatureExistsError,
    FeatureNotFoundError,
    RepoNotFoundError,
)


def sandbox_branch(feature_name: str) -> str:
    """Return the sandbox branch name for a feature."""
    return f"mgit/{feature_name}"


def find_dirty_repos(ws: Workspace, repo_names: list[str]) -> list[tuple[str, str]]:
    """Find repos with uncommitted changes.

    Returns list of (repo_name, current_branch) tuples.
    """
    dirty = []
    for name in repo_names:
        if name not in ws.repos:
            continue
        repo = Repo(ws.get_repo(name), ws.root)
        if repo.is_dirty():
            dirty.append((name, repo.current_branch()))
    return dirty


class FeatureManager:
    """Manages feature lifecycle within a workspace."""

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def _feature_path(self, name: str) -> Path:
        return self.ws.features_dir / f"{name}.toml"

    def _save_feature(self, feature: FeatureInfo) -> None:
        config.write_toml(
            self._feature_path(feature.name),
            config.feature_to_dict(feature),
        )

    def create(
        self,
        name: str,
        description: str = "",
    ) -> FeatureInfo:
        """Create a new empty feature definition.

        Args:
            name: Feature name (used as filename).
            description: Optional description.

        Raises:
            FeatureExistsError: If feature already exists.
        """
        path = self._feature_path(name)
        if path.exists():
            raise FeatureExistsError(f"Feature '{name}' already exists")

        feature = FeatureInfo(
            name=name,
            description=description,
            branches={},
        )
        config.write_toml(path, config.feature_to_dict(feature))
        return feature

    def delete(self, name: str) -> None:
        """Delete a feature definition and clean up its worktrees."""
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")

        # Remove worktrees for all enrolled repos
        feature = self.get(name)
        for repo_name in feature.branches:
            wt_path = self.ws.worktree_path(name, repo_name)
            if wt_path.exists():
                try:
                    repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                    repo.remove_worktree(wt_path)
                except Exception:
                    # If git worktree remove fails, clean up manually
                    shutil.rmtree(wt_path, ignore_errors=True)

        # Clean up feature's worktree directory
        feature_wt_dir = self.ws.worktrees_dir / name
        if feature_wt_dir.exists():
            shutil.rmtree(feature_wt_dir, ignore_errors=True)

        # Delete feature file
        path.unlink()

        # Clear active if this was the active feature
        if self.ws.get_active_feature() == name:
            self.ws.clear_active_feature()

    def get(self, name: str) -> FeatureInfo:
        """Load a feature by name."""
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")
        data = config.read_toml(path)
        return config.dict_to_feature(data)

    def list(self) -> list[FeatureInfo]:
        """List all features."""
        features = []
        if not self.ws.features_dir.exists():
            return features
        for path in sorted(self.ws.features_dir.glob("*.toml")):
            data = config.read_toml(path)
            features.append(config.dict_to_feature(data))
        return features

    def remove_repo(self, feature_name: str, repo_name: str) -> FeatureInfo:
        """Remove a repo from a feature and clean up its worktree."""
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not in feature '{feature_name}'"
            )

        # Remove worktree
        wt_path = self.ws.worktree_path(feature_name, repo_name)
        if wt_path.exists():
            try:
                repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                repo.remove_worktree(wt_path)
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)

        del feature.branches[repo_name]
        self._save_feature(feature)
        return feature

    def start(
        self,
        feature_name: str,
        repo_names: list[str] | None = None,
        target_branch: str | None = None,
        description: str = "",
        materialize: bool = False,
    ) -> tuple[FeatureInfo, list[str]]:
        """Start working on a feature: create if needed, enroll repos.

        Args:
            feature_name: Name of the feature.
            repo_names: List of repo names to enroll. None = enroll all workspace repos.
            target_branch: Remote target branch (default = feature name).
            description: Description (only used on initial creation).
            materialize: If True, create worktrees for newly enrolled repos
                immediately, auto-carrying dirty changes.

        Returns:
            Tuple of (feature, list_of_newly_added_repo_names).
        """
        target = target_branch or feature_name

        # Default to all workspace repos
        names = repo_names if repo_names is not None else list(self.ws.repos.keys())

        # Get or create feature
        try:
            feature = self.get(feature_name)
        except FeatureNotFoundError:
            feature = self.create(feature_name, description=description)

        newly_added: list[str] = []

        for repo_name in names:
            # Validate repo exists
            if repo_name not in self.ws.repos:
                raise RepoNotFoundError(
                    f"Repo '{repo_name}' not found in workspace"
                )

            # Enroll in feature if not already
            if repo_name not in feature.branches:
                feature.branches[repo_name] = target
                newly_added.append(repo_name)

        # Save feature state
        self._save_feature(feature)

        # Set as active feature
        self.ws.set_active_feature(feature_name)

        # Materialize worktrees for newly enrolled repos if requested
        if materialize and newly_added:
            dirty_set = {
                name for name, _ in find_dirty_repos(self.ws, newly_added)
            }
            for repo_name in newly_added:
                self.work(feature_name, repo_name, carry=repo_name in dirty_set)

        return feature, newly_added

    def work(self, feature_name: str, repo_name: str, carry: bool = False) -> Path:
        """Materialize a single repo's worktree for an existing feature.

        Args:
            feature_name: Name of the feature.
            repo_name: Name of the repo to materialize.
            carry: If True, stash uncommitted changes from original repo
                and pop them into the new worktree.

        Returns:
            Path to the worktree directory.

        Raises:
            FeatureNotFoundError: If feature doesn't exist.
            RepoNotFoundError: If repo is not enrolled in the feature.
        """
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not enrolled in feature '{feature_name}'"
            )

        wt_path = self.ws.worktree_path(feature_name, repo_name)

        # Idempotent: if already materialized, return existing path
        if wt_path.exists():
            return wt_path

        sb = sandbox_branch(feature_name)
        repo = Repo(self.ws.get_repo(repo_name), self.ws.root)

        # Stash changes before creating worktree if carrying
        stashed = False
        if carry:
            stashed = repo.stash_push(f"mgit-carry:{feature_name}")

        wt_path.parent.mkdir(parents=True, exist_ok=True)
        repo.add_worktree(wt_path, sb)

        # Pop stash in the worktree if we stashed something
        if stashed:
            git.run_git("stash", "pop", cwd=wt_path)

        return wt_path

    def sync(self, feature_name: str) -> list[str]:
        """Discover dirty repos and enroll + materialize them into the feature.

        Scans all workspace repos for uncommitted changes, enrolls any that
        aren't already part of the feature, and materializes worktrees with
        carry=True so changes are moved into the feature worktree.

        Args:
            feature_name: Name of the feature to sync into.

        Returns:
            List of newly synced repo names.
        """
        feature = self.get(feature_name)

        all_repo_names = list(self.ws.repos.keys())
        dirty = find_dirty_repos(self.ws, all_repo_names)

        # Filter out repos already enrolled in the feature
        new_dirty = [(name, branch) for name, branch in dirty if name not in feature.branches]

        synced: list[str] = []
        for repo_name, _ in new_dirty:
            self.start(feature_name, [repo_name])
            self.work(feature_name, repo_name, carry=True)
            synced.append(repo_name)

        return synced

    def is_materialized(self, feature_name: str, repo_name: str) -> bool:
        """Check if a repo's worktree has been materialized for a feature."""
        return self.ws.worktree_path(feature_name, repo_name).exists()

    def switch(self, name: str) -> dict[str, Path]:
        """Set the active feature. Worktrees are always ready.

        Args:
            name: Feature name to switch to.

        Returns:
            Dict of repo_name -> worktree_path for each enrolled repo.
        """
        feature = self.get(name)

        # Set as active feature
        self.ws.set_active_feature(name)

        return self.get_worktree_paths(name)

    def get_worktree_paths(self, feature_name: str) -> dict[str, Path]:
        """Get worktree paths for each enrolled repo in a feature.

        Returns:
            Dict of repo_name -> worktree_path.
        """
        feature = self.get(feature_name)
        paths = {}
        for repo_name in feature.branches:
            paths[repo_name] = self.ws.worktree_path(feature_name, repo_name)
        return paths
