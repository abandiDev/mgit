"""Tests for workspace initialization and management."""


import pytest

from mgit.core.workspace import Workspace
from mgit.utils.errors import WorkspaceExistsError, WorkspaceNotFoundError


class TestWorkspaceInit:
    def test_init_creates_mgit_dir(self, tmp_workspace):
        ws, _ = Workspace.init(tmp_workspace)
        assert (tmp_workspace / ".mgit").is_dir()
        assert (tmp_workspace / ".mgit" / "config.toml").is_file()
        assert (tmp_workspace / ".mgit" / "features").is_dir()

    def test_init_sets_name(self, tmp_workspace):
        ws, _ = Workspace.init(tmp_workspace, name="my-platform")
        assert ws.name == "my-platform"

    def test_init_defaults_name_to_dir(self, tmp_workspace):
        ws, _ = Workspace.init(tmp_workspace)
        assert ws.name == tmp_workspace.name

    def test_init_raises_if_already_exists(self, tmp_workspace):
        Workspace.init(tmp_workspace)
        with pytest.raises(WorkspaceExistsError):
            Workspace.init(tmp_workspace)

    def test_init_creates_dir_if_missing(self, tmp_path):
        new_dir = tmp_path / "new-workspace"
        ws, _ = Workspace.init(new_dir)
        assert new_dir.is_dir()
        assert ws.root == new_dir

    def test_init_scans_repos(self, workspace_with_repos):
        ws, found = Workspace.init(workspace_with_repos, scan=True, auto_add=False)
        assert len(found) == 2
        names = {r.name for r in found}
        assert names == {"repo-a", "repo-b"}

    def test_init_auto_add(self, workspace_with_repos):
        ws, found = Workspace.init(workspace_with_repos, scan=True, auto_add=True)
        assert len(ws.repos) == 2
        assert "repo-a" in ws.repos
        assert "repo-b" in ws.repos

    def test_init_no_scan(self, workspace_with_repos):
        ws, found = Workspace.init(workspace_with_repos, scan=False)
        assert len(found) == 0
        assert len(ws.repos) == 0

    def test_init_generates_agent_md(self, tmp_workspace):
        ws, _ = Workspace.init(tmp_workspace)
        agent_md = tmp_workspace / "AGENT.md"
        assert agent_md.exists()
        content = agent_md.read_text()
        assert "mgit Workspace" in content
        assert "mgit context" in content
        assert "mgit feature start" in content
        assert "sandbox branches" in content.lower() or "Sandbox branches" in content


class TestWorkspaceFind:
    def test_find_from_root(self, initialized_workspace):
        ws = Workspace.find(initialized_workspace.root)
        assert ws.root == initialized_workspace.root

    def test_find_from_subdirectory(self, initialized_workspace):
        sub = initialized_workspace.root / "repo-a"
        ws = Workspace.find(sub)
        assert ws.root == initialized_workspace.root

    def test_find_raises_if_none(self, tmp_path):
        with pytest.raises(WorkspaceNotFoundError):
            Workspace.find(tmp_path)

    def test_find_loads_repos(self, initialized_workspace):
        ws = Workspace.find(initialized_workspace.root)
        assert len(ws.repos) == 2


class TestWorkspaceRepoManagement:
    def test_add_repo(self, initialized_workspace):
        from mgit.models.types import RepoInfo

        ws = initialized_workspace
        ws.repos.clear()
        ws.save()

        repo = RepoInfo(name="new-repo", path="new-repo", default_branch="main")
        ws.add_repo(repo)
        assert "new-repo" in ws.repos

        # Verify persistence
        ws2 = Workspace.find(ws.root)
        assert "new-repo" in ws2.repos

    def test_add_duplicate_repo_raises(self, initialized_workspace):
        from mgit.models.types import RepoInfo
        from mgit.utils.errors import RepoExistsError

        ws = initialized_workspace
        with pytest.raises(RepoExistsError):
            ws.add_repo(RepoInfo(name="repo-a", path="repo-a"))

    def test_remove_repo(self, initialized_workspace):
        ws = initialized_workspace
        ws.remove_repo("repo-a")
        assert "repo-a" not in ws.repos

    def test_remove_nonexistent_raises(self, initialized_workspace):
        from mgit.utils.errors import RepoNotFoundError

        ws = initialized_workspace
        with pytest.raises(RepoNotFoundError):
            ws.remove_repo("no-such-repo")

    def test_get_repo(self, initialized_workspace):
        ws = initialized_workspace
        repo = ws.get_repo("repo-a")
        assert repo.name == "repo-a"

    def test_config_roundtrip(self, initialized_workspace):
        """Config survives save/load cycle."""
        ws = initialized_workspace
        ws.save()
        ws2 = Workspace.find(ws.root)
        assert ws2.name == ws.name
        assert set(ws2.repos.keys()) == set(ws.repos.keys())
        for name in ws.repos:
            assert ws2.repos[name].path == ws.repos[name].path
            assert ws2.repos[name].default_branch == ws.repos[name].default_branch


class TestWorkspaceRemove:
    def test_remove_deletes_mgit_dir(self, initialized_workspace):
        ws = initialized_workspace
        assert ws.mgit_dir.exists()
        ws.remove()
        assert not ws.mgit_dir.exists()

    def test_remove_deletes_agent_md(self, tmp_workspace):
        ws, _ = Workspace.init(tmp_workspace)
        agent_md = tmp_workspace / "AGENT.md"
        assert agent_md.exists()
        ws.remove()
        assert not agent_md.exists()

    def test_remove_leaves_repos_untouched(self, initialized_workspace):
        ws = initialized_workspace
        repo_a = ws.root / "repo-a"
        assert repo_a.is_dir()
        ws.remove()
        assert repo_a.is_dir()

    def test_remove_idempotent_on_agent_md(self, initialized_workspace):
        ws = initialized_workspace
        # AGENT.md may not exist if workspace was created by fixture (no init)
        agent_md = ws.root / "AGENT.md"
        if agent_md.exists():
            agent_md.unlink()
        # Should not raise
        ws.remove()
        assert not ws.mgit_dir.exists()

    def test_find_raises_after_remove(self, initialized_workspace):
        ws = initialized_workspace
        root = ws.root
        ws.remove()
        with pytest.raises(WorkspaceNotFoundError):
            Workspace.find(root)


class TestActiveFeature:
    def test_get_active_feature_none(self, initialized_workspace):
        ws = initialized_workspace
        assert ws.get_active_feature() is None

    def test_set_and_get_active_feature(self, initialized_workspace):
        ws = initialized_workspace
        ws.set_active_feature("my-feature")
        assert ws.get_active_feature() == "my-feature"

    def test_clear_active_feature(self, initialized_workspace):
        ws = initialized_workspace
        ws.set_active_feature("my-feature")
        ws.clear_active_feature()
        assert ws.get_active_feature() is None

    def test_clear_when_no_active(self, initialized_workspace):
        ws = initialized_workspace
        # Should not raise
        ws.clear_active_feature()
        assert ws.get_active_feature() is None

    def test_active_feature_persists(self, initialized_workspace):
        ws = initialized_workspace
        ws.set_active_feature("persistent")
        # Re-read from filesystem
        ws2 = Workspace.find(ws.root)
        assert ws2.get_active_feature() == "persistent"


class TestDetectRepoFromCwd:
    def test_detect_from_repo_root(self, initialized_workspace):
        ws = initialized_workspace
        cwd = ws.root / "repo-a"
        assert ws.detect_repo_from_cwd(cwd) == "repo-a"

    def test_detect_from_subdirectory(self, initialized_workspace):
        ws = initialized_workspace
        sub = ws.root / "repo-a" / "subdir"
        sub.mkdir(parents=True, exist_ok=True)
        assert ws.detect_repo_from_cwd(sub) == "repo-a"

    def test_detect_from_workspace_root(self, initialized_workspace):
        ws = initialized_workspace
        assert ws.detect_repo_from_cwd(ws.root) is None

    def test_detect_from_outside_workspace(self, initialized_workspace, tmp_path):
        ws = initialized_workspace
        assert ws.detect_repo_from_cwd(tmp_path) is None
