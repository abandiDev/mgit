"""Tests for mgit context command."""

import json

import pytest
from click.testing import CliRunner

from mgit.cli import main
from mgit.core.feature import FeatureManager


@pytest.fixture
def runner():
    return CliRunner()


class TestContextCommand:
    def test_context_basic(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)

        result = runner.invoke(main, ["context"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        assert data["workspace"]["name"] == ws.name
        assert data["workspace"]["root"] == str(ws.root)
        assert "repo-a" in data["repos"]
        assert "repo-b" in data["repos"]
        assert data["active_feature"] is None
        assert data["features"] == []

    def test_context_pretty(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)

        result = runner.invoke(main, ["context", "--pretty"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        # Pretty output should have indentation
        assert "\n" in result.output
        assert "  " in result.output
        assert data["workspace"]["name"] == ws.name

    def test_context_with_active_feature(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)

        fm = FeatureManager(ws)
        fm.start("auth-refactor", ["repo-a", "repo-b"])

        result = runner.invoke(main, ["context"])
        assert result.exit_code == 0

        data = json.loads(result.output)
        af = data["active_feature"]
        assert af is not None
        assert af["name"] == "auth-refactor"
        assert af["sandbox_branch"] == "mgit/auth-refactor"
        assert "repo-a" in af["repos"]
        assert "repo-b" in af["repos"]

    def test_context_repo_info(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)

        result = runner.invoke(main, ["context"])
        data = json.loads(result.output)

        repo_a = data["repos"]["repo-a"]
        assert repo_a["path"] == "repo-a"
        assert repo_a["default_branch"] in ("main", "master")
        assert "current_branch" in repo_a
        assert "dirty" in repo_a

    def test_context_current_repo_detection(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root / "repo-a")

        result = runner.invoke(main, ["context"])
        data = json.loads(result.output)
        assert data["current_repo"] == "repo-a"

    def test_context_features_list(self, initialized_workspace, runner, monkeypatch):
        ws = initialized_workspace
        monkeypatch.chdir(ws.root)

        fm = FeatureManager(ws)
        fm.start("feat-1", ["repo-a"])
        fm.start("feat-2", ["repo-b"])

        result = runner.invoke(main, ["context"])
        data = json.loads(result.output)

        names = {f["name"] for f in data["features"]}
        assert names == {"feat-1", "feat-2"}
