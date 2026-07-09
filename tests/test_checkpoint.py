"""Tests for cross-repo checkpoints: save/restore, refs, cleanup."""

import pytest

from mgit.core import git, memory
from mgit.core.checkpoint import CheckpointManager, checkpoint_ref
from mgit.core.feature import FeatureManager
from mgit.models.types import MemoryState
from mgit.utils.errors import MgitError


def commit_in(path, fname, content, message):
    (path / fname).write_text(content)
    git.run_git("add", "-A", cwd=path)
    git.run_git("commit", "-m", message, cwd=path)


@pytest.fixture
def feature_workspace(initialized_workspace):
    """Workspace with a materialized feature on repo-a."""
    ws = initialized_workspace
    fm = FeatureManager(ws)
    fm.start("feat", ["repo-a", "repo-b"])
    fm.materialize("feat", "repo-a")
    return ws, fm


class TestCheckpointSave:
    def test_save_clean_pins_head(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        cp = cm.save(fm, "feat", label="clean point")

        assert cp.id == "cp-0001"
        st = cp.repos["repo-a"]
        assert st.dirty is False
        assert st.snapshot == st.head
        # Unmaterialized repo-b is not covered
        assert "repo-b" not in cp.repos

    def test_save_dirty_snapshots_without_touching_worktree(self, feature_workspace):
        ws, fm = feature_workspace
        wt = ws.worktree_path("feat", "repo-a")
        (wt / "untracked.py").write_text("new")
        (wt / "README.md").write_text("modified")

        cp = CheckpointManager(ws).save(fm, "feat")
        st = cp.repos["repo-a"]
        assert st.dirty is True
        assert st.snapshot != st.head

        # Non-destructive: worktree still dirty exactly as before
        porcelain = git.run_git("status", "--porcelain", cwd=wt).stdout
        assert "untracked.py" in porcelain
        assert "README.md" in porcelain

        # Snapshot pinned by a ref in the origin repo's shared ref store
        origin = ws.root / "repo-a"
        ref_sha = git.run_git(
            "rev-parse", checkpoint_ref("feat", cp.id), cwd=origin,
        ).stdout.strip()
        assert ref_sha == st.snapshot

    def test_save_journals_event(self, feature_workspace):
        ws, fm = feature_workspace
        CheckpointManager(ws).save(fm, "feat", label="x")
        entries, _ = memory.read_journal(ws, "feat")
        assert any(e.get("event") == "checkpoint" for e in entries)


class TestCheckpointRestore:
    def test_restore_round_trip(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        wt = ws.worktree_path("feat", "repo-a")

        commit_in(wt, "committed.py", "v1", "committed work")
        (wt / "wip.py").write_text("uncommitted work")
        cp = cm.save(fm, "feat", label="good state")

        # Wreck everything: new commit, delete WIP, add junk
        commit_in(wt, "regret.py", "bad", "regrettable commit")
        (wt / "wip.py").unlink()
        (wt / "junk.txt").write_text("junk")

        restored, safety, untouched = cm.restore(fm, "feat", cp.id)

        head = git.run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()
        assert head == cp.repos["repo-a"].head
        assert (wt / "wip.py").read_text() == "uncommitted work"
        assert not (wt / "regret.py").exists()
        assert not (wt / "junk.txt").exists()
        # WIP is dirty again, not committed
        assert "wip.py" in git.run_git("status", "--porcelain", cwd=wt).stdout

        # Safety checkpoint captured the pre-restore state
        assert safety.id != cp.id
        assert cm.get("feat", safety.id).repos["repo-a"].dirty is True

    def test_restore_rewinds_memory(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        memory.save_state(ws, "feat", MemoryState(status="before"))
        cp = cm.save(fm, "feat")
        memory.save_state(ws, "feat", MemoryState(status="after"))

        cm.restore(fm, "feat", cp.id)
        assert memory.load_state(ws, "feat").status == "before"

    def test_restore_reports_untouched_repos(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        cp = cm.save(fm, "feat")
        # Materialize repo-b after the checkpoint
        fm.materialize("feat", "repo-b")
        _, _, untouched = cm.restore(fm, "feat", cp.id)
        assert untouched == ["repo-b"]

    def test_restore_missing_raises(self, feature_workspace):
        ws, fm = feature_workspace
        with pytest.raises(MgitError):
            CheckpointManager(ws).restore(fm, "feat", "cp-9999")


class TestCheckpointCleanup:
    def test_delete_checkpoint_unpins_ref(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        wt = ws.worktree_path("feat", "repo-a")
        (wt / "wip.py").write_text("x")
        cp = cm.save(fm, "feat")

        origin = ws.root / "repo-a"
        assert git.run_git(
            "rev-parse", "--verify", "--quiet", checkpoint_ref("feat", cp.id),
            cwd=origin, check=False,
        ).returncode == 0

        cm.delete("feat", cp.id)
        assert git.run_git(
            "rev-parse", "--verify", "--quiet", checkpoint_ref("feat", cp.id),
            cwd=origin, check=False,
        ).returncode != 0
        with pytest.raises(MgitError):
            cm.get("feat", cp.id)

    def test_feature_delete_cleans_manifests_and_refs(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        wt = ws.worktree_path("feat", "repo-a")
        (wt / "wip.py").write_text("x")
        cp = cm.save(fm, "feat")
        origin = ws.root / "repo-a"

        fm.delete("feat", force=True)  # worktree is dirty by design here

        assert not (ws.checkpoints_dir / "feat").exists()
        assert git.run_git(
            "rev-parse", "--verify", "--quiet", checkpoint_ref("feat", cp.id),
            cwd=origin, check=False,
        ).returncode != 0

    def test_ids_allocate_sequentially(self, feature_workspace):
        ws, fm = feature_workspace
        cm = CheckpointManager(ws)
        assert cm.save(fm, "feat").id == "cp-0001"
        assert cm.save(fm, "feat").id == "cp-0002"
        assert [c.id for c in cm.list("feat")] == ["cp-0001", "cp-0002"]
