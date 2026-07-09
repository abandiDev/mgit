"""Tests for per-feature working memory: state, journal, brief, fork, validation."""


import pytest

from mgit.core import git, memory
from mgit.core.feature import FeatureManager, validate_feature_name
from mgit.models.types import MemoryState
from mgit.utils.errors import MgitError


def commit_in(path, fname, content, message):
    """Write a file and commit it in the given git dir."""
    (path / fname).write_text(content)
    git.run_git("add", "-A", cwd=path)
    git.run_git("commit", "-m", message, cwd=path)


class TestMemoryState:
    def test_state_round_trip(self, initialized_workspace):
        ws = initialized_workspace
        state = MemoryState(
            goal="refactor auth", status="in progress",
            next_steps=["audit", "swap"], open_questions=["v1 support?"],
        )
        memory.save_state(ws, "feat", state)
        loaded = memory.load_state(ws, "feat")
        assert loaded.goal == "refactor auth"
        assert loaded.status == "in progress"
        assert loaded.next_steps == ["audit", "swap"]
        assert loaded.open_questions == ["v1 support?"]
        assert loaded.updated_at != ""

    def test_load_missing_returns_empty(self, initialized_workspace):
        state = memory.load_state(initialized_workspace, "does-not-exist")
        assert state.goal == ""
        assert state.next_steps == []
        assert state.updated_at == ""


class TestJournal:
    def test_append_and_read(self, initialized_workspace):
        ws = initialized_workspace
        memory.append_event(ws, "feat", "first", type_="note", actor="agent")
        memory.append_event(ws, "feat", "second", event="synced")
        entries, errors = memory.read_journal(ws, "feat")
        assert errors == 0
        assert [e["text"] for e in entries] == ["first", "second"]
        assert entries[0]["actor"] == "agent"
        assert entries[1]["event"] == "synced"

    def test_corrupt_lines_skipped_and_counted(self, initialized_workspace):
        ws = initialized_workspace
        memory.append_event(ws, "feat", "good")
        with open(memory.journal_path(ws, "feat"), "a") as f:
            f.write("{not json\n")
        memory.append_event(ws, "feat", "also good")
        entries, errors = memory.read_journal(ws, "feat")
        assert errors == 1
        assert len(entries) == 2

    def test_type_filter_and_tail(self, initialized_workspace):
        ws = initialized_workspace
        for i in range(5):
            memory.append_event(ws, "feat", f"n{i}", type_="note")
        memory.append_event(ws, "feat", "d1", type_="decision")
        decisions, _ = memory.read_journal(ws, "feat", type_filter="decision")
        assert [e["text"] for e in decisions] == ["d1"]
        tail, _ = memory.journal_tail(ws, "feat", 2)
        assert [e["text"] for e in tail] == ["n4", "d1"]


class TestAutoEvents:
    def test_start_journals_created_and_enrolled(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        entries, _ = memory.read_journal(ws, "feat")
        events = [e.get("event") for e in entries]
        assert "created" in events
        assert "repo_enrolled" in events

    def test_materialize_journals_event(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")
        entries, _ = memory.read_journal(ws, "feat")
        mat = [e for e in entries if e.get("event") == "materialized"]
        assert len(mat) == 1
        assert mat[0]["repo"] == "repo-a"
        assert mat[0]["meta"]["carried"] is False

    def test_sync_journals_event(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        (ws.root / "repo-b" / "dirty.txt").write_text("x")
        fm.sync("feat")
        entries, _ = memory.read_journal(ws, "feat")
        synced = [e for e in entries if e.get("event") == "synced"]
        assert len(synced) == 1
        assert synced[0]["meta"]["repos"] == ["repo-b"]

    def test_switch_journals_event(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.switch("feat")
        entries, _ = memory.read_journal(ws, "feat")
        assert any(e.get("event") == "switched" for e in entries)

    def test_delete_removes_sidecar(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        memory.append_event(ws, "feat", "note", type_="note")
        assert memory.memory_dir(ws, "feat").exists()
        fm.delete("feat")
        assert not memory.memory_dir(ws, "feat").exists()


class TestBrief:
    def test_brief_live_facts(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        commit_in(wt, "new.py", "x = 1", "add new module")

        brief = memory.build_brief(ws, fm.get("feat"))
        facts = brief["repos"][0]
        assert facts["repo"] == "repo-a"
        assert facts["materialized"] is True
        assert facts["dirty"] is False
        assert facts["ahead"] == 1
        assert "add new module" in facts["last_commit"]

    def test_brief_unmaterialized_degrades(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        brief = memory.build_brief(ws, fm.get("feat"))
        facts = brief["repos"][0]
        assert facts["materialized"] is False
        assert facts["dirty"] is None

    def test_render_contains_memory_and_journal(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        memory.save_state(ws, "feat", MemoryState(goal="the goal", status="going"))
        memory.append_event(ws, "feat", "a decision", type_="decision", actor="agent")
        text = memory.render_brief(memory.build_brief(ws, fm.get("feat")))
        assert "the goal" in text
        assert "a decision" in text
        assert "repo-a" in text


class TestNameValidation:
    @pytest.mark.parametrize("bad", ["a/b", "-x", ".hidden", "a..b", "x.lock", "", "a b"])
    def test_rejects_hostile_names(self, bad):
        with pytest.raises(MgitError):
            validate_feature_name(bad)

    @pytest.mark.parametrize("good", ["feat", "my-feat", "v1.2", "a_b", "feat-2"])
    def test_accepts_normal_names(self, good):
        validate_feature_name(good)

    def test_create_rejects_slash(self, initialized_workspace):
        fm = FeatureManager(initialized_workspace)
        with pytest.raises(MgitError):
            fm.create("bad/name")


class TestFork:
    def test_fork_pins_shas_and_lineage(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a", "repo-b"])
        fm.materialize("parent", "repo-a")

        child = fm.fork("child", "parent")
        assert child.parent == "parent"
        assert set(child.branches) == {"repo-a", "repo-b"}
        # Child targets its own remote branch by default
        assert child.branches["repo-a"] == "child"
        assert set(child.fork_base) == {"repo-a", "repo-b"}
        # Persisted round-trip
        loaded = fm.get("child")
        assert loaded.parent == "parent"
        assert loaded.fork_base == child.fork_base
        # Fork sets the child active
        assert ws.get_active_feature() == "child"

    def test_fork_child_branches_at_fork_base_after_parent_advances(
        self, initialized_workspace
    ):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a"])
        wt = fm.materialize("parent", "repo-a")
        commit_in(wt, "before.py", "1", "before fork")
        pinned = git.run_git("rev-parse", "HEAD", cwd=wt).stdout.strip()

        fm.fork("child", "parent")
        # Parent advances after the fork
        commit_in(wt, "after.py", "2", "after fork")

        child_wt = fm.materialize("child", "repo-a")
        child_head = git.run_git("rev-parse", "HEAD", cwd=child_wt).stdout.strip()
        assert child_head == pinned
        assert (child_wt / "before.py").exists()
        assert not (child_wt / "after.py").exists()

    def test_fork_seeds_memory_and_journals_both_sides(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a"])
        memory.save_state(ws, "parent", MemoryState(goal="shared goal"))

        fm.fork("child", "parent")
        assert memory.load_state(ws, "child").goal == "shared goal"
        child_events = [e.get("event") for e in memory.read_journal(ws, "child")[0]]
        parent_events = [e.get("event") for e in memory.read_journal(ws, "parent")[0]]
        assert "branched_from" in child_events
        assert "forked" in parent_events

    def test_fork_carry_wip(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a"])
        wt = fm.materialize("parent", "repo-a")
        (wt / "wip.py").write_text("work in progress")

        fm.fork("child", "parent", carry_wip=True)
        child_wt = fm.materialize("child", "repo-a")

        # WIP re-materialized as dirty files in the child
        assert (child_wt / "wip.py").read_text() == "work in progress"
        assert git.run_git("status", "--porcelain", cwd=child_wt).stdout.strip()
        # Parent worktree untouched (still dirty with its own WIP)
        assert (wt / "wip.py").exists()

    def test_fork_missing_parent_raises(self, initialized_workspace):
        fm = FeatureManager(initialized_workspace)
        with pytest.raises(MgitError):
            fm.fork("child", "no-such-parent")
