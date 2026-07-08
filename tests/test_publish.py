"""Tests for the delivery layer: forge detection, publish, checks.

Network surfaces are faked hermetically: gh/glab are PATH-shim scripts that
log their invocations and return canned JSON; pushes go to local bare repos.
"""

import json
import os
import subprocess

import pytest
from click.testing import CliRunner

from mgit.cli import main
from mgit.core import git, memory
from mgit.core.feature import FeatureManager
from mgit.core.forge import detect_forge
from mgit.utils import output


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def reset_json_mode():
    output.set_json_mode(False)
    yield
    output.set_json_mode(False)


class TestDetectForge:
    @pytest.mark.parametrize("url,kind", [
        ("https://github.com/org/repo.git", "github"),
        ("git@github.com:org/repo.git", "github"),
        ("https://github.mycorp.com/org/repo.git", "github"),
        ("https://gitlab.com/org/repo.git", "gitlab"),
        ("git@gitlab.mycorp.io:org/repo.git", "gitlab"),
        ("https://bitbucket.org/org/repo.git", None),
        (None, None),
        ("/local/path/repo", None),
    ])
    def test_detection(self, url, kind):
        assert detect_forge(url) == kind


GH_FAKE = """#!/bin/bash
echo "gh $*" >> "$FAKE_LOG"
case "$1 $2" in
  "pr list") cat "$FAKE_DIR/gh_pr_list.json" 2>/dev/null || echo "[]";;
  "pr create") echo "https://github.com/org/repo-a/pull/12";;
  "pr edit") :;;
  "pr view") cat "$FAKE_DIR/gh_pr_view.json" 2>/dev/null || { echo "no pull requests found" >&2; exit 1; };;
esac
"""

GLAB_FAKE = """#!/bin/bash
echo "glab $*" >> "$FAKE_LOG"
case "$1 $2" in
  "mr list") cat "$FAKE_DIR/glab_mr_list.json" 2>/dev/null || echo "[]";;
  "mr create") echo "https://gitlab.com/org/repo-b/-/merge_requests/7";;
  "mr update") :;;
  "mr view") cat "$FAKE_DIR/glab_mr_view.json" 2>/dev/null || exit 1;;
esac
"""


@pytest.fixture
def publish_workspace(initialized_workspace, tmp_path, monkeypatch):
    """Workspace where repo-a looks like GitHub, repo-b like GitLab, both
    pushing to local bare origins, with fake gh/glab on PATH."""
    ws = initialized_workspace

    # Local bare origins so push works offline
    for name in ("repo-a", "repo-b"):
        bare = tmp_path / f"{name}-origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "-C", str(ws.root / name),
                        "remote", "add", "origin", str(bare)], check=True)

    # Recorded URLs drive forge detection (independent of the push remote)
    ws.repos["repo-a"].url = "https://github.com/org/repo-a.git"
    ws.repos["repo-b"].url = "https://gitlab.com/org/repo-b.git"
    ws.save()

    # PATH-shim fakes
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_dir = tmp_path / "fakes"
    fake_dir.mkdir()
    fake_log = tmp_path / "forge.log"
    fake_log.touch()
    for fname, script in (("gh", GH_FAKE), ("glab", GLAB_FAKE)):
        path = fake_bin / fname
        path.write_text(script)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(fake_log))
    monkeypatch.setenv("FAKE_DIR", str(fake_dir))
    monkeypatch.chdir(ws.root)

    return ws, fake_log, fake_dir


def _commit_in(path, fname, message):
    (path / fname).write_text("content")
    git.run_git("add", "-A", cwd=path)
    git.run_git("commit", "-m", message, cwd=path)


class TestPublish:
    def test_publish_pushes_and_creates_pr(self, publish_workspace, runner, tmp_path):
        ws, fake_log, _ = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        _commit_in(wt, "change.py", "the change")

        result = runner.invoke(main, ["feature", "publish"])
        assert result.exit_code == 0, result.output
        assert "PR created" in result.output

        # Branch actually landed in the bare origin
        bare = tmp_path / "repo-a-origin.git"
        assert git.run_git("rev-parse", "--verify", "refs/heads/feat",
                           cwd=bare, check=False).returncode == 0

        log = fake_log.read_text()
        assert "gh pr list" in log
        assert "gh pr create" in log

        # URL persisted and journaled
        feat = fm.get("feat")
        assert feat.prs["repo-a"] == "https://github.com/org/repo-a/pull/12"
        events = [e.get("event") for e in memory.read_journal(ws, "feat")[0]]
        assert "published" in events

    def test_publish_skips_when_up_to_date(self, publish_workspace, runner):
        ws, fake_log, _ = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        _commit_in(wt, "change.py", "the change")

        assert runner.invoke(main, ["feature", "publish"]).exit_code == 0
        fake_log.write_text("")  # reset log

        result = runner.invoke(main, ["feature", "publish"])
        assert result.exit_code == 0
        assert "up to date" in result.output
        assert "gh" not in fake_log.read_text()

    def test_publish_idempotent_updates_existing_pr(
        self, publish_workspace, runner,
    ):
        ws, fake_log, fake_dir = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        _commit_in(wt, "one.py", "first")
        assert runner.invoke(main, ["feature", "publish"]).exit_code == 0

        # Now the PR "exists" on the forge; add another commit and republish
        (fake_dir / "gh_pr_list.json").write_text(json.dumps([
            {"number": 12, "url": "https://github.com/org/repo-a/pull/12",
             "state": "open"},
        ]))
        _commit_in(wt, "two.py", "second")
        fake_log.write_text("")

        result = runner.invoke(main, ["feature", "publish"])
        assert result.exit_code == 0
        assert "PR updated" in result.output
        log = fake_log.read_text()
        assert "gh pr create" not in log
        assert "gh pr edit 12" in log

    def test_publish_cross_links_and_mixed_forges(self, publish_workspace, runner):
        ws, fake_log, _ = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        for repo in ("repo-a", "repo-b"):
            wt = fm.materialize("feat", repo)
            _commit_in(wt, "change.py", "the change")

        result = runner.invoke(main, ["feature", "publish"])
        assert result.exit_code == 0, result.output

        feat = fm.get("feat")
        assert feat.prs["repo-a"].startswith("https://github.com")
        assert feat.prs["repo-b"].startswith("https://gitlab.com")

        log = fake_log.read_text()
        # GitHub repo went through gh, GitLab repo through glab
        assert "gh pr create" in log
        assert "glab mr create" in log
        # Cross-link pass: repo-a's PR body references repo-b's MR
        assert "gh pr edit 12" in log
        assert "merge_requests/7" in log

    def test_publish_unknown_forge_still_pushes(self, publish_workspace, runner, tmp_path):
        ws, fake_log, _ = publish_workspace
        ws.repos["repo-a"].url = "https://bitbucket.org/org/repo-a.git"
        ws.save()
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        _commit_in(wt, "change.py", "the change")

        result = runner.invoke(main, ["feature", "publish"])
        assert result.exit_code == 0
        assert "can't detect forge" in result.output
        # Push still happened
        bare = tmp_path / "repo-a-origin.git"
        assert git.run_git("rev-parse", "--verify", "refs/heads/feat",
                           cwd=bare, check=False).returncode == 0
        assert "gh" not in fake_log.read_text()

    def test_publish_json_envelope(self, publish_workspace, runner):
        ws, _, _ = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])
        wt = fm.materialize("feat", "repo-a")
        _commit_in(wt, "change.py", "the change")

        result = runner.invoke(main, ["feature", "publish", "--json"])
        env = json.loads(result.output)
        assert env["ok"] is True
        assert env["command"] == "feature.publish"
        assert env["data"]["prs"]["repo-a"].endswith("/pull/12")
        assert env["data"]["summary"]["succeeded"] == 1


class TestChecks:
    def test_checks_aggregates_ci_state(self, publish_workspace, runner):
        ws, _, fake_dir = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a", "repo-b"])
        fm.materialize("feat", "repo-a")

        (fake_dir / "gh_pr_view.json").write_text(json.dumps({
            "number": 12,
            "url": "https://github.com/org/repo-a/pull/12",
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "reviewDecision": "APPROVED",
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"conclusion": "SUCCESS"},
                {"conclusion": "FAILURE"},
                {"status": "IN_PROGRESS"},
            ],
        }))
        (fake_dir / "glab_mr_view.json").write_text(json.dumps({
            "iid": 7,
            "web_url": "https://gitlab.com/org/repo-b/-/merge_requests/7",
            "state": "opened",
            "pipeline": {"status": "success"},
        }))

        result = runner.invoke(main, ["feature", "checks", "--json"])
        assert result.exit_code == 0, result.output
        env = json.loads(result.output)
        rows = {r["repo"]: r for r in env["data"]["prs"]}
        a = rows["repo-a"]
        assert a["checks"] == {"passed": 2, "failed": 1, "pending": 1}
        assert a["review"] == "APPROVED"
        b = rows["repo-b"]
        assert b["checks"]["passed"] == 1

    def test_checks_no_pr(self, publish_workspace, runner):
        ws, _, _ = publish_workspace
        fm = FeatureManager(ws)
        fm.start("feat", ["repo-a"])

        result = runner.invoke(main, ["feature", "checks", "--json"])
        env = json.loads(result.output)
        rows = {r["repo"]: r for r in env["data"]["prs"]}
        assert rows["repo-a"]["state"] == "no-pr"
