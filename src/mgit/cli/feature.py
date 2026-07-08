"""CLI commands: mgit feature {start,materialize,switch,activate,deactivate,show,list,delete,remove-repo}."""

import click

from mgit.core.feature import (
    FeatureManager,
    find_dirty_repos,
    sandbox_branch,
)
from mgit.core.workspace import Workspace
from mgit.utils.errors import MgitError


def _print_setup_status(ws, fm, repo_name, wt_path):
    """Print setup command result for a repo if configured."""
    repo_info = ws.get_repo(repo_name)
    if not repo_info.setup:
        return
    click.echo(f"    Running setup: {repo_info.setup}")
    ok, output = fm.run_setup(repo_name, wt_path)
    if ok:
        click.echo(f"    Setup complete.")
    else:
        click.secho(f"    Setup failed: {output}", fg="yellow", err=True)


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
              help="Materialize worktrees immediately.")
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to worktrees (default: prompt per repo).")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def start(feature_name, repo_names, branch, description, materialize, carry, no_setup):
    """Start a feature: create/enroll repos.

    If no -r is given, enrolls all workspace repos.
    Pass --materialize to create worktrees immediately.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    repo_list = list(repo_names) if repo_names else None

    # Resolve carry_repos when materializing
    carry_repos = None
    if materialize:
        names_to_check = repo_list if repo_list is not None else list(ws.repos.keys())
        if carry is True:
            # --carry: carry all dirty repos without prompting
            carry_repos = {
                name for name, _ in find_dirty_repos(ws, names_to_check)
            }
        elif carry is False:
            # --no-carry: skip carry for all
            carry_repos = set()
        else:
            # Neither flag: prompt per dirty repo
            dirty = find_dirty_repos(ws, names_to_check)
            carry_repos = set()
            for name, br in dirty:
                if click.confirm(
                    f"  {name} has uncommitted changes (on {br}). "
                    f"Carry to feature worktree?",
                    default=False,
                ):
                    carry_repos.add(name)

    try:
        feat, newly_added = fm.start(
            feature_name, repo_list,
            target_branch=branch,
            description=description,
            materialize=materialize,
            run_setup=not no_setup,
            carry_repos=carry_repos,
        )
    except MgitError as e:
        raise click.ClickException(str(e))

    total = len(feat.branches)
    new_count = len(newly_added)

    click.echo(f"Feature '{feature_name}': {new_count} newly enrolled, {total} total.")
    for repo_name in newly_added:
        if materialize:
            wt_path = ws.worktree_path(feature_name, repo_name)
            carried = carry_repos is not None and repo_name in carry_repos
            suffix = " (changes carried)" if carried else ""
            click.echo(f"  + {repo_name}: {wt_path}/{suffix}")
            if not no_setup:
                _print_setup_status(ws, fm, repo_name, wt_path)
        else:
            click.echo(f"  + {repo_name}")
    click.echo(f"Active feature: {feature_name}")
    if not materialize:
        click.echo()
        click.echo("Use 'mgit feature materialize <repo>' to materialize a worktree.")


def _materialize_handler(repo_name, carry, no_setup):
    """Shared handler for materialize and work commands."""
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

    # Check if already materialized (setup only runs for newly created worktrees)
    already_materialized = fm.is_materialized(active, repo_name)

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
        wt_path = fm.materialize(active, repo_name, carry=do_carry)
    except MgitError as e:
        raise click.ClickException(str(e))

    suffix = " (changes carried)" if do_carry else ""
    click.echo(f"  {repo_name}: materialized -> {wt_path}/{suffix}")

    # Run setup only for newly created worktrees
    if not already_materialized and not no_setup:
        _print_setup_status(ws, fm, repo_name, wt_path)


@feature.command("materialize")
@click.argument("repo_name", required=False)
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to the feature worktree.")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def materialize_cmd(repo_name, carry, no_setup):
    """Materialize a worktree for a repo in the active feature.

    If REPO_NAME is omitted, auto-detects from current directory.
    """
    _materialize_handler(repo_name, carry, no_setup)


@feature.command("work", hidden=True)
@click.argument("repo_name", required=False)
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to the feature worktree.")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def work(repo_name, carry, no_setup):
    """Materialize a worktree for a repo in the active feature (alias for materialize)."""
    _materialize_handler(repo_name, carry, no_setup)


@feature.command("sync")
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to worktrees (default: prompt per repo).")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def sync(carry, no_setup):
    """Discover dirty repos and enroll them into the active feature.

    Scans all workspace repos for uncommitted changes, enrolls any that
    aren't already part of the active feature, and materializes worktrees.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active = ws.get_active_feature()
    if not active:
        raise click.ClickException(
            "No active feature. Use 'mgit feature start' first."
        )

    # Resolve carry_repos
    carry_repos = None
    if carry is not None:
        feature = fm.get(active)
        all_repo_names = list(ws.repos.keys())
        dirty = find_dirty_repos(ws, all_repo_names)
        new_dirty = [(name, br) for name, br in dirty if name not in feature.branches]

        if carry is True:
            carry_repos = {name for name, _ in new_dirty}
        else:
            # --no-carry
            carry_repos = set()
    else:
        # Neither flag: prompt per dirty repo
        feature = fm.get(active)
        all_repo_names = list(ws.repos.keys())
        dirty = find_dirty_repos(ws, all_repo_names)
        new_dirty = [(name, br) for name, br in dirty if name not in feature.branches]

        carry_repos = set()
        for name, br in new_dirty:
            if click.confirm(
                f"  {name} has uncommitted changes (on {br}). "
                f"Carry to feature worktree?",
                default=False,
            ):
                carry_repos.add(name)

    try:
        synced = fm.sync(active, run_setup=not no_setup, carry_repos=carry_repos)
    except MgitError as e:
        raise click.ClickException(str(e))

    if synced:
        click.echo(f"Synced {len(synced)} repo(s) into feature '{active}':")
        for name in synced:
            carried = carry_repos is not None and name in carry_repos
            suffix = " (changes carried)" if carried else ""
            click.echo(f"  + {name}{suffix}")
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
