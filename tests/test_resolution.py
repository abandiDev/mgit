"""Tests for parallel-session feature resolution.

Regression: two sessions on different features must not cross-talk through
the workspace-global active pointer — notes/plan writes from a session inside
feature A's worktree must land in A's memory even when the active pointer
says B.
"""

import json

import pytest
from click.testing import CliRunner

from mgit.cli import main
from mgit.core import memory
from mgit.core.feature import FeatureManager


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def two_features(initialized_workspace):
    """feat-a (repo-a, materialized) and feat-b (repo-b); active = feat-b."""
    ws = initialized_workspace
    fm = FeatureManager(ws)
    fm.start("feat-a", ["repo-a"])
    fm.materialize("feat-a", "repo-a")
    fm.start("feat-b", ["repo-b"])  # start switches active to feat-b
    assert ws.get_active_feature() == "feat-b"
    return ws, fm


class TestResolvePriority:
    def test_cwd_inside_worktree_beats_active(self, two_features, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        assert ws.resolve_feature() == "feat-a"

    def test_env_beats_cwd(self, two_features, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        monkeypatch.setenv("MGIT_FEATURE", "feat-b")
        assert ws.resolve_feature() == "feat-b"

    def test_explicit_beats_env(self, two_features, monkeypatch):
        ws, _ = two_features
        monkeypatch.setenv("MGIT_FEATURE", "feat-b")
        assert ws.resolve_feature("feat-a") == "feat-a"

    def test_dot_falls_through_to_session_resolution(self, two_features, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        assert ws.resolve_feature(".") == "feat-a"

    def test_fallback_is_active_pointer(self, two_features, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.root)
        assert ws.resolve_feature() == "feat-b"

    def test_stale_worktree_dir_ignored(self, two_features, monkeypatch):
        ws, _ = two_features
        stale = ws.worktrees_dir / "deleted-feature" / "x"
        stale.mkdir(parents=True)
        monkeypatch.chdir(stale)
        # No feature TOML for that dir -> falls back to active
        assert ws.detect_feature_from_cwd() is None
        assert ws.resolve_feature() == "feat-b"


class TestCrossTalkRegression:
    def test_note_from_worktree_lands_in_that_feature(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        # Session A works inside feat-a's worktree while active = feat-b
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        result = runner.invoke(main, ["feature", "note", "session A note"])
        assert result.exit_code == 0
        assert "'feat-a'" in result.output

        a_entries, _ = memory.read_journal(ws, "feat-a")
        b_entries, _ = memory.read_journal(ws, "feat-b")
        assert any(e["text"] == "session A note" for e in a_entries)
        assert not any(e["text"] == "session A note" for e in b_entries)

    def test_plan_from_worktree_updates_that_feature(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        from mgit.models.types import MemoryState
        memory.save_state(ws, "feat-b", MemoryState(status="b's status"))

        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        result = runner.invoke(main, ["feature", "plan", "--status", "a's status"])
        assert result.exit_code == 0

        assert memory.load_state(ws, "feat-a").status == "a's status"
        assert memory.load_state(ws, "feat-b").status == "b's status"  # untouched

    def test_env_pins_feature_from_workspace_root(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        monkeypatch.chdir(ws.root)
        monkeypatch.setenv("MGIT_FEATURE", "feat-a")
        result = runner.invoke(main, ["feature", "note", "pinned note"])
        assert result.exit_code == 0
        a_entries, _ = memory.read_journal(ws, "feat-a")
        assert any(e["text"] == "pinned note" for e in a_entries)

    def test_bulk_dot_scope_resolves_from_worktree(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        result = runner.invoke(main, ["status", "-f", ".", "--json"])
        assert result.exit_code == 0
        env = json.loads(result.output)
        repos = {r["repo"] for r in env["data"]["results"]}
        assert repos == {"repo-a"}  # feat-a's repos, not active feat-b's

    def test_brief_from_worktree_shows_that_feature(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        monkeypatch.chdir(ws.worktree_path("feat-a", "repo-a"))
        result = runner.invoke(main, ["feature", "brief"])
        assert result.exit_code == 0
        assert "# Feature: feat-a" in result.output


class TestNoActivate:
    def test_start_no_activate_keeps_pointer(self, two_features, runner, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.root)
        result = runner.invoke(
            main, ["feature", "start", "feat-c", "-r", "repo-a", "--no-activate"],
        )
        assert result.exit_code == 0
        assert ws.get_active_feature() == "feat-b"

    def test_fork_no_activate_keeps_pointer(self, two_features, runner, monkeypatch):
        ws, _ = two_features
        monkeypatch.chdir(ws.root)
        result = runner.invoke(
            main, ["feature", "fork", "feat-a-v2", "--from", "feat-a", "--no-activate"],
        )
        assert result.exit_code == 0, result.output
        assert ws.get_active_feature() == "feat-b"


class TestPlanRecoverability:
    def test_plan_updated_event_carries_full_state(
        self, two_features, runner, monkeypatch,
    ):
        ws, _ = two_features
        monkeypatch.chdir(ws.root)
        result = runner.invoke(main, [
            "feature", "plan", "-f", "feat-a",
            "--goal", "recoverable goal", "--next", "step",
        ])
        assert result.exit_code == 0

        entries, _ = memory.read_journal(ws, "feat-a")
        updates = [e for e in entries if e.get("event") == "plan_updated"]
        assert updates
        snap = updates[-1]["meta"]
        assert snap["goal"] == "recoverable goal"
        assert snap["next_steps"] == ["step"]
