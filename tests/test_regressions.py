"""Regression tests from the post-release bug sweep."""

from mgit.cli.feature import _ts_sort_key
from mgit.core import git, memory
from mgit.core.checkpoint import CheckpointManager
from mgit.core.feature import FeatureManager


class TestTimestampSort:
    def test_mixed_timezone_forms_sort_chronologically(self):
        # 14:03+05:30 is 08:33Z — chronologically BEFORE 08:38Z, but the raw
        # strings compare the other way around
        journal = {"ts": "2026-07-08T08:38:04Z"}
        commit = {"ts": "2026-07-08T14:03:12+05:30"}
        assert sorted([journal, commit], key=_ts_sort_key) == [commit, journal]

    def test_unparsable_ts_sorts_first_not_crashes(self):
        entries = [{"ts": "garbage"}, {"ts": "2026-07-08T08:38:04Z"}]
        assert sorted(entries, key=_ts_sort_key)[0]["ts"] == "garbage"


class TestCheckpointIdStability:
    def test_ids_not_reused_after_delete(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        cm = CheckpointManager(ws)

        assert cm.save(fm, "feat").id == "cp-0001"
        assert cm.save(fm, "feat").id == "cp-0002"
        cm.delete("feat", "cp-0002")
        # A new save must NOT become a different cp-0002 — journal references
        # to the deleted checkpoint would become ambiguous
        assert cm.save(fm, "feat").id == "cp-0003"

    def test_ids_advance_even_after_all_deleted(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        cm = CheckpointManager(ws)

        cp = cm.save(fm, "feat")
        cm.delete("feat", cp.id)
        assert cm.save(fm, "feat").id == "cp-0002"


class TestSyncSemantics:
    def test_sync_inherits_uniform_target_branch(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"], target_branch="release/1.2")
        (ws.root / "repo-b" / "dirty.txt").write_text("x")

        fm.sync("feat")
        feat = fm.get("feat")
        assert feat.branches["repo-b"] == "release/1.2"

    def test_sync_does_not_move_active_pointer(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat-a", ["repo-a"])
        fm.start("feat-b", [])  # active is now feat-b
        assert ws.get_active_feature() == "feat-b"

        (ws.root / "repo-b" / "dirty.txt").write_text("x")
        fm.sync("feat-a")  # enrollment maintenance on a non-active feature
        assert ws.get_active_feature() == "feat-b"


class TestWorktreeRepoDetection:
    def test_detect_repo_inside_worktree(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        assert ws.detect_repo_from_cwd(wt) == "repo-a"
        sub = wt / "deep" / "dir"
        sub.mkdir(parents=True)
        assert ws.detect_repo_from_cwd(sub) == "repo-a"

    def test_detect_repo_outside_still_works(self, initialized_workspace):
        ws = initialized_workspace
        assert ws.detect_repo_from_cwd(ws.root / "repo-b") == "repo-b"
        assert ws.detect_repo_from_cwd(ws.root) is None


class TestRestoreBranchDrift:
    def test_restore_returns_worktree_to_checkpointed_branch(
        self, initialized_workspace,
    ):
        """Restore must not rewrite whatever branch the user switched to."""
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        cm = CheckpointManager(ws)
        cp = cm.save(fm, "feat")

        # User switches the worktree to an experiment branch and commits
        git.run_git("checkout", "-b", "experiment", cwd=wt)
        (wt / "exp.py").write_text("y")
        git.run_git("add", "-A", cwd=wt)
        git.run_git("commit", "-m", "experiment commit", cwd=wt)
        exp_head = git.run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()

        cm.restore(fm, "feat", cp.id)

        # Worktree is back on the sandbox branch at the checkpointed head
        branch = git.run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt).stdout.strip()
        assert branch == "mgit/feat"
        head = git.run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        assert head == cp.repos["repo-a"].head
        # The experiment branch keeps its commit — it was NOT rewritten
        exp = git.run_git("rev-parse", "refs/heads/experiment", cwd=wt).stdout.strip()
        assert exp == exp_head


class TestUnregisteredRepoGuards:
    def test_checkpoint_save_skips_unregistered_repo(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        fm.materialize("feat", "repo-a")
        fm.materialize("feat", "repo-b")
        ws.remove_repo("repo-b")  # workspace-level removal, still enrolled

        cp = CheckpointManager(ws).save(fm, "feat")
        assert "repo-a" in cp.repos
        assert "repo-b" not in cp.repos

    def test_fork_skips_unregistered_repo(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        ws.remove_repo("repo-b")

        child = fm.fork("feat-2", "feat")
        assert "repo-a" in child.branches
        assert "repo-b" not in child.branches

    def test_feature_delete_survives_unregistered_repo(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        ws.remove_repo("repo-b")
        fm.delete("feat")  # must not raise


class TestCarryRollback:
    def test_failed_worktree_creation_restores_stash(self, initialized_workspace):
        """A failed materialize --carry must put the user's changes back."""
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        repo_dir = ws.root / "repo-a"
        (repo_dir / "precious.txt").write_text("my uncommitted work")

        # Sabotage: check the sandbox branch out in the origin repo, so
        # 'git worktree add' fails with 'already checked out' AFTER the
        # carry stash has been taken
        git.run_git("checkout", "-b", "mgit/feat", cwd=repo_dir)

        import pytest as _pytest
        with _pytest.raises(Exception):
            fm.materialize("feat", "repo-a", carry=True)

        # The dirty file is back in the origin repo, not lost in a stash
        assert (repo_dir / "precious.txt").read_text() == "my uncommitted work"
        stashes = git.run_git("stash", "list", cwd=repo_dir).stdout
        assert "mgit-carry" not in stashes

    def test_rematerialize_after_manual_rmrf(self, initialized_workspace):
        """rm -rf'd worktree dirs stay registered; prune must recover."""
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        import shutil as _shutil
        _shutil.rmtree(wt)

        wt2 = fm.materialize("feat", "repo-a")  # must not raise
        assert wt2.exists()


class TestSandboxBranchCleanup:
    def test_recreated_feature_does_not_resurrect_old_commits(
        self, initialized_workspace,
    ):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        (wt / "secret-old-work.txt").write_text("x")
        git.run_git("add", "-A", cwd=wt)
        git.run_git("commit", "-m", "old feature commit", cwd=wt)
        fm.delete("feat", force=True)  # unmerged sandbox commit by design here

        fm.start("feat", ["repo-a"])
        wt2 = fm.materialize("feat", "repo-a")
        assert not (wt2 / "secret-old-work.txt").exists()


class TestScanNestedRepos:
    def test_plain_dir_inside_outer_repo_not_registered(self, tmp_path):
        import subprocess
        from mgit.core.workspace import Workspace

        outer = tmp_path / "outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        wsdir = outer / "workspace"
        wsdir.mkdir()
        (wsdir / "not-a-repo").mkdir()
        real = wsdir / "real-repo"
        real.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=real, check=True)
        subprocess.run(["git", "-C", str(real), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(real), "config", "user.name", "T"], check=True)
        (real / "f").write_text("x")
        subprocess.run(["git", "-C", str(real), "add", "."], check=True)
        subprocess.run(["git", "-C", str(real), "commit", "-qm", "init"], check=True)

        ws, found = Workspace.init(wsdir, scan=True, auto_add=False)
        names = {r.name for r in found}
        assert "real-repo" in names
        assert "not-a-repo" not in names


class TestTomlSuffixName:
    def test_toml_suffix_feature_name_rejected(self, initialized_workspace):
        import pytest as _pytest
        from mgit.utils.errors import MgitError
        fm = FeatureManager(initialized_workspace)
        with _pytest.raises(MgitError):
            fm.create("foo.toml")


class TestExecPassthrough:
    def test_option_like_tokens_reach_the_command(self, initialized_workspace,
                                                  monkeypatch):
        from click.testing import CliRunner
        from mgit.cli import main
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        runner = CliRunner()

        # --fail-fast after the command must go to echo, not to mgit
        result = runner.invoke(main, ["exec", "echo", "--fail-fast", "hello"])
        assert result.exit_code == 0
        assert "--fail-fast hello" in result.output

        # -f after the command must not be parsed as a feature scope
        result = runner.invoke(main, ["exec", "echo", "-f", "x.txt"])
        assert result.exit_code == 0
        assert "-f x.txt" in result.output

    def test_mgit_options_before_command_still_work(self, initialized_workspace,
                                                    monkeypatch):
        from click.testing import CliRunner
        from mgit.cli import main
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        runner = CliRunner()

        result = runner.invoke(main, ["exec", "-r", "repo-a", "echo", "hi"])
        assert result.exit_code == 0
        assert "repo-a" in result.output
        assert "repo-b" not in result.output


class TestBulkDomainErrors:
    def _invoke_entry(self, monkeypatch, capsys, args):
        import sys as _sys
        from mgit.cli import cli
        monkeypatch.setattr(_sys, "argv", ["mgit", *args])
        code = 0
        try:
            cli()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def test_unknown_feature_scope_exits_2(self, initialized_workspace,
                                           monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, _, err = self._invoke_entry(
            monkeypatch, capsys, ["status", "-f", "nosuch"])
        assert code == 2
        assert "not found" in err

    def test_unknown_feature_scope_json_envelope(self, initialized_workspace,
                                                 monkeypatch, capsys):
        import json as _json
        monkeypatch.chdir(initialized_workspace.root)
        code, out, _ = self._invoke_entry(
            monkeypatch, capsys, ["status", "-f", "nosuch", "--json"])
        assert code == 2
        env = _json.loads(out)
        assert env["ok"] is False
        assert env["error"]["type"] == "FeatureNotFoundError"

    def test_init_on_existing_workspace_exits_2(self, initialized_workspace,
                                                monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, _, err = self._invoke_entry(monkeypatch, capsys, ["init"])
        assert code == 2
        assert "already exists" in err


class TestMaterializeIdempotencyTruth:
    def test_no_false_carried_claim_when_already_materialized(
        self, initialized_workspace, monkeypatch,
    ):
        from click.testing import CliRunner
        from mgit.cli import main
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        (ws.root / "repo-a" / "dirty.txt").write_text("x")

        runner = CliRunner()
        # Headless with no flag: must NOT refuse (nothing will be carried),
        # and must NOT claim changes were carried
        result = runner.invoke(main, ["feature", "materialize", "repo-a"])
        assert result.exit_code == 0, result.output
        assert "already materialized" in result.output
        assert "NOT carried" in result.output
        assert "(changes carried)" not in result.output
        # Origin still dirty — nothing silently vanished
        assert (ws.root / "repo-a" / "dirty.txt").exists()

    def test_start_rerun_skips_carry_for_enrolled_repos(
        self, initialized_workspace, monkeypatch, capsys,
    ):
        import sys as _sys
        from mgit.cli import cli
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        (ws.root / "repo-a" / "dirty.txt").write_text("x")
        monkeypatch.chdir(ws.root)

        # Headless re-run: repo-a is already enrolled, so no carry prompt /
        # refusal may fire for it
        monkeypatch.setattr(_sys, "argv",
                            ["mgit", "feature", "start", "feat", "-r", "repo-a", "-m"])
        code = 0
        try:
            cli()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        capsys.readouterr()
        assert code == 0


class TestStartAtomicity:
    def test_typo_repo_leaves_no_half_created_feature(self, initialized_workspace):
        import pytest as _pytest
        from mgit.utils.errors import RepoNotFoundError
        ws = initialized_workspace
        fm = FeatureManager(ws)
        with _pytest.raises(RepoNotFoundError):
            fm.start("ghost", ["nosuchrepo"])
        assert not any(f.name == "ghost" for f in fm.list())
        assert not memory.memory_dir(ws, "ghost").exists()


class TestNaiveTimestampTolerance:
    def test_offsetless_journal_ts_does_not_crash_log(
        self, initialized_workspace, monkeypatch,
    ):
        import json as _json
        from click.testing import CliRunner
        from mgit.cli import main
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        with open(memory.journal_path(ws, "feat"), "a") as f:
            f.write(_json.dumps({"ts": "2026-07-08T11:00:00", "actor": "agent",
                                 "type": "note", "text": "naive ts"}) + "\n")
        monkeypatch.chdir(ws.root)
        runner = CliRunner()
        result = runner.invoke(main, ["feature", "log", "--commits"])
        assert result.exit_code == 0, result.output
        assert "naive ts" in result.output
