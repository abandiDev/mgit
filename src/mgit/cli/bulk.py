"""CLI commands: mgit {status,pull,push,commit,exec,setup}."""

from __future__ import annotations

import subprocess
import sys

import click

from mgit.core.feature import FeatureManager
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import BulkResult, FeatureInfo, OpStatus, RepoOpResult
from mgit.utils import output
from mgit.utils.display import print_bulk_results
from mgit.utils.errors import MgitError
from mgit.utils.parallel import run_bulk


def _resolve_feature_name(ws: Workspace, feature_name: str) -> str:
    """Resolve '.' to this session's feature ($MGIT_FEATURE > cwd worktree > active)."""
    if feature_name == ".":
        resolved = ws.resolve_feature(".")
        if not resolved:
            raise MgitError(
                "No feature in scope. Pass -f <name>, export MGIT_FEATURE, "
                "run inside a feature worktree, or use 'mgit feature start'."
            )
        return resolved
    return feature_name


def _resolve_repos(ws: Workspace, feature_name: str | None, repos_csv: str | None) -> list[str]:
    """Determine which repos to operate on."""
    if feature_name:
        resolved = _resolve_feature_name(ws, feature_name)
        fm = FeatureManager(ws)
        feat = fm.get(resolved)
        repo_names = list(feat.branches.keys())
        if not repo_names:
            raise MgitError(
                f"Feature '{resolved}' has no enrolled repos. "
                "Use 'mgit feature start' to enroll repos."
            )
        return repo_names
    elif repos_csv:
        names = [n.strip() for n in repos_csv.split(",")]
        for n in names:
            if n not in ws.repos:
                raise MgitError(f"Repo '{n}' not found in workspace")
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
    f = click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")(f)
    return f


def _finish(command: str, result: BulkResult, as_json: bool):
    """Emit results (envelope or human) and exit with the bulk code (0/1)."""
    if as_json:
        output.emit(command, result.to_dict())
    else:
        print_bulk_results(result)
    sys.exit(result.exit_code)


def _journal_bulk(ws: Workspace, feature_info: FeatureInfo | None,
                  result: BulkResult, action: str) -> None:
    """Auto-capture successful feature-scoped commits/pushes in the journal."""
    if not feature_info or not result.succeeded:
        return
    from mgit.core import git as gitmod, memory
    for r in result.succeeded:
        try:
            if action == "commit":
                wt = ws.worktree_path(feature_info.name, r.repo)
                res = gitmod.run_git("log", "-1", "--format=%h%x00%s",
                                     cwd=wt, check=False)
                meta = None
                text = f"Committed in '{r.repo}'"
                if res.returncode == 0 and "\x00" in res.stdout:
                    sha, subject = res.stdout.strip().split("\x00", 1)
                    meta = {"sha": sha, "subject": subject}
                    text = f"Committed in '{r.repo}': {subject}"
                memory.append_event(ws, feature_info.name, text,
                                    event="commit", repo=r.repo, meta=meta)
            elif action == "push":
                target = feature_info.branches.get(r.repo)
                memory.append_event(
                    ws, feature_info.name, f"Pushed '{r.repo}' -> {target}",
                    event="push", repo=r.repo,
                    meta={"target_branch": target},
                )
        except Exception:
            continue
    try:
        from mgit.core.brief import write_feature_brief
        write_feature_brief(ws, feature_info)
    except Exception:
        pass


def _get_feature_info(ws: Workspace, feature: str | None) -> FeatureInfo | None:
    """Load feature info if a feature is specified."""
    if not feature:
        return None
    resolved = _resolve_feature_name(ws, feature)
    fm = FeatureManager(ws)
    return fm.get(resolved)


@click.command("status")
@_common_options
def status(feature, repos, fail_fast, as_json):
    """Show git status across repos."""
    if as_json:
        output.set_json_mode()
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
    _finish("status", result, as_json)


@click.command("pull")
@_common_options
def pull(feature, repos, fail_fast, as_json):
    """Pull across repos."""
    if as_json:
        output.set_json_mode()
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
    _finish("pull", result, as_json)


@click.command("push")
@_common_options
def push(feature, repos, fail_fast, as_json):
    """Push across repos. When -f is used, pushes sandbox branches to target remote branches."""
    if as_json:
        output.set_json_mode()
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
    _journal_bulk(ws, feature_info, result, "push")
    _finish("push", result, as_json)


@click.command("commit")
@click.option("-m", "--message", required=True, help="Commit message.")
@_common_options
def commit(message, feature, repos, fail_fast, as_json):
    """Commit across repos (stages all changes)."""
    if as_json:
        output.set_json_mode()
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
    _journal_bulk(ws, feature_info, result, "commit")
    _finish("commit", result, as_json)


@click.command("exec", context_settings={
    "ignore_unknown_options": True,
    "allow_interspersed_args": False,
})
@_common_options
@click.argument("command", nargs=-1, required=True)
def exec_cmd(command, feature, repos, fail_fast, as_json):
    """Run an arbitrary command in each repo.

    mgit options (-f/-r/--fail-fast/--json) must come BEFORE the command;
    everything from the first non-option token on is passed through verbatim
    (so 'mgit exec -f . git clean -fd' runs exactly 'git clean -fd').
    """
    if as_json:
        output.set_json_mode()
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
    _finish("exec", result, as_json)


@click.command("setup")
@_common_options
def setup_cmd(feature, repos, fail_fast, as_json):
    """Run configured setup commands across repos.

    Skips repos with no setup command or unmaterialized worktrees.
    """
    if as_json:
        output.set_json_mode()
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
    _finish("setup", result, as_json)
