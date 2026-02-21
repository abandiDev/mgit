"""FeatureManager: CRUD operations and sandbox workflow for cross-repo features."""

from __future__ import annotations

from pathlib import Path

from mgit.core import config
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
        """Delete a feature definition."""
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")
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
        """Remove a repo from a feature."""
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not in feature '{feature_name}'"
            )
        del feature.branches[repo_name]
        self._save_feature(feature)
        return feature

    def start(
        self,
        feature_name: str,
        repo_names: list[str],
        target_branch: str | None = None,
        description: str = "",
    ) -> tuple[FeatureInfo, list[str]]:
        """Start working on a feature: create if needed, enroll repos, switch branches.

        This is the core operation that handles everything:
        1. If feature doesn't exist -> create it
        2. If feature exists -> load it
        3. For each repo:
           a. Validate repo exists
           b. Auto-stash if dirty and on a different feature's sandbox branch
           c. Enroll in feature if not already
           d. Checkout sandbox branch
           e. Auto-unstash previous work on this feature

        Args:
            feature_name: Name of the feature.
            repo_names: List of repo names to enroll and switch.
            target_branch: Remote target branch (default = feature name).
            description: Description (only used on initial creation).

        Returns:
            Tuple of (feature, list_of_newly_added_repo_names).
        """
        target = target_branch or feature_name
        sb = sandbox_branch(feature_name)

        # 1. Get or create feature
        try:
            feature = self.get(feature_name)
        except FeatureNotFoundError:
            feature = self.create(feature_name, description=description)

        newly_added: list[str] = []

        for repo_name in repo_names:
            # 3a. Validate repo exists
            if repo_name not in self.ws.repos:
                raise RepoNotFoundError(
                    f"Repo '{repo_name}' not found in workspace"
                )

            repo = Repo(self.ws.get_repo(repo_name), self.ws.root)

            # 3b. Auto-stash if dirty and on a DIFFERENT feature's sandbox
            current = repo.current_branch()
            if repo.is_dirty() and current.startswith("mgit/") and current != sb:
                old_feature = current.removeprefix("mgit/")
                repo.stash_push(f"mgit:{old_feature}")

            # 3c. Enroll in feature if not already
            if repo_name not in feature.branches:
                feature.branches[repo_name] = target
                newly_added.append(repo_name)

            # 3d. Checkout sandbox branch
            repo.checkout(sb)

            # 3e. Auto-unstash previous work on this feature
            repo.stash_pop_by_message(f"mgit:{feature_name}")

        # Save feature state
        self._save_feature(feature)

        # Set as active feature
        self.ws.set_active_feature(feature_name)

        return feature, newly_added

    def switch(self, name: str) -> dict[str, str]:
        """Switch all repos in a feature to their sandbox branches.

        Auto-stashes dirty work on the old feature, auto-unstashes work
        on the target feature.

        Returns:
            Dict of repo_name -> sandbox_branch that were checked out.
        """
        feature = self.get(name)
        sb = sandbox_branch(name)
        old_active = self.ws.get_active_feature()

        switched = {}
        for repo_name in feature.branches:
            repo = Repo(self.ws.get_repo(repo_name), self.ws.root)

            # Auto-stash if dirty and on old feature's sandbox
            current = repo.current_branch()
            if repo.is_dirty() and old_active and current.startswith("mgit/"):
                old_feature = current.removeprefix("mgit/")
                repo.stash_push(f"mgit:{old_feature}")

            # Checkout sandbox branch
            repo.checkout(sb)

            # Auto-unstash work for this feature
            repo.stash_pop_by_message(f"mgit:{name}")

            switched[repo_name] = sb

        # Set as active feature
        self.ws.set_active_feature(name)

        return switched
