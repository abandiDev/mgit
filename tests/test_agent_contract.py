"""Tests for the agent contract: exit codes, JSON envelopes, non-TTY guards,
generated ambient briefs, upgrade, and the memory/fork CLI commands."""

import json
import sys

import pytest
from click.testing import CliRunner

from mgit.cli import cli, main
from mgit.core import memory
from mgit.core.feature import FeatureManager
from mgit.utils import output


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def reset_json_mode():
    output.set_json_mode(False)
    yield
    output.set_json_mode(False)


def invoke_entry(monkeypatch, capsys, args):
    """Invoke the real entry point (cli) to test exit-code mapping."""
    monkeypatch.setattr(sys, "argv", ["mgit", *args])
    code = 0
    try:
        cli()
    except SystemExit as e:
        code = e.code if e.code is not None else 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestExitCodes:
    def test_success_exits_0(self, initialized_workspace, monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, _, _ = invoke_entry(monkeypatch, capsys, ["feature", "list"])
        assert code == 0

    def test_domain_error_exits_2(self, initialized_workspace, monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, _, err = invoke_entry(monkeypatch, capsys, ["feature", "show", "nope"])
        assert code == 2
        assert "not found" in err

    def test_usage_error_exits_3(self, initialized_workspace, monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, _, _ = invoke_entry(monkeypatch, capsys, ["feature", "bogus-cmd"])
        assert code == 3

    def test_no_traceback_on_domain_error(self, initialized_workspace, monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        _, out, err = invoke_entry(monkeypatch, capsys, ["feature", "show", "nope"])
        assert "Traceback" not in out + err


class TestJsonEnvelope:
    def test_brief_json_envelope(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])

        result = runner.invoke(main, ["feature", "brief", "--json"])
        assert result.exit_code == 0
        env = json.loads(result.output)
        assert env["mgit_schema"] == 1
        assert env["ok"] is True
        assert env["command"] == "feature.brief"
        assert env["data"]["feature"] == "feat"
        assert env["error"] is None

    def test_error_envelope_on_json_command(self, initialized_workspace, monkeypatch, capsys):
        monkeypatch.chdir(initialized_workspace.root)
        code, out, _ = invoke_entry(
            monkeypatch, capsys, ["feature", "brief", "--json", "-f", "nope"],
        )
        assert code == 2
        env = json.loads(out)
        assert env["ok"] is False
        assert env["error"]["type"] == "FeatureNotFoundError"

    def test_bulk_status_json(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        result = runner.invoke(main, ["status", "--json"])
        assert result.exit_code == 0
        env = json.loads(result.output)
        assert env["command"] == "status"
        assert env["data"]["summary"]["succeeded"] == 2
        repos = {r["repo"] for r in env["data"]["results"]}
        assert repos == {"repo-a", "repo-b"}

    def test_context_has_schema_and_memory(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])

        result = runner.invoke(main, ["context"])
        data = json.loads(result.output)
        assert data["mgit_schema"] == 1
        assert data["active_feature"]["memory"]["goal"] == ""
        assert data["active_feature"]["parent"] is None
        assert data["active_feature"]["checkpoint_count"] == 0
        feat = data["features"][0]
        assert feat["parent"] is None
        assert "status" in feat and "updated_at" in feat

    def test_context_feature_scope_deep(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")

        result = runner.invoke(main, ["context", "-f", "."])
        data = json.loads(result.output)
        assert data["feature"]["name"] == "feat"
        facts = data["feature"]["repo_facts"][0]
        assert facts["materialized"] is True
        assert facts["dirty"] is False


class TestNonTTYGuard:
    def test_materialize_dirty_without_flag_refuses(
        self, initialized_workspace, monkeypatch, capsys,
    ):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])
        (ws.root / "repo-a" / "dirty.txt").write_text("x")

        # pytest's stdin is not a TTY — the prompt path must refuse, not hang
        code, _, err = invoke_entry(
            monkeypatch, capsys, ["feature", "materialize", "repo-a"],
        )
        assert code == 2
        assert "--carry" in err

    def test_materialize_with_flag_proceeds_headless(
        self, initialized_workspace, monkeypatch, capsys,
    ):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])
        (ws.root / "repo-a" / "dirty.txt").write_text("x")

        code, out, _ = invoke_entry(
            monkeypatch, capsys, ["feature", "materialize", "repo-a", "--carry"],
        )
        assert code == 0
        assert "changes carried" in out


class TestGeneratedBriefs:
    def test_materialize_writes_ambient_files(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        fm.materialize("feat", "repo-a")

        d = ws.worktrees_dir / "feat"
        for fname in ("CLAUDE.md", "AGENTS.md"):
            content = (d / fname).read_text()
            assert "generated by mgit" in content
            assert "# Feature: feat" in content
            assert "mgit feature brief" in content

    def test_note_refreshes_ambient_brief(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])

        result = runner.invoke(
            main, ["feature", "note", "key decision here", "--type", "decision"],
        )
        assert result.exit_code == 0
        content = (ws.worktrees_dir / "feat" / "CLAUDE.md").read_text()
        assert "key decision here" in content

    def test_delete_removes_ambient_files(self, initialized_workspace):
        ws = initialized_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        assert (ws.worktrees_dir / "feat" / "CLAUDE.md").exists()
        fm.delete("feat")
        assert not (ws.worktrees_dir / "feat").exists()


class TestPlanCommand:
    def test_plan_updates_and_journals(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])

        result = runner.invoke(main, [
            "feature", "plan", "--goal", "big goal",
            "--status", "started", "--next", "step one", "--next", "step two",
        ])
        assert result.exit_code == 0

        state = memory.load_state(ws, "feat")
        assert state.goal == "big goal"
        assert state.next_steps == ["step one", "step two"]

        result = runner.invoke(main, ["feature", "plan", "--done", "1"])
        assert result.exit_code == 0
        state = memory.load_state(ws, "feat")
        assert state.next_steps == ["step two"]

        events = [e.get("event") for e in memory.read_journal(ws, "feat")[0]]
        assert "plan_updated" in events
        assert "step_done" in events

    def test_plan_read_only_without_flags(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])
        before = memory.load_state(ws, "feat").updated_at

        result = runner.invoke(main, ["feature", "plan"])
        assert result.exit_code == 0
        assert memory.load_state(ws, "feat").updated_at == before


class TestBulkJournaling:
    def test_commit_journals_sha_and_subject(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        (wt / "change.py").write_text("x")

        result = runner.invoke(main, ["commit", "-m", "journaled commit", "-f", "."])
        assert result.exit_code == 0

        entries, _ = memory.read_journal(ws, "feat")
        commits = [e for e in entries if e.get("event") == "commit"]
        assert len(commits) == 1
        assert commits[0]["repo"] == "repo-a"
        assert commits[0]["meta"]["subject"] == "journaled commit"
        assert commits[0]["meta"]["sha"]


class TestTreeCommand:
    def test_tree_shows_ancestry(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a"])
        fm.fork("child", "parent")

        result = runner.invoke(main, ["feature", "tree"])
        assert result.exit_code == 0
        lines = result.output.splitlines()
        assert any("parent" in line and "└─" not in line for line in lines)
        assert any("└─ child" in line for line in lines)
        # Active marker on the child (fork switches to it)
        assert any(line.startswith("* ") and "child" in line for line in lines)

    def test_tree_json(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("parent", ["repo-a"])
        fm.fork("child", "parent")

        result = runner.invoke(main, ["feature", "tree", "--json"])
        env = json.loads(result.output)
        by_name = {f["name"]: f for f in env["data"]["features"]}
        assert by_name["child"]["parent"] == "parent"
        assert env["data"]["active"] == "child"


class TestUpgrade:
    def test_upgrade_regenerates_and_preserves_foreign_content(
        self, initialized_workspace, runner, monkeypatch
    ):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])

        # Simulate a pre-upgrade workspace: a hand-written AGENT.md, no AGENTS.md,
        # no ambient brief files
        (ws.root / "AGENT.md").write_text("old customized content")
        (ws.root / "AGENTS.md").unlink()
        import shutil
        shutil.rmtree(ws.worktrees_dir / "feat", ignore_errors=True)

        result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0

        agent_md = (ws.root / "AGENT.md").read_text()
        assert "Working Memory" in agent_md          # mgit's block installed
        assert "old customized content" in agent_md  # ...without eating theirs
        assert (ws.root / "AGENTS.md").exists()
        assert (ws.worktrees_dir / "feat" / "CLAUDE.md").exists()

    def test_init_writes_both_agent_files(self, workspace_with_repos):
        from mgit.core.workspace import Workspace
        ws, _ = Workspace.init(workspace_with_repos, scan=True, auto_add=True)
        assert (ws.root / "AGENT.md").exists()
        assert (ws.root / "AGENTS.md").exists()


class TestCheckpointCli:
    def test_save_list_restore_via_cli(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        (wt / "wip.py").write_text("wip")

        result = runner.invoke(main, ["checkpoint", "save", "--label", "safe", "--json"])
        assert result.exit_code == 0
        env = json.loads(result.output)
        assert env["data"]["id"] == "cp-0001"
        assert env["data"]["repos"]["repo-a"]["dirty"] is True

        (wt / "wip.py").unlink()
        result = runner.invoke(main, ["checkpoint", "restore", "cp-0001"])
        assert result.exit_code == 0
        assert (wt / "wip.py").read_text() == "wip"

        result = runner.invoke(main, ["checkpoint", "list", "--json"])
        env = json.loads(result.output)
        ids = [c["id"] for c in env["data"]["checkpoints"]]
        assert ids == ["cp-0001", "cp-0002"]  # original + safety backup
