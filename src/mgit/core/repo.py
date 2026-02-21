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
        """Stash dirty changes (including untracked files) with a message.

        Returns True if something was stashed, False if working tree was clean.
        """
        if not self.is_dirty():
            return False
        git.run_git("stash", "push", "-u", "-m", message, cwd=self.path)
        return True

    def stash_pop_by_message(self, message: str) -> bool:
        """Find and pop a stash entry by its message.

        Scans `git stash list` for an entry matching the message and pops it.
        Returns True if a matching stash was found and popped.
        """
        result = git.run_git("stash", "list", cwd=self.path, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return False

        for line in result.stdout.strip().splitlines():
            # Format: stash@{N}: On branch: message
            if message in line:
                # Extract stash ref (e.g., "stash@{0}")
                stash_ref = line.split(":")[0].strip()
                git.run_git("stash", "pop", stash_ref, cwd=self.path)
                return True

        return False

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
