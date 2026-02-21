"""Tests for feature lifecycle (worktree-based model)."""

import pytest

from mgit.core.feature import (
    FeatureManager,
    sandbox_branch,
)
from mgit.core.repo import Repo
from mgit.core import git
from mgit.utils.errors import (
    FeatureExistsError,
    FeatureNotFoundError,
    RepoNotFoundError,
)


class TestSandboxBranch:
    def test_sandbox_branch_format(self):
        assert sandbox_branch("auth-refactor") == "mgit/auth-refactor"

    def test_sandbox_branch_simple(self):
        assert sandbox_branch("fix") == "mgit/fix"


class TestFeatureCreate:
    def test_create_empty_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        feat = fm.create("test-feat")
        assert feat.name == "test-feat"
        assert feat.branches == {}
        assert (ws.features_dir / "test-feat.toml").exists()

    def test_create_with_description(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        feat = fm.create("test-feat", description="My feature")
        assert feat.description == "My feature"

    def test_create_duplicate_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("test-feat")
        with pytest.raises(FeatureExistsError):
            fm.create("test-feat")


class TestFeatureDelete:
    def test_delete_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("to-delete")
        fm.delete("to-delete")
        assert not (ws.features_dir / "to-delete.toml").exists()

    def test_delete_nonexistent_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        with pytest.raises(FeatureNotFoundError):
            fm.delete("no-such-feature")

    def test_delete_clears_active(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("active-feat")
        ws.set_active_feature("active-feat")
        fm.delete("active-feat")
        assert ws.get_active_feature() is None

    def test_delete_cleans_up_worktrees(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("to-delete", ["repo-a", "repo-b"])
        wt_a = ws.worktree_path("to-delete", "repo-a")
        wt_b = ws.worktree_path("to-delete", "repo-b")
        assert wt_a.exists()
        assert wt_b.exists()

        fm.delete("to-delete")

        # Worktree directories should be gone
        assert not wt_a.exists()
        assert not wt_b.exists()
        # Feature-level worktree dir should be gone
        assert not (ws.worktrees_dir / "to-delete").exists()


class TestFeatureGet:
    def test_get_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("my-feat", description="desc")
        feat = fm.get("my-feat")
        assert feat.name == "my-feat"
        assert feat.description == "desc"

    def test_get_nonexistent_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        with pytest.raises(FeatureNotFoundError):
            fm.get("nope")


class TestFeatureList:
    def test_list_empty(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        assert fm.list() == []

    def test_list_features(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat-1")
        fm.create("feat-2")
        features = fm.list()
        assert len(features) == 2
        names = {f.name for f in features}
        assert names == {"feat-1", "feat-2"}


class TestFeatureRemoveRepo:
    def test_remove_repo_from_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        fm.remove_repo("feat", "repo-a")
        feat = fm.get("feat")
        assert "repo-a" not in feat.branches
        assert "repo-b" in feat.branches

    def test_remove_nonexistent_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        with pytest.raises(RepoNotFoundError):
            fm.remove_repo("feat", "repo-b")

    def test_remove_repo_cleans_worktree(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat", ["repo-a", "repo-b"])
        wt_a = ws.worktree_path("feat", "repo-a")
        assert wt_a.exists()

        fm.remove_repo("feat", "repo-a")

        # Worktree for repo-a should be gone
        assert not wt_a.exists()
        # repo-b's worktree should still exist
        assert ws.worktree_path("feat", "repo-b").exists()


class TestFeatureStart:
    def test_start_creates_feature_and_enrolls(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        feat, newly_added = fm.start("my-feat", ["repo-a", "repo-b"])

        assert feat.name == "my-feat"
        assert "repo-a" in feat.branches
        assert "repo-b" in feat.branches
        assert set(newly_added) == {"repo-a", "repo-b"}

    def test_start_creates_worktree_dirs(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("my-feat", ["repo-a", "repo-b"])

        wt_a = ws.worktree_path("my-feat", "repo-a")
        wt_b = ws.worktree_path("my-feat", "repo-b")
        assert wt_a.exists()
        assert wt_b.exists()
        assert wt_a.is_dir()
        assert wt_b.is_dir()

    def test_start_worktree_has_sandbox_branch(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("my-feat", ["repo-a"])

        wt_path = ws.worktree_path("my-feat", "repo-a")
        branch = git.get_current_branch(wt_path)
        assert branch == "mgit/my-feat"

    def test_start_existing_feature_enrolls_more(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("my-feat", ["repo-a"])
        feat, newly_added = fm.start("my-feat", ["repo-b"])

        assert "repo-a" in feat.branches
        assert "repo-b" in feat.branches
        assert newly_added == ["repo-b"]

    def test_start_sets_active_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("my-feat", ["repo-a"])
        assert ws.get_active_feature() == "my-feat"

    def test_start_with_custom_target(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        feat, _ = fm.start("my-feat", ["repo-a"], target_branch="custom-branch")
        assert feat.branches["repo-a"] == "custom-branch"

    def test_start_unknown_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        with pytest.raises(RepoNotFoundError):
            fm.start("my-feat", ["no-such-repo"])

    def test_start_idempotent_re_enrollment(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        feat1, added1 = fm.start("my-feat", ["repo-a"])
        feat2, added2 = fm.start("my-feat", ["repo-a"])

        assert added1 == ["repo-a"]
        assert added2 == []  # Already enrolled

    def test_parallel_features_same_repo(self, initialized_workspace):
        """Two features on the same repo should each get independent worktrees."""
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat-a", ["repo-a"])
        fm.start("feat-b", ["repo-a"])

        wt_a = ws.worktree_path("feat-a", "repo-a")
        wt_b = ws.worktree_path("feat-b", "repo-a")

        assert wt_a.exists()
        assert wt_b.exists()
        assert wt_a != wt_b

        # Each worktree should be on its own sandbox branch
        branch_a = git.get_current_branch(wt_a)
        branch_b = git.get_current_branch(wt_b)
        assert branch_a == "mgit/feat-a"
        assert branch_b == "mgit/feat-b"

        # Create files in each — no conflicts
        (wt_a / "a-work.txt").write_text("feat-a work")
        (wt_b / "b-work.txt").write_text("feat-b work")
        assert (wt_a / "a-work.txt").exists()
        assert (wt_b / "b-work.txt").exists()
        # Files should NOT bleed across worktrees
        assert not (wt_a / "b-work.txt").exists()
        assert not (wt_b / "a-work.txt").exists()

    def test_start_does_not_modify_original_repo(self, initialized_workspace):
        """Starting a feature should not change the original repo's branch."""
        ws = initialized_workspace
        fm = FeatureManager(ws)

        repo = Repo(ws.get_repo("repo-a"), ws.root)
        original_branch = repo.current_branch()

        fm.start("my-feat", ["repo-a"])

        # Original repo should still be on its original branch
        assert repo.current_branch() == original_branch


class TestFeatureSwitch:
    def test_switch_just_sets_active(self, initialized_workspace):
        """Switch should just set the active feature — no checkout happens."""
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat-a", ["repo-a"])
        fm.start("feat-b", ["repo-a"])

        # Original repo branch should not change during switch
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        branch_before = repo.current_branch()

        wt_paths = fm.switch("feat-a")

        assert ws.get_active_feature() == "feat-a"
        # Original repo unchanged
        assert repo.current_branch() == branch_before
        # Returns worktree paths
        assert "repo-a" in wt_paths
        assert wt_paths["repo-a"] == ws.worktree_path("feat-a", "repo-a")

    def test_switch_sets_active_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat-a", ["repo-a"])
        fm.start("feat-b", ["repo-a"])

        fm.switch("feat-a")
        assert ws.get_active_feature() == "feat-a"

    def test_switch_returns_worktree_paths(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat", ["repo-a", "repo-b"])

        wt_paths = fm.switch("feat")
        assert set(wt_paths.keys()) == {"repo-a", "repo-b"}
        assert wt_paths["repo-a"] == ws.worktree_path("feat", "repo-a")
        assert wt_paths["repo-b"] == ws.worktree_path("feat", "repo-b")


class TestGetWorktreePaths:
    def test_get_worktree_paths(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("my-feat", ["repo-a", "repo-b"])
        paths = fm.get_worktree_paths("my-feat")

        assert set(paths.keys()) == {"repo-a", "repo-b"}
        assert paths["repo-a"] == ws.worktree_path("my-feat", "repo-a")
        assert paths["repo-b"] == ws.worktree_path("my-feat", "repo-b")
