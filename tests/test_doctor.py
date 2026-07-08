"""Tests for the upgrade health checks (core/doctor.py + 'mgit upgrade --fix')."""

import pytest
from click.testing import CliRunner

from mgit.cli import main
from mgit.core import git
from mgit.core.doctor import run_checks
from mgit.core.feature import FeatureManager
from mgit.models.types import RepoInfo


@pytest.fixture
def runner():
    return CliRunner()


class TestChecksClean:
    def test_healthy_workspace_has_no_findings(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        assert run_checks(ws) == []

    def test_upgrade_reports_clean(self, initialized_workspace, runner, monkeypatch):
        monkeypatch.chdir(initialized_workspace.root)
        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert "no legacy issues" in result.output


class TestOrphanedSandboxBranches:
    def _orphan_branch(self, ws):
        """Simulate an old-mgit delete: branch left behind, feature gone."""
        fm = FeatureManager(ws)
        fm.start("ghost", ["repo-a"])
        fm.materialize("ghost", "repo-a")
        # Bypass the fixed delete: remove feature file + worktree only
        import shutil
        wt = ws.worktree_path("ghost", "repo-a")
        git.run_git("worktree", "remove", str(wt), "--force",
                    cwd=ws.root / "repo-a")
        shutil.rmtree(ws.worktrees_dir / "ghost", ignore_errors=True)
        (ws.features_dir / "ghost.toml").unlink()
        shutil.rmtree(ws.features_dir / "ghost", ignore_errors=True)
        ws.clear_active_feature()

    def test_detected_and_fixed(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        self._orphan_branch(ws)
        repo_dir = ws.root / "repo-a"
        assert git.run_git("rev-parse", "--verify", "--quiet",
                           "refs/heads/mgit/ghost", cwd=repo_dir,
                           check=False).returncode == 0

        findings = run_checks(ws)
        assert any("mgit/ghost" in f.message and f.fixable for f in findings)

        monkeypatch.chdir(ws.root)
        result = runner.invoke(main, ["upgrade"])
        assert "mgit/ghost" in result.output
        assert "--fix would" in result.output
        # Report-only run must not delete anything
        assert git.run_git("rev-parse", "--verify", "--quiet",
                           "refs/heads/mgit/ghost", cwd=repo_dir,
                           check=False).returncode == 0

        result = runner.invoke(main, ["upgrade", "--fix"])
        assert result.exit_code == 0
        assert git.run_git("rev-parse", "--verify", "--quiet",
                           "refs/heads/mgit/ghost", cwd=repo_dir,
                           check=False).returncode != 0

    def test_live_feature_branch_not_flagged(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("alive", ["repo-a"])
        fm.materialize("alive", "repo-a")
        findings = run_checks(ws)
        assert not any("mgit/alive" in f.message for f in findings)


class TestOrphanedRefs:
    def test_checkpoint_ref_of_deleted_feature_fixed(
        self, initialized_workspace, runner, monkeypatch,
    ):
        ws = initialized_workspace
        repo_dir = ws.root / "repo-a"
        head = git.run_git("rev-parse", "HEAD", cwd=repo_dir).stdout.strip()
        # Simulate an old-mgit leftover
        git.run_git("update-ref", "refs/mgit/checkpoint/gone/cp-0001", head,
                    cwd=repo_dir)

        findings = run_checks(ws)
        assert any("refs/mgit/checkpoint/gone" in f.message and f.fixable
                   for f in findings)

        monkeypatch.chdir(ws.root)
        runner.invoke(main, ["upgrade", "--fix"])
        assert git.run_git("rev-parse", "--verify", "--quiet",
                           "refs/mgit/checkpoint/gone/cp-0001", cwd=repo_dir,
                           check=False).returncode != 0


class TestBogusRepoRegistration:
    def test_plain_dir_registration_detected_and_unregistered(
        self, initialized_workspace, runner, monkeypatch,
    ):
        ws = initialized_workspace
        (ws.root / "not-a-repo").mkdir()
        ws.repos["not-a-repo"] = RepoInfo(name="not-a-repo", path="not-a-repo")
        ws.save()

        findings = run_checks(ws)
        assert any("not-a-repo" in f.message and f.fixable for f in findings)

        monkeypatch.chdir(ws.root)
        result = runner.invoke(main, ["upgrade", "--fix"])
        assert result.exit_code == 0

        from mgit.core.workspace import Workspace
        reloaded = Workspace.find(ws.root)
        assert "not-a-repo" not in reloaded.repos
        assert (ws.root / "not-a-repo").exists()  # files untouched

    def test_missing_path_reported_not_fixed(self, initialized_workspace):
        ws = initialized_workspace
        ws.repos["vanished"] = RepoInfo(name="vanished", path="vanished")
        ws.save()
        findings = run_checks(ws)
        hits = [f for f in findings if "vanished" in f.message]
        assert hits and not hits[0].fixable


class TestMixedTargets:
    def test_reported_without_fix(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"], target_branch="release/1.2")
        # Simulate old sync divergence
        feat = fm.get("feat")
        feat.branches["repo-b"] = "feat"
        fm._save_feature(feat)

        findings = run_checks(ws)
        hits = [f for f in findings if "mixed target branches" in f.message]
        assert hits and not hits[0].fixable


class TestOrphanedDirs:
    def test_orphan_sidecar_reported(self, initialized_workspace):
        ws = initialized_workspace
        (ws.features_dir / "long-gone").mkdir(parents=True)
        (ws.features_dir / "long-gone" / "journal.jsonl").write_text("{}\n")
        findings = run_checks(ws)
        assert any("orphaned memory sidecar" in f.message for f in findings)
