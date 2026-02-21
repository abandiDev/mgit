"""Tests for feature lifecycle."""

import pytest

from mgit.core.feature import FeatureManager
from mgit.core.repo import Repo
from mgit.utils.errors import (
    DirtyTreeError,
    FeatureExistsError,
    FeatureNotFoundError,
    RepoNotFoundError,
)


class TestFeatureCreate:
    def test_create_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        feat = fm.create("test-feat", {"repo-a": "feat-branch-a"})
        assert feat.name == "test-feat"
        assert feat.branches == {"repo-a": "feat-branch-a"}
        assert (ws.features_dir / "test-feat.toml").exists()

    def test_create_with_description(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        feat = fm.create("test-feat", {"repo-a": "branch"}, description="My feature")
        assert feat.description == "My feature"

    def test_create_duplicate_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("test-feat", {"repo-a": "branch"})
        with pytest.raises(FeatureExistsError):
            fm.create("test-feat", {"repo-a": "branch2"})

    def test_create_unknown_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        with pytest.raises(RepoNotFoundError):
            fm.create("test-feat", {"no-such-repo": "branch"})

    def test_create_multi_repo(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        feat = fm.create("test-feat", {
            "repo-a": "feat-a",
            "repo-b": "feat-b",
        })
        assert len(feat.branches) == 2


class TestFeatureDelete:
    def test_delete_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("to-delete", {"repo-a": "branch"})
        fm.delete("to-delete")
        assert not (ws.features_dir / "to-delete.toml").exists()

    def test_delete_nonexistent_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        with pytest.raises(FeatureNotFoundError):
            fm.delete("no-such-feature")


class TestFeatureGet:
    def test_get_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("my-feat", {"repo-a": "branch-a"}, description="desc")
        feat = fm.get("my-feat")
        assert feat.name == "my-feat"
        assert feat.description == "desc"
        assert feat.branches["repo-a"] == "branch-a"

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
        fm.create("feat-1", {"repo-a": "b1"})
        fm.create("feat-2", {"repo-b": "b2"})
        features = fm.list()
        assert len(features) == 2
        names = {f.name for f in features}
        assert names == {"feat-1", "feat-2"}


class TestFeatureAddRemoveRepo:
    def test_add_repo_to_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {"repo-a": "branch-a"})
        fm.add_repo("feat", "repo-b", "branch-b")
        feat = fm.get("feat")
        assert "repo-b" in feat.branches
        assert feat.branches["repo-b"] == "branch-b"

    def test_add_unknown_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {"repo-a": "branch"})
        with pytest.raises(RepoNotFoundError):
            fm.add_repo("feat", "no-such", "branch")

    def test_remove_repo_from_feature(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {"repo-a": "a", "repo-b": "b"})
        fm.remove_repo("feat", "repo-a")
        feat = fm.get("feat")
        assert "repo-a" not in feat.branches
        assert "repo-b" in feat.branches

    def test_remove_nonexistent_repo_raises(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {"repo-a": "a"})
        with pytest.raises(RepoNotFoundError):
            fm.remove_repo("feat", "repo-b")


class TestFeatureSwitch:
    def test_switch_branches(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {
            "repo-a": "feature-branch-a",
            "repo-b": "feature-branch-b",
        })
        switched = fm.switch("feat")
        assert switched == {
            "repo-a": "feature-branch-a",
            "repo-b": "feature-branch-b",
        }
        # Verify branches actually switched
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo_a.current_branch() == "feature-branch-a"
        repo_b = Repo(ws.get_repo("repo-b"), ws.root)
        assert repo_b.current_branch() == "feature-branch-b"

    def test_switch_aborts_on_dirty_tree(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.create("feat", {"repo-a": "branch-a", "repo-b": "branch-b"})

        # Make repo-a dirty
        (ws.root / "repo-a" / "dirty.txt").write_text("uncommitted")

        with pytest.raises(DirtyTreeError) as exc_info:
            fm.switch("feat")
        assert "repo-a" in exc_info.value.dirty_repos

        # Verify neither repo was switched
        repo_a = Repo(ws.get_repo("repo-a"), ws.root)
        repo_b = Repo(ws.get_repo("repo-b"), ws.root)
        assert repo_a.current_branch() != "branch-a"
        assert repo_b.current_branch() != "branch-b"
