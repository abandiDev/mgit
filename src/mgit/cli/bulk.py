"""CLI commands: mgit {status,pull,push,commit,exec}."""

from __future__ import annotations

import sys

import click

from mgit.core.feature import FeatureManager
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import OpStatus, RepoOpResult
from mgit.utils.display import print_bulk_results
from mgit.utils.errors import MgitError
from mgit.utils.parallel import run_bulk


def _resolve_repos(ws: Workspace, feature_name: str | None, repos_csv: str | None) -> list[str]:
    """Determine which repos to operate on."""
    if feature_name:
        fm = FeatureManager(ws)
        try:
            feat = fm.get(feature_name)
        except MgitError as e:
            raise click.ClickException(str(e))
        return list(feat.branches.keys())
    elif repos_csv:
        names = [n.strip() for n in repos_csv.split(",")]
        for n in names:
            if n not in ws.repos:
                raise click.ClickException(f"Repo '{n}' not found in workspace")
        return names
    else:
        return list(ws.repos.keys())


def _common_options(f):
    """Shared options for bulk commands."""
    f = click.option("--feature", "-f", default=None, help="Scope to a feature's repos.")(f)
    f = click.option("--repos", "-r", default=None, help="Comma-separated repo names.")(f)
    f = click.option("--fail-fast", is_flag=True, help="Stop on first failure.")(f)
    return f


@click.command("status")
@_common_options
def status(feature, repos, fail_fast):
    """Show git status across repos."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)

    def op(name: str) -> RepoOpResult:
        repo = Repo(ws.get_repo(name), ws.root)
        branch = repo.current_branch()
        output = repo.status()
        if output.strip():
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                message=f"on {branch}", output=output,
            )
        else:
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                message=f"on {branch} (clean)",
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)


@click.command("pull")
@_common_options
def pull(feature, repos, fail_fast):
    """Pull across repos."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)

    def op(name: str) -> RepoOpResult:
        repo = Repo(ws.get_repo(name), ws.root)
        try:
            output = repo.pull()
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                message="pulled", output=output,
            )
        except MgitError as e:
            return RepoOpResult(
                repo=name, status=OpStatus.FAILED,
                message=str(e),
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)


@click.command("push")
@_common_options
def push(feature, repos, fail_fast):
    """Push across repos."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)

    def op(name: str) -> RepoOpResult:
        repo = Repo(ws.get_repo(name), ws.root)
        try:
            output = repo.push()
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                message="pushed", output=output,
            )
        except MgitError as e:
            return RepoOpResult(
                repo=name, status=OpStatus.FAILED,
                message=str(e),
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)


@click.command("commit")
@click.option("-m", "--message", required=True, help="Commit message.")
@_common_options
def commit(message, feature, repos, fail_fast):
    """Commit across repos (stages all changes)."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)

    def op(name: str) -> RepoOpResult:
        repo = Repo(ws.get_repo(name), ws.root)
        try:
            output = repo.commit(message)
            if "nothing to commit" in output:
                return RepoOpResult(
                    repo=name, status=OpStatus.SKIPPED,
                    message="nothing to commit",
                )
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                message="committed", output=output,
            )
        except MgitError as e:
            return RepoOpResult(
                repo=name, status=OpStatus.FAILED,
                message=str(e),
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)


@click.command("exec")
@_common_options
@click.argument("command", nargs=-1, required=True)
def exec_cmd(command, feature, repos, fail_fast):
    """Run an arbitrary command in each repo."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)
    cmd_list = list(command)

    def op(name: str) -> RepoOpResult:
        repo = Repo(ws.get_repo(name), ws.root)
        returncode, stdout, stderr = repo.exec(cmd_list)
        if returncode == 0:
            return RepoOpResult(
                repo=name, status=OpStatus.SUCCESS,
                output=stdout,
            )
        else:
            return RepoOpResult(
                repo=name, status=OpStatus.FAILED,
                message=f"exit code {returncode}",
                output=stderr or stdout,
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)
