"""Tests for feature lifecycle."""

import pytest

from mgit.core.feature import FeatureManager, sandbox_branch
from mgit.core.repo import Repo
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
        fm.create("feat")
        # Enroll repos via start
        fm.start("feat", ["repo-a", "repo-b"])
        fm.remove_repo("feat", "repo-a")
        feat = fm.get("feat")
        assert "repo-a" not in feat.branches
        assert "repo-b" in feat.branches

    def test_remove_nonexistent_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat")
        fm.start("feat", ["repo-a"])
        with pytest.raises(RepoNotFoundError):
            fm.remove_repo("feat", "repo-b")


class TestFeatureStart:
    def test_start_creates_feature_and_enrolls(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        feat, newly_added = fm.start("my-feat", ["repo-a", "repo-b"])

        assert feat.name == "my-feat"
        assert "repo-a" in feat.branches
        assert "repo-b" in feat.branches
        assert set(newly_added) == {"repo-a", "repo-b"}

        # Verify repos are on sandbox branch
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo_a.current_branch() == "mgit/my-feat"
        repo_b = Repo(ws.get_repo("repo-b"), ws.root)
        assert repo_b.current_branch() == "mgit/my-feat"

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

    def test_start_auto_stashes_on_different_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        # Start feature A on repo-a
        fm.start("feat-a", ["repo-a"])

        # Make repo-a dirty while on feat-a's sandbox
        (ws.root / "repo-a" / "dirty.txt").write_text("dirty work")

        # Start feature B on repo-a — should auto-stash feat-a changes
        fm.start("feat-b", ["repo-a"])

        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo_a.current_branch() == "mgit/feat-b"
        # Should be clean after stash
        assert not repo_a.is_dirty()

    def test_start_auto_unstashes_previous_work(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        # Start feature A and make changes
        fm.start("feat-a", ["repo-a"])
        (ws.root / "repo-a" / "work.txt").write_text("feat-a work")

        # Switch to feature B (auto-stashes feat-a)
        fm.start("feat-b", ["repo-a"])
        assert not Repo(ws.get_repo("repo-a"), ws.root).is_dirty()

        # Switch back to feature A (auto-unstashes)
        fm.start("feat-a", ["repo-a"])
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo_a.current_branch() == "mgit/feat-a"
        assert (ws.root / "repo-a" / "work.txt").exists()

    def test_start_idempotent_re_enrollment(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        feat1, added1 = fm.start("my-feat", ["repo-a"])
        feat2, added2 = fm.start("my-feat", ["repo-a"])

        assert added1 == ["repo-a"]
        assert added2 == []  # Already enrolled


class TestFeatureSwitch:
    def test_switch_branches(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        # Start a feature to enroll repos
        fm.start("feat", ["repo-a", "repo-b"])

        # Go back to main
        Repo(ws.get_repo("repo-a"), ws.root).checkout("main")
        Repo(ws.get_repo("repo-b"), ws.root).checkout("main")

        # Switch back
        switched = fm.switch("feat")
        assert switched == {
            "repo-a": "mgit/feat",
            "repo-b": "mgit/feat",
        }
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo_a.current_branch() == "mgit/feat"
        repo_b = Repo(ws.get_repo("repo-b"), ws.root)
        assert repo_b.current_branch() == "mgit/feat"

    def test_switch_auto_stashes_old_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        # Start feat-a, make dirty
        fm.start("feat-a", ["repo-a"])
        (ws.root / "repo-a" / "change.txt").write_text("feat-a change")

        # Start feat-b with repo-a enrolled
        fm.start("feat-b", ["repo-a"])

        # Switch to feat-b should have stashed feat-a
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert not repo_a.is_dirty()

    def test_switch_sets_active_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)

        fm.start("feat-a", ["repo-a"])
        fm.start("feat-b", ["repo-a"])

        fm.switch("feat-a")
        assert ws.get_active_feature() == "feat-a"
