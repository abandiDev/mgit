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

    def add_worktree(self, path: Path, branch: str, start_point: str | None = None) -> None:
        """Create a worktree at path on the given branch.

        If the branch doesn't exist, creates it with -b — from start_point
        when given (used by forked features to branch at the pinned SHA),
        else from the current HEAD.
        """
        # A worktree dir deleted with rm -rf (not 'git worktree remove')
        # stays registered and would make 'worktree add' fail forever
        git.run_git("worktree", "prune", cwd=self.path, check=False)

        # Check if branch already exists
        result = git.run_git(
            "rev-parse", "--verify", branch,
            cwd=self.path, check=False,
        )
        if result.returncode == 0:
            # Branch exists — just add worktree on it
            git.run_git("worktree", "add", str(path), branch, cwd=self.path)
        elif start_point:
            git.run_git("worktree", "add", "-b", branch, str(path), start_point, cwd=self.path)
        else:
            git.run_git("worktree", "add", "-b", branch, str(path), cwd=self.path)

    def remove_worktree(self, path: Path) -> None:
        """Remove a worktree directory."""
        git.run_git("worktree", "remove", str(path), "--force", cwd=self.path)

    # --- Ref plumbing (checkpoint/WIP anchoring) ---

    def rev_parse(self, ref: str) -> str | None:
        """Resolve a ref to a SHA, or None if it doesn't exist."""
        result = git.run_git(
            "rev-parse", "--verify", "--quiet", ref, cwd=self.path, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def update_ref(self, ref: str, sha: str) -> None:
        """Create or move a ref (pins commits against gc)."""
        git.run_git("update-ref", ref, sha, cwd=self.path)

    def delete_ref(self, ref: str) -> None:
        """Delete a ref if it exists."""
        git.run_git("update-ref", "-d", ref, cwd=self.path, check=False)

    def list_refs(self, prefix: str) -> dict[str, str]:
        """List refs under a prefix as {refname: sha}."""
        result = git.run_git(
            "for-each-ref", "--format=%(refname) %(objectname)", prefix,
            cwd=self.path, check=False,
        )
        refs: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if " " in line:
                name, _, sha = line.rpartition(" ")
                refs[name] = sha
        return refs

    # --- Snapshot (non-destructive WIP capture) ---

    def snapshot_commit(self, message: str) -> str:
        """Capture the working tree (staged + unstaged + untracked, respecting
        .gitignore) as a commit object parented on HEAD — via a temporary
        index, so the real index and working tree are untouched.

        Returns the snapshot SHA, or the HEAD SHA when the tree matches HEAD.
        """
        import tempfile

        head = git.run_git("rev-parse", "HEAD", cwd=self.path).stdout.strip()
        fd, tmp_index = tempfile.mkstemp(prefix="mgit-index-")
        os.close(fd)
        os.unlink(tmp_index)  # git creates the index file itself
        env = {"GIT_INDEX_FILE": tmp_index}
        try:
            git.run_git("read-tree", "HEAD", cwd=self.path, env=env)
            git.run_git("add", "-A", cwd=self.path, env=env)
            tree = git.run_git("write-tree", cwd=self.path, env=env).stdout.strip()
            head_tree = git.run_git(
                "rev-parse", "HEAD^{tree}", cwd=self.path,
            ).stdout.strip()
            if tree == head_tree:
                return head
            return git.run_git(
                "commit-tree", tree, "-p", head, "-m", message, cwd=self.path,
            ).stdout.strip()
        finally:
            try:
                os.unlink(tmp_index)
            except OSError:
                pass

    def restore_to(self, head: str, snapshot: str, branch: str | None = None) -> None:
        """Restore the working tree to a checkpoint: committed state at `head`,
        WIP from `snapshot` re-materialized as dirty files.

        When `branch` is given, the worktree is forced back onto that branch
        first — otherwise the resets would move whatever branch the user
        happens to have checked out (e.g. an experiment branch), silently
        rewriting it. clean -fd removes files that didn't exist at the
        checkpoint (deletes un-gitignored artifacts — callers must
        checkpoint first).
        """
        if branch:
            current = git.run_git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=self.path, check=False,
            ).stdout.strip()
            if current != branch:
                git.run_git("checkout", "-f", "-B", branch, head, cwd=self.path)
        git.run_git("clean", "-fd", cwd=self.path)
        git.run_git("reset", "--hard", snapshot, cwd=self.path)
        if snapshot != head:
            git.run_git("reset", head, cwd=self.path)

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
