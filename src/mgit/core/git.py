"""Git subprocess wrapper - all git operations go through here."""

from __future__ import annotations

import subprocess
from pathlib import Path

from mgit.utils.errors import GitError


def run_git(
    *args: str,
    cwd: str | Path | None = None,
    check: bool = True,
    capture: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result.

    Args:
        *args: Git subcommand and arguments (e.g. "status", "--short").
        cwd: Working directory for the command.
        check: If True, raise GitError on non-zero exit.
        capture: If True, capture stdout/stderr.
        env: Extra environment variables, merged over os.environ
            (e.g. GIT_INDEX_FILE for temp-index snapshots).

    Returns:
        CompletedProcess with stdout/stderr as strings.

    Raises:
        GitError: If check=True and the command fails.
    """
    import os

    cmd = ["git", *args]
    full_env = {**os.environ, **env} if env else None
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            env=full_env,
        )
    except FileNotFoundError:
        raise GitError("git is not installed or not in PATH")

    if check and result.returncode != 0:
        stderr = result.stderr.strip() if capture else ""
        raise GitError(
            f"git {args[0]} failed: {stderr}",
            returncode=result.returncode,
            stderr=stderr,
        )

    return result


def clone_repo(url: str, dest: Path, name: str | None = None) -> Path:
    """Clone a git repository.

    Returns the path to the cloned directory.
    """
    cmd = ["clone", url]
    if name:
        cmd.append(name)
        clone_path = dest / name
    else:
        # Derive name from URL
        repo_name = url.rstrip("/").rsplit("/", 1)[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        clone_path = dest / repo_name

    run_git(*cmd, cwd=dest)
    return clone_path


def get_current_branch(repo_path: Path) -> str:
    """Get the current branch name of a repo."""
    result = run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_path)
    return result.stdout.strip()


def get_remote_url(repo_path: Path) -> str | None:
    """Get the origin remote URL, or None if no remote."""
    result = run_git("remote", "get-url", "origin", cwd=repo_path, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def is_git_repo(path: Path) -> bool:
    """Check if a path is a git repository."""
    result = run_git(
        "rev-parse", "--is-inside-work-tree",
        cwd=path, check=False, capture=True,
    )
    return result.returncode == 0


def is_dirty(repo_path: Path) -> bool:
    """Check if a repo has uncommitted changes (staged or unstaged)."""
    result = run_git("status", "--porcelain", cwd=repo_path)
    return bool(result.stdout.strip())
