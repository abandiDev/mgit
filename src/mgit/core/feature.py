"""FeatureManager: CRUD operations for cross-repo features."""

from __future__ import annotations

from pathlib import Path

from mgit.core import config
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import FeatureInfo
from mgit.utils.errors import (
    DirtyTreeError,
    FeatureExistsError,
    FeatureNotFoundError,
    RepoNotFoundError,
)


class FeatureManager:
    """Manages feature lifecycle within a workspace."""

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def _feature_path(self, name: str) -> Path:
        return self.ws.features_dir / f"{name}.toml"

    def create(
        self,
        name: str,
        branches: dict[str, str],
        description: str = "",
    ) -> FeatureInfo:
        """Create a new feature definition.

        Args:
            name: Feature name (used as filename).
            branches: Mapping of repo_name -> branch_name.
            description: Optional description.

        Raises:
            FeatureExistsError: If feature already exists.
            RepoNotFoundError: If a referenced repo is not registered.
        """
        path = self._feature_path(name)
        if path.exists():
            raise FeatureExistsError(f"Feature '{name}' already exists")

        # Validate all repos exist
        for repo_name in branches:
            if repo_name not in self.ws.repos:
                raise RepoNotFoundError(
                    f"Repo '{repo_name}' not found in workspace"
                )

        feature = FeatureInfo(
            name=name,
            description=description,
            branches=branches,
        )
        config.write_toml(path, config.feature_to_dict(feature))
        return feature

    def delete(self, name: str) -> None:
        """Delete a feature definition."""
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")
        path.unlink()

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

    def add_repo(self, feature_name: str, repo_name: str, branch: str) -> FeatureInfo:
        """Add a repo:branch mapping to an existing feature."""
        feature = self.get(feature_name)
        if repo_name not in self.ws.repos:
            raise RepoNotFoundError(f"Repo '{repo_name}' not found in workspace")
        feature.branches[repo_name] = branch
        config.write_toml(
            self._feature_path(feature_name),
            config.feature_to_dict(feature),
        )
        return feature

    def remove_repo(self, feature_name: str, repo_name: str) -> FeatureInfo:
        """Remove a repo from a feature."""
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not in feature '{feature_name}'"
            )
        del feature.branches[repo_name]
        config.write_toml(
            self._feature_path(feature_name),
            config.feature_to_dict(feature),
        )
        return feature

    def switch(self, name: str) -> dict[str, str]:
        """Switch all repos in a feature to their feature branches.

        Pre-flight checks for dirty trees to prevent half-switched state.

        Returns:
            Dict of repo_name -> branch_name that were checked out.

        Raises:
            DirtyTreeError: If any repo has uncommitted changes.
        """
        feature = self.get(name)

        # Pre-flight: check all repos are clean
        dirty = []
        for repo_name in feature.branches:
            repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
            if repo.is_dirty():
                dirty.append(repo_name)

        if dirty:
            raise DirtyTreeError(dirty)

        # All clean - switch branches
        switched = {}
        for repo_name, branch in feature.branches.items():
            repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
            repo.checkout(branch)
            switched[repo_name] = branch

        return switched
