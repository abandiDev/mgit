"""CLI commands: mgit feature {start,work,switch,activate,deactivate,show,list,delete,remove-repo}."""

import click

from mgit.core.feature import (
    FeatureManager,
    find_dirty_repos,
    sandbox_branch,
)
from mgit.core.workspace import Workspace
from mgit.utils.errors import MgitError


@click.group()
def feature():
    """Manage cross-repo features."""


@feature.command("start")
@click.argument("feature_name")
@click.option(
    "--repo", "-r", "repo_names", multiple=True,
    help="Repo name (can be repeated). Omit to enroll all repos.",
)
@click.option("--branch", default=None, help="Remote target branch (default: feature name).")
@click.option("--description", "-d", default="", help="Feature description (only on creation).")
@click.option("--materialize", "-m", is_flag=True, default=False,
              help="Materialize worktrees immediately (auto-carries dirty changes).")
def start(feature_name, repo_names, branch, description, materialize):
    """Start a feature: create/enroll repos.

    If no -r is given, enrolls all workspace repos.
    Pass --materialize to create worktrees immediately.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    repo_list = list(repo_names) if repo_names else None

    try:
        feat, newly_added = fm.start(
            feature_name, repo_list,
            target_branch=branch,
            description=description,
            materialize=materialize,
        )
    except MgitError as e:
        raise click.ClickException(str(e))

    total = len(feat.branches)
    new_count = len(newly_added)

    click.echo(f"Feature '{feature_name}': {new_count} newly enrolled, {total} total.")
    for repo_name in newly_added:
        if materialize:
            wt_path = ws.worktree_path(feature_name, repo_name)
            click.echo(f"  + {repo_name}: {wt_path}/")
        else:
            click.echo(f"  + {repo_name}")
    click.echo(f"Active feature: {feature_name}")
    if not materialize:
        click.echo()
        click.echo("Use 'mgit feature work <repo>' to materialize a worktree.")


@feature.command("work")
@click.argument("repo_name", required=False)
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to the feature worktree.")
def work(repo_name, carry):
    """Materialize a worktree for a repo in the active feature.

    If REPO_NAME is omitted, auto-detects from current directory.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active = ws.get_active_feature()
    if not active:
        raise click.ClickException(
            "No active feature. Use 'mgit feature start' first."
        )

    # Auto-detect repo from cwd if not specified
    if not repo_name:
        detected = ws.detect_repo_from_cwd()
        if detected:
            repo_name = detected
        else:
            raise click.ClickException(
                "No repo specified and cwd is not inside a registered repo."
            )

    # Detect dirty repo and resolve carry behavior
    do_carry = False
    if carry is not False:
        dirty = find_dirty_repos(ws, [repo_name])
        if dirty:
            if carry is None:
                # No flag — prompt
                _, br = dirty[0]
                click.echo(f"  {repo_name} has uncommitted changes (on {br})")
                do_carry = click.confirm(
                    "Carry changes to the feature worktree?", default=False
                )
            else:
                do_carry = True  # --carry flag

    try:
        wt_path = fm.work(active, repo_name, carry=do_carry)
    except MgitError as e:
        raise click.ClickException(str(e))

    suffix = " (changes carried)" if do_carry else ""
    click.echo(f"  {repo_name}: materialized -> {wt_path}/{suffix}")


@feature.command("sync")
def sync():
    """Discover dirty repos and enroll them into the active feature.

    Scans all workspace repos for uncommitted changes, enrolls any that
    aren't already part of the active feature, and materializes worktrees
    carrying changes over.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active = ws.get_active_feature()
    if not active:
        raise click.ClickException(
            "No active feature. Use 'mgit feature start' first."
        )

    try:
        synced = fm.sync(active)
    except MgitError as e:
        raise click.ClickException(str(e))

    if synced:
        click.echo(f"Synced {len(synced)} repo(s) into feature '{active}':")
        for name in synced:
            click.echo(f"  + {name} (changes carried)")
    else:
        click.echo("No new dirty repos to sync.")


@feature.command("switch")
@click.argument("name")
def switch(name):
    """Set the active feature. Worktrees are always ready — no branch switching needed."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    try:
        wt_paths = fm.switch(name)
    except MgitError as e:
        raise click.ClickException(str(e))

    for repo_name, wt_path in wt_paths.items():
        click.echo(f"  {repo_name}: {wt_path}")
    click.echo(f"Active feature: {name}")


@feature.command("activate")
@click.argument("name")
def activate(name):
    """Set the active feature without switching branches."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    # Validate feature exists
    try:
        fm.get(name)
    except MgitError as e:
        raise click.ClickException(str(e))

    ws.set_active_feature(name)
    click.echo(f"Active feature: {name}")


@feature.command("deactivate")
def deactivate():
    """Clear the active feature."""
    ws = Workspace.find()
    ws.clear_active_feature()
    click.echo("No active feature.")


@feature.command("show")
@click.argument("name")
def show(name):
    """Show details of a feature."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        f = fm.get(name)
    except MgitError as e:
        raise click.ClickException(str(e))

    active = ws.get_active_feature()
    active_marker = " (active)" if active == name else ""

    click.echo(f"Feature: {f.name}{active_marker}")
    if f.description:
        click.echo(f"Description: {f.description}")
    click.echo(f"Sandbox branch: {sandbox_branch(name)}")
    click.echo("Repos:")
    if f.branches:
        for repo, target in f.branches.items():
            wt_path = ws.worktree_path(name, repo)
            mat = "*" if wt_path.exists() else " "
            click.echo(f"  {mat} {repo:<20} -> {target}  ({wt_path})")
    else:
        click.echo("  (none)")


@feature.command("list")
def list_features():
    """List all features."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    features = fm.list()
    active = ws.get_active_feature()

    if not features:
        click.echo("No features defined. Use 'mgit feature start' to create one.")
        return

    for f in features:
        marker = "* " if f.name == active else "  "
        repos = ", ".join(f.branches.keys()) if f.branches else "(no repos)"
        desc = f" - {f.description}" if f.description else ""
        click.echo(f"{marker}{f.name}{desc}  [{repos}]")


@feature.command("delete")
@click.argument("name")
def delete(name):
    """Delete a feature definition and clean up its worktrees."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        fm.delete(name)
    except MgitError as e:
        raise click.ClickException(str(e))
    click.echo(f"Deleted feature '{name}'.")


@feature.command("remove-repo")
@click.argument("feature_name")
@click.argument("repo_name")
def remove_repo(feature_name, repo_name):
    """Remove a repo from a feature and clean up its worktree."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        fm.remove_repo(feature_name, repo_name)
    except MgitError as e:
        raise click.ClickException(str(e))
    click.echo(f"Removed '{repo_name}' from feature '{feature_name}'.")
