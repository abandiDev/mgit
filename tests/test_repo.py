"""Tests for repo operations."""

import os
import subprocess
from pathlib import Path

import pytest

from mgit.core.repo import Repo, add_repo_from_path, add_repo_from_url
from mgit.core.workspace import Workspace
from mgit.models.types import RepoInfo


class TestRepo:
    def test_current_branch(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        branch = repo.current_branch()
        assert isinstance(branch, str)
        assert len(branch) > 0

    def test_is_dirty_clean(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        assert repo.is_dirty() is False

    def test_is_dirty_with_changes(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        (ws.root / "repo-a" / "new-file.txt").write_text("dirty")
        assert repo.is_dirty() is True

    def test_status(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        # Clean repo
        status = repo.status()
        assert status.strip() == ""

        # Dirty repo
        (ws.root / "repo-a" / "new-file.txt").write_text("dirty")
        status = repo.status()
        assert "new-file.txt" in status

    def test_checkout_creates_branch(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        repo.checkout("test-branch", create=True)
        assert repo.current_branch() == "test-branch"

    def test_checkout_existing_branch(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        original = repo.current_branch()
        repo.checkout("test-branch", create=True)
        repo.checkout(original)
        assert repo.current_branch() == original

    def test_checkout_auto_creates(self, initialized_workspace):
        """checkout() without create=True auto-creates if branch doesn't exist."""
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        repo.checkout("auto-created-branch")
        assert repo.current_branch() == "auto-created-branch"

    def test_commit(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        (ws.root / "repo-a" / "new-file.txt").write_text("content")
        output = repo.commit("test commit")
        assert "nothing to commit" not in output

    def test_commit_nothing(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        output = repo.commit("empty")
        assert "nothing to commit" in output

    def test_exec(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)
        code, stdout, stderr = repo.exec(["echo", "hello"])
        assert code == 0
        assert "hello" in stdout


class TestAddRepoFromPath:
    def test_symlinks_local_repo(self, initialized_workspace, git_repo):
        ws = initialized_workspace
        info = add_repo_from_path(ws.root, git_repo, name="linked-repo")
        assert info.name == "linked-repo"
        assert info.path == "linked-repo"
        link = ws.root / "linked-repo"
        assert link.is_symlink()
        assert link.resolve() == git_repo

    def test_uses_dir_name_by_default(self, initialized_workspace, git_repo):
        ws = initialized_workspace
        info = add_repo_from_path(ws.root, git_repo)
        assert info.name == git_repo.name

    def test_rejects_non_git_dir(self, initialized_workspace, tmp_path):
        ws = initialized_workspace
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()
        with pytest.raises(ValueError, match="not a git repository"):
            add_repo_from_path(ws.root, non_git)
