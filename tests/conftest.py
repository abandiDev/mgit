"""Shared test fixtures for mgit."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary directory to use as a workspace."""
    return tmp_path


@pytest.fixture
def git_repo(tmp_path):
    """Create a bare git repo with one commit."""
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    # Create initial commit
    (repo / "README.md").write_text("# test")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


@pytest.fixture
def workspace_with_repos(tmp_path):
    """Create a workspace directory with two git repos already in it."""
    ws_dir = tmp_path / "my-workspace"
    ws_dir.mkdir()

    for name in ["repo-a", "repo-b"]:
        repo = ws_dir / name
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "README.md").write_text(f"# {name}")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)

    return ws_dir


@pytest.fixture
def initialized_workspace(workspace_with_repos):
    """A fully initialized workspace with repos registered."""
    from mgit.core.workspace import Workspace

    ws, found = Workspace.init(workspace_with_repos, scan=True, auto_add=True)
    return ws
