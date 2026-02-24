"""CLI commands: mgit {status,pull,push,commit,exec,setup}."""

from __future__ import annotations

import subprocess
import sys

import click

from mgit.core.feature import FeatureManager
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import FeatureInfo, OpStatus, RepoOpResult
from mgit.utils.display import print_bulk_results
from mgit.utils.errors import MgitError
from mgit.utils.parallel import run_bulk


def _resolve_feature_name(ws: Workspace, feature_name: str) -> str:
    """Resolve '.' to the active feature name."""
    if feature_name == ".":
        active = ws.get_active_feature()
        if not active:
            raise click.ClickException(
                "No active feature. Use 'mgit feature start' or 'mgit feature activate' first."
            )
        return active
    return feature_name


def _resolve_repos(ws: Workspace, feature_name: str | None, repos_csv: str | None) -> list[str]:
    """Determine which repos to operate on."""
    if feature_name:
        resolved = _resolve_feature_name(ws, feature_name)
        fm = FeatureManager(ws)
        try:
            feat = fm.get(resolved)
        except MgitError as e:
            raise click.ClickException(str(e))
        repo_names = list(feat.branches.keys())
        if not repo_names:
            raise click.ClickException(
                f"Feature '{resolved}' has no enrolled repos. "
                "Use 'mgit feature start' to enroll repos."
            )
        return repo_names
    elif repos_csv:
        names = [n.strip() for n in repos_csv.split(",")]
        for n in names:
            if n not in ws.repos:
                raise click.ClickException(f"Repo '{n}' not found in workspace")
        return names
    else:
        return list(ws.repos.keys())


def _make_repo(ws: Workspace, repo_name: str, feature_info: FeatureInfo | None = None) -> Repo | None:
    """Create a Repo at the right path (worktree or original).

    When a feature is specified and the repo is enrolled, returns a Repo
    pointing to the worktree directory — or None if the worktree hasn't
    been materialized yet. Otherwise returns a Repo pointing to the
    original repo path.
    """
    if feature_info and repo_name in feature_info.branches:
        wt_path = ws.worktree_path(feature_info.name, repo_name)
        if not wt_path.exists():
            return None
        return Repo.at_worktree(wt_path, ws.get_repo(repo_name))
    return Repo(ws.get_repo(repo_name), ws.root)


def _common_options(f):
    """Shared options for bulk commands."""
    f = click.option("--feature", "-f", default=None, help="Scope to a feature's repos (use '.' for active).")(f)
    f = click.option("--repos", "-r", default=None, help="Comma-separated repo names.")(f)
    f = click.option("--fail-fast", is_flag=True, help="Stop on first failure.")(f)
    return f


def _get_feature_info(ws: Workspace, feature: str | None) -> FeatureInfo | None:
    """Load feature info if a feature is specified."""
    if not feature:
        return None
    resolved = _resolve_feature_name(ws, feature)
    fm = FeatureManager(ws)
    return fm.get(resolved)


@click.command("status")
@_common_options
def status(feature, repos, fail_fast):
    """Show git status across repos."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)
    feature_info = _get_feature_info(ws, feature)

    def op(name: str) -> RepoOpResult:
        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")
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
    feature_info = _get_feature_info(ws, feature)

    def op(name: str) -> RepoOpResult:
        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")
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
    """Push across repos. When -f is used, pushes sandbox branches to target remote branches."""
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)
    feature_info = _get_feature_info(ws, feature)

    def op(name: str) -> RepoOpResult:
        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")
        try:
            if feature_info and name in feature_info.branches:
                target = feature_info.branches[name]
                output = repo.push_to_target(target)
            else:
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
    feature_info = _get_feature_info(ws, feature)

    def op(name: str) -> RepoOpResult:
        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")
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
    feature_info = _get_feature_info(ws, feature)
    cmd_list = list(command)

    def op(name: str) -> RepoOpResult:
        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")
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


@click.command("setup")
@_common_options
def setup_cmd(feature, repos, fail_fast):
    """Run configured setup commands across repos.

    Skips repos with no setup command or unmaterialized worktrees.
    """
    ws = Workspace.find()
    repo_names = _resolve_repos(ws, feature, repos)
    feature_info = _get_feature_info(ws, feature)

    def op(name: str) -> RepoOpResult:
        repo_info = ws.get_repo(name)
        if not repo_info.setup:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="no setup command configured")

        repo = _make_repo(ws, name, feature_info)
        if repo is None:
            return RepoOpResult(repo=name, status=OpStatus.SKIPPED,
                                message="not materialized")

        try:
            proc = subprocess.run(
                repo_info.setup,
                shell=True,
                cwd=repo.path,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = proc.stdout + proc.stderr
            if proc.returncode == 0:
                return RepoOpResult(
                    repo=name, status=OpStatus.SUCCESS,
                    message="setup complete", output=output.strip(),
                )
            else:
                return RepoOpResult(
                    repo=name, status=OpStatus.FAILED,
                    message=f"exit code {proc.returncode}",
                    output=output.strip(),
                )
        except subprocess.TimeoutExpired:
            return RepoOpResult(
                repo=name, status=OpStatus.FAILED,
                message="setup timed out (>300s)",
            )

    result = run_bulk(repo_names, op, fail_fast=fail_fast)
    print_bulk_results(result)
    sys.exit(result.exit_code)
