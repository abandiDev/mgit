"""Tests for bulk operations and parallel execution."""

import pytest

from mgit.core.repo import Repo
from mgit.models.types import BulkResult, OpStatus, RepoOpResult
from mgit.utils.parallel import run_bulk


class TestBulkResult:
    def test_empty_result(self):
        r = BulkResult()
        assert r.all_ok is True
        assert r.exit_code == 0
        assert r.succeeded == []
        assert r.failed == []
        assert r.skipped == []

    def test_all_success(self):
        r = BulkResult(results=[
            RepoOpResult(repo="a", status=OpStatus.SUCCESS),
            RepoOpResult(repo="b", status=OpStatus.SUCCESS),
        ])
        assert r.all_ok is True
        assert r.exit_code == 0
        assert len(r.succeeded) == 2

    def test_with_failure(self):
        r = BulkResult(results=[
            RepoOpResult(repo="a", status=OpStatus.SUCCESS),
            RepoOpResult(repo="b", status=OpStatus.FAILED, message="error"),
        ])
        assert r.all_ok is False
        assert r.exit_code == 1
        assert len(r.failed) == 1

    def test_mixed_results(self):
        r = BulkResult(results=[
            RepoOpResult(repo="a", status=OpStatus.SUCCESS),
            RepoOpResult(repo="b", status=OpStatus.SKIPPED),
            RepoOpResult(repo="c", status=OpStatus.FAILED),
        ])
        assert len(r.succeeded) == 1
        assert len(r.skipped) == 1
        assert len(r.failed) == 1


class TestRunBulk:
    def test_run_all_succeed(self):
        def op(name):
            return RepoOpResult(repo=name, status=OpStatus.SUCCESS)

        result = run_bulk(["a", "b", "c"], op)
        assert len(result.succeeded) == 3
        assert result.all_ok is True

    def test_run_with_failure(self):
        def op(name):
            if name == "b":
                return RepoOpResult(repo=name, status=OpStatus.FAILED, message="oops")
            return RepoOpResult(repo=name, status=OpStatus.SUCCESS)

        result = run_bulk(["a", "b", "c"], op)
        assert len(result.succeeded) == 2
        assert len(result.failed) == 1

    def test_run_handles_exception(self):
        def op(name):
            if name == "b":
                raise RuntimeError("boom")
            return RepoOpResult(repo=name, status=OpStatus.SUCCESS)

        result = run_bulk(["a", "b", "c"], op)
        assert len(result.failed) == 1
        assert "boom" in result.failed[0].message

    def test_run_empty_list(self):
        result = run_bulk([], lambda n: None)
        assert len(result.results) == 0

    def test_results_sorted_by_name(self):
        def op(name):
            return RepoOpResult(repo=name, status=OpStatus.SUCCESS)

        result = run_bulk(["c", "a", "b"], op)
        names = [r.repo for r in result.results]
        assert names == ["a", "b", "c"]


class TestBulkOpsIntegration:
    def test_status_all_repos(self, initialized_workspace):
        ws = initialized_workspace

        def status_op(name):
            repo = Repo(ws.get_repo(name), ws.root)
            output = repo.status()
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS, output=output,
            )

        result = run_bulk(list(ws.repos.keys()), status_op)
        assert result.all_ok is True
        assert len(result.results) == 2

    def test_commit_across_repos(self, initialized_workspace):
        ws = initialized_workspace

        # Make changes in both repos
        (ws.root / "repo-a" / "file.txt").write_text("change a")
        (ws.root / "repo-b" / "file.txt").write_text("change b")

        def commit_op(name):
            repo = Repo(ws.get_repo(name), ws.root)
            output = repo.commit("bulk commit")
            return RepoOpResult(repo=name, status=OpStatus.SUCCESS, output=output)

        result = run_bulk(list(ws.repos.keys()), commit_op)
        assert result.all_ok is True

        # Verify repos are clean after commit
        for name in ws.repos:
            repo = Repo(ws.get_repo(name), ws.root)
            assert repo.is_dirty() is False

    def test_exec_across_repos(self, initialized_workspace):
        ws = initialized_workspace

        def exec_op(name):
            repo = Repo(ws.get_repo(name), ws.root)
            code, stdout, _ = repo.exec(["git", "log", "--oneline", "-1"])
            if code == 0:
                return RepoOpResult(repo=name, status=OpStatus.SUCCESS, output=stdout)
            return RepoOpResult(repo=name, status=OpStatus.FAILED)

        result = run_bulk(list(ws.repos.keys()), exec_op)
        assert result.all_ok is True
        for r in result.results:
            assert "init" in r.output
