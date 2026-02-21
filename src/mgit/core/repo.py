"""Repo class: git operations on a single repository."""

from __future__ import annotations

import os
from pathlib import Path

from mgit.core import git
from mgit.models.types import RepoInfo


class Repo:
    """Git operations on a single registered repository."""

    def __init__(self, info: RepoInfo, workspace_root: Path):
        self.info = info
        self.workspace_root = workspace_root
        self._path = workspace_root / info.path
        # Resolve symlinks so git operations work on the real path
        self.path = self._path.resolve() if self._path.is_symlink() else self._path

    @classmethod
    def at_worktree(cls, worktree_path: Path, info: RepoInfo) -> Repo:
        """Construct a Repo whose path points to a worktree directory.

        Used by bulk ops to run git commands in a feature's worktree
        rather than the original repo directory.
        """
        repo = object.__new__(cls)
        repo.info = info
        repo.workspace_root = worktree_path  # not used for worktree ops
        repo._path = worktree_path
        repo.path = worktree_path
        return repo

    def current_branch(self) -> str:
        return git.get_current_branch(self.path)

    def is_dirty(self) -> bool:
        return git.is_dirty(self.path)

    def checkout(self, branch: str, create: bool = False) -> None:
        """Checkout a branch, optionally creating it."""
        if create:
            git.run_git("checkout", "-b", branch, cwd=self.path)
        else:
            # Try checkout; if it doesn't exist, create it
            result = git.run_git("checkout", branch, cwd=self.path, check=False)
            if result.returncode != 0:
                git.run_git("checkout", "-b", branch, cwd=self.path)

    def status(self) -> str:
        """Get short status output."""
        result = git.run_git("status", "--short", cwd=self.path)
        return result.stdout

    def pull(self) -> str:
        """Pull from remote."""
        result = git.run_git("pull", cwd=self.path)
        return result.stdout + result.stderr

    def push(self) -> str:
        """Push to remote, setting upstream if needed."""
        branch = self.current_branch()
        result = git.run_git("push", cwd=self.path, check=False)
        if result.returncode != 0 and "no upstream branch" in result.stderr:
            result = git.run_git(
                "push", "--set-upstream", "origin", branch, cwd=self.path
            )
        elif result.returncode != 0:
            from mgit.utils.errors import GitError
            raise GitError(
                f"push failed: {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
        return result.stdout + result.stderr

    def commit(self, message: str) -> str:
        """Stage all changes and commit."""
        git.run_git("add", "-A", cwd=self.path)
        result = git.run_git("commit", "-m", message, cwd=self.path, check=False)
        if result.returncode != 0:
            if "nothing to commit" in result.stdout:
                return "nothing to commit"
            from mgit.utils.errors import GitError
            raise GitError(
                f"commit failed: {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
        return result.stdout

    def exec(self, command: list[str]) -> tuple[int, str, str]:
        """Run an arbitrary command in the repo directory.

        Returns (returncode, stdout, stderr).
        """
        import subprocess
        result = subprocess.run(
            command, cwd=self.path, capture_output=True, text=True,
        )
        return result.returncode, result.stdout, result.stderr

    # --- Stash operations ---

    def stash_push(self, message: str) -> bool:
        """Stash all changes (including untracked) with a message.

        Returns True if something was stashed, False if clean.
        """
        result = git.run_git(
            "stash", "push", "--include-untracked", "-m", message,
            cwd=self.path,
        )
        # git stash prints "No local changes to save" when clean
        return "No local changes to save" not in result.stdout

    def stash_pop(self) -> None:
        """Pop the most recent stash entry."""
        git.run_git("stash", "pop", cwd=self.path)

    # --- Worktree operations ---

    def add_worktree(self, path: Path, branch: str) -> None:
        """Create a worktree at path on the given branch.

        If the branch doesn't exist, creates it with -b.
        """
        # Check if branch already exists
        result = git.run_git(
            "rev-parse", "--verify", branch,
            cwd=self.path, check=False,
        )
        if result.returncode == 0:
            # Branch exists — just add worktree on it
            git.run_git("worktree", "add", str(path), branch, cwd=self.path)
        else:
            # Branch doesn't exist — create it
            git.run_git("worktree", "add", "-b", branch, str(path), cwd=self.path)

    def remove_worktree(self, path: Path) -> None:
        """Remove a worktree directory."""
        git.run_git("worktree", "remove", str(path), "--force", cwd=self.path)

    # --- Refspec push ---

    def push_to_target(self, target_branch: str) -> str:
        """Push current branch to a different remote branch name.

        Uses `git push -u origin <current>:<target>` so the first push
        sets tracking; subsequent push/pull work normally.
        """
        current = self.current_branch()
        refspec = f"{current}:{target_branch}"
        result = git.run_git(
            "push", "-u", "origin", refspec, cwd=self.path, check=False
        )
        if result.returncode != 0:
            from mgit.utils.errors import GitError
            raise GitError(
                f"push failed: {result.stderr.strip()}",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
        return result.stdout + result.stderr


def add_repo_from_url(
    workspace_root: Path, url: str, name: str | None = None
) -> RepoInfo:
    """Clone a repo from URL into the workspace and return RepoInfo."""
    clone_path = git.clone_repo(url, workspace_root, name=name)
    repo_name = name or clone_path.name
    branch = git.get_current_branch(clone_path)
    return RepoInfo(
        name=repo_name,
        path=repo_name,
        url=url,
        default_branch=branch,
    )


def add_repo_from_path(
    workspace_root: Path, local_path: Path, name: str | None = None
) -> RepoInfo:
    """Symlink a local repo into the workspace and return RepoInfo."""
    local_path = local_path.resolve()
    if not git.is_git_repo(local_path):
        raise ValueError(f"{local_path} is not a git repository")

    repo_name = name or local_path.name
    link_path = workspace_root / repo_name

    if not link_path.exists():
        os.symlink(local_path, link_path)

    branch = git.get_current_branch(local_path)
    url = git.get_remote_url(local_path)
    return RepoInfo(
        name=repo_name,
        path=repo_name,
        url=url,
        default_branch=branch,
    )
