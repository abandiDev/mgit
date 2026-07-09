"""Tests for repo operations."""


import pytest

from mgit.core.repo import Repo, add_repo_from_path
from mgit.core import git


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


class TestWorktreeOperations:
    def test_add_worktree(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)

        wt_path = ws.worktrees_dir / "test-feat" / "repo-a"
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        repo.add_worktree(wt_path, "mgit/test-feat")

        assert wt_path.exists()
        assert wt_path.is_dir()
        branch = git.get_current_branch(wt_path)
        assert branch == "mgit/test-feat"

    def test_add_worktree_existing_branch(self, initialized_workspace):
        """add_worktree works when the branch already exists."""
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)

        # Create branch first
        repo.checkout("mgit/existing", create=True)
        repo.checkout("main")

        wt_path = ws.worktrees_dir / "existing" / "repo-a"
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        repo.add_worktree(wt_path, "mgit/existing")

        assert wt_path.exists()
        branch = git.get_current_branch(wt_path)
        assert branch == "mgit/existing"

    def test_remove_worktree(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)

        wt_path = ws.worktrees_dir / "test-feat" / "repo-a"
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        repo.add_worktree(wt_path, "mgit/test-feat")
        assert wt_path.exists()

        repo.remove_worktree(wt_path)
        assert not wt_path.exists()

    def test_at_worktree(self, initialized_workspace):
        ws = initialized_workspace
        repo = Repo(ws.get_repo("repo-a"), ws.root)

        wt_path = ws.worktrees_dir / "test-feat" / "repo-a"
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        repo.add_worktree(wt_path, "mgit/test-feat")

        # Create a Repo pointing at the worktree
        wt_repo = Repo.at_worktree(wt_path, ws.get_repo("repo-a"))
        assert wt_repo.path == wt_path
        assert wt_repo.current_branch() == "mgit/test-feat"

        # Should be able to do normal operations
        (wt_path / "wt-file.txt").write_text("worktree content")
        assert wt_repo.is_dirty()
        status = wt_repo.status()
        assert "wt-file.txt" in status


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
