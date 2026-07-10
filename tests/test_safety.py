"""Destructive paths must not silently lose work, and mgit must not lie to users.

Every test here corresponds to a defect found by driving mgit against a real
single-repo project (LeetSketch2, a Next.js app whose entire source was
uncommitted). See docs/ for the write-up.
"""

import re
import subprocess

import pytest
from click.testing import CliRunner

from mgit.cli import main
from mgit.core import git
from mgit.core.feature import FeatureManager, rescue_ref
from mgit.core.repo import add_repo_from_path
from mgit.core.workspace import (
    MGIT_BLOCK_BEGIN,
    MGIT_BLOCK_END,
    Workspace,
)
from mgit.utils.errors import MgitError


def _dirty_worktree(ws, feature="feat", repo="repo-a"):
    fm = FeatureManager(ws)
    fm.start(feature, [repo])
    wt = fm.materialize(feature, repo)
    (wt / "unsaved.txt").write_text("work nobody committed")
    return fm, wt


def _committed_worktree(ws, feature="feat", repo="repo-a"):
    fm = FeatureManager(ws)
    fm.start(feature, [repo])
    wt = fm.materialize(feature, repo)
    (wt / "work.txt").write_text("a week of work")
    git.run_git("add", "-A", cwd=wt)
    git.run_git("commit", "-m", "important", cwd=wt)
    return fm, wt


class TestDeleteGuards:
    def test_delete_refuses_dirty_worktree(self, initialized_workspace):
        ws = initialized_workspace
        fm, _ = _dirty_worktree(ws)
        with pytest.raises(MgitError, match="Refusing to destroy work"):
            fm.delete("feat")
        assert fm.get("feat") is not None  # still there

    def test_delete_refuses_unmerged_commits(self, initialized_workspace):
        ws = initialized_workspace
        fm, _ = _committed_worktree(ws)
        with pytest.raises(MgitError, match="unmerged commit"):
            fm.delete("feat")

    def test_delete_of_clean_merged_feature_needs_no_force(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        assert fm.delete("feat") == []  # nothing at risk, nothing rescued

    def test_forced_delete_pins_recoverable_rescue_ref(self, initialized_workspace):
        ws = initialized_workspace
        fm, _ = _dirty_worktree(ws)
        rescued = fm.delete("feat", force=True)
        assert rescued, "forced delete must pin a rescue ref"

        origin = ws.root / "repo-a"
        ref = rescue_ref("feat", "repo-a")
        blob = git.run_git("show", f"{ref}:unsaved.txt", cwd=origin, check=False)
        assert blob.returncode == 0
        assert "work nobody committed" in blob.stdout

    def test_rescue_survives_gc(self, initialized_workspace):
        """The whole point: a rescue ref is reachable, so gc cannot reclaim it."""
        ws = initialized_workspace
        fm, _ = _dirty_worktree(ws)
        fm.delete("feat", force=True)
        origin = ws.root / "repo-a"
        subprocess.run(["git", "gc", "--prune=now", "--quiet"], cwd=origin, check=True)
        ref = rescue_ref("feat", "repo-a")
        assert git.run_git("rev-parse", "--verify", ref, cwd=origin, check=False).returncode == 0


class TestRemoveRepoGuards:
    """remove-repo reads like a metadata edit but destroys a worktree + branch."""

    def test_remove_repo_refuses_unmerged_commits(self, initialized_workspace):
        ws = initialized_workspace
        fm, _ = _committed_worktree(ws)
        with pytest.raises(MgitError, match="Refusing to destroy work"):
            fm.remove_repo("feat", "repo-a")

    def test_forced_remove_repo_rescues_the_commit(self, initialized_workspace):
        ws = initialized_workspace
        fm, wt = _committed_worktree(ws)
        sha = git.run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        _, rescued = fm.remove_repo("feat", "repo-a", force=True)
        assert rescued

        origin = ws.root / "repo-a"
        ref = rescue_ref("feat", "repo-a")
        # the orphaned commit is an ancestor of the rescue snapshot
        merge_base = git.run_git(
            "merge-base", "--is-ancestor", sha, ref, cwd=origin, check=False
        )
        assert merge_base.returncode == 0

    def test_cli_remove_repo_says_what_it_destroyed(self, initialized_workspace, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        result = CliRunner().invoke(main, ["feature", "remove-repo", "feat", "repo-a"])
        assert result.exit_code == 0
        assert "worktree and sandbox branch deleted" in result.output


class TestAgentFileOwnership:
    """AGENTS.md is a shared standard; mgit owns only its sentinel block."""

    def test_init_merges_into_foreign_agents_md(self, tmp_path):
        foreign = "<!-- BEGIN:nextjs-agent-rules -->\nRead the docs.\n"
        (tmp_path / "AGENTS.md").write_text(foreign)
        Workspace.init(tmp_path, scan=False)
        text = (tmp_path / "AGENTS.md").read_text()
        assert "Read the docs." in text          # theirs survives
        assert MGIT_BLOCK_BEGIN in text          # ours is added
        assert MGIT_BLOCK_END in text

    def test_rewrite_is_idempotent(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Keep me.\n")
        ws, _ = Workspace.init(tmp_path, scan=False)
        ws.write_agent_md()
        ws.write_agent_md()
        text = (tmp_path / "AGENTS.md").read_text()
        assert text.count(MGIT_BLOCK_BEGIN) == 1
        assert text.count("Keep me.") == 1

    def test_remove_strips_block_but_keeps_foreign_content(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Keep me.\n")
        ws, _ = Workspace.init(tmp_path, scan=False)
        ws.remove()
        text = (tmp_path / "AGENTS.md").read_text()
        assert "Keep me." in text
        assert MGIT_BLOCK_BEGIN not in text

    def test_remove_deletes_a_file_that_was_only_ours(self, tmp_path):
        ws, _ = Workspace.init(tmp_path, scan=False)
        assert (tmp_path / "AGENT.md").exists()
        ws.remove()
        assert not (tmp_path / "AGENT.md").exists()


class TestSelfReferentialRepo:
    def test_repo_add_rejects_the_workspace_root(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "i"], cwd=tmp_path, check=True)
        Workspace.init(tmp_path, scan=False)
        with pytest.raises(ValueError, match="cannot register itself"):
            add_repo_from_path(tmp_path, tmp_path)


class TestUserFacingStringsNameRealCommands:
    """mgit told users to run 'mgit feature create' and 'mgit skill review'.

    Neither existed. Both were caught only by a human reading output. This test
    is the mechanical check: any `mgit <words>` mgit prints must be runnable.
    """

    # `mgit foo bar` / `mgit foo`. Options and <placeholders> stop the match.
    PATTERN = re.compile(r"mgit ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)?)")

    def _verdict(self, parts):
        """True/False if `parts` names a command; None if it isn't a reference."""
        from mgit.cli import main as root

        if parts[0] not in root.commands:
            return None  # e.g. "mgit never stores state" — prose, not a command
        cmd = root.commands[parts[0]]
        if len(parts) == 1:
            return True
        if not hasattr(cmd, "commands"):
            return True  # leaf command; the next word is an argument or prose
        # A group MUST be followed by a real subcommand. This is the check that
        # 'feature create' and 'skill review' both needed and never had.
        return parts[1] in cmd.commands

    def test_every_referenced_command_exists(self):
        import pathlib

        src = pathlib.Path(__file__).resolve().parent.parent / "src" / "mgit"
        bad = []
        for py in sorted(src.rglob("*.py")):
            for match in self.PATTERN.finditer(py.read_text()):
                phrase = match.group(1)
                if self._verdict(phrase.split()) is False:
                    bad.append(f"{py.relative_to(src.parent.parent)}: 'mgit {phrase}'")
        assert not bad, "user-facing text names commands that do not exist:\n" + "\n".join(bad)


class TestEphemeralWorkspaceRefused:
    """A workspace under /tmp holds the only copy of its own working memory.

    One was created in an agent's scratchpad; /tmp was swept, and the journal
    and memory.toml of a live feature went with it. The commits survived only
    because they had been pushed. `init` now refuses the trap by default.
    """

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

    def test_init_under_tmp_exits_2_and_writes_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("MGIT_ALLOW_EPHEMERAL")
        monkeypatch.chdir(tmp_path)
        code, _, err = self._invoke_entry(monkeypatch, capsys, ["init"])
        assert code == 2
        assert "reaps" in err
        assert not (tmp_path / ".mgit").exists()
        assert not (tmp_path / "AGENT.md").exists()

    def test_the_refusal_names_the_memory_it_is_protecting(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("MGIT_ALLOW_EPHEMERAL")
        monkeypatch.chdir(tmp_path)
        _, _, err = self._invoke_entry(monkeypatch, capsys, ["init"])
        assert "journal.jsonl" in err
        assert "--allow-ephemeral" in err

    def test_named_workspace_under_tmp_is_refused_before_the_dir_is_made(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("MGIT_ALLOW_EPHEMERAL")
        monkeypatch.chdir(tmp_path)
        code, _, _ = self._invoke_entry(monkeypatch, capsys, ["init", "slab"])
        assert code == 2
        assert not (tmp_path / "slab").exists()

    def test_the_flag_is_the_escape_hatch(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("MGIT_ALLOW_EPHEMERAL")
        monkeypatch.chdir(tmp_path)
        code, _, _ = self._invoke_entry(
            monkeypatch, capsys, ["init", "--no-interactive", "--allow-ephemeral"]
        )
        assert code == 0
        assert (tmp_path / ".mgit").is_dir()

    def test_the_env_var_is_the_escape_hatch_for_test_and_e2e_sandboxes(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("MGIT_ALLOW_EPHEMERAL", "1")
        monkeypatch.chdir(tmp_path)
        code, _, _ = self._invoke_entry(monkeypatch, capsys, ["init", "--no-interactive"])
        assert code == 0
        assert (tmp_path / ".mgit").is_dir()

    def test_env_var_set_to_zero_does_not_allow(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("MGIT_ALLOW_EPHEMERAL", "0")
        monkeypatch.chdir(tmp_path)
        code, _, _ = self._invoke_entry(monkeypatch, capsys, ["init", "--no-interactive"])
        assert code == 2

    def test_a_durable_path_is_not_ephemeral(self):
        import pathlib

        from mgit.core.workspace import ephemeral_root

        repo = pathlib.Path(__file__).resolve().parent.parent
        assert ephemeral_root(repo) is None

    def test_tmp_itself_and_its_children_are_ephemeral(self, tmp_path):
        import pathlib

        from mgit.core.workspace import ephemeral_root

        assert ephemeral_root(pathlib.Path("/tmp")) == pathlib.Path("/tmp").resolve()
        assert ephemeral_root(tmp_path) is not None
        # the path need not exist yet: init checks before it creates
        assert ephemeral_root(tmp_path / "not-created-yet") is not None
