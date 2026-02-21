"""CLI commands: mgit feature {create,start,switch,activate,deactivate,show,list,delete,remove-repo}."""

import click

from mgit.core.feature import (
    FeatureManager,
    find_dirty_non_sandbox_repos,
    sandbox_branch,
)
from mgit.core.workspace import Workspace
from mgit.utils.errors import MgitError


def _prompt_dirty_action(dirty_repos: list[tuple[str, str]]) -> str:
    """Show dirty non-sandbox repos and ask the user what to do.

    Returns "stash" or "carry".
    """
    click.echo("Repos with uncommitted changes:")
    for name, branch in dirty_repos:
        click.echo(f"  {name} (on {branch})")
    click.echo()
    choice = click.prompt(
        "Stash these changes or carry them to the feature branch?",
        type=click.Choice(["stash", "carry"], case_sensitive=False),
        default="stash",
    )
    return choice.lower()


def _resolve_dirty_action(
    stash_flag: bool, carry_flag: bool,
    dirty_repos: list[tuple[str, str]],
) -> str:
    """Determine dirty_action from flags or by prompting.

    Returns "stash" or "carry".
    """
    if stash_flag:
        return "stash"
    if carry_flag:
        return "carry"
    if dirty_repos:
        return _prompt_dirty_action(dirty_repos)
    return "carry"


@click.group()
def feature():
    """Manage cross-repo features."""


@feature.command("create")
@click.argument("name")
@click.option("--description", "-d", default="", help="Feature description.")
def create(name, description):
    """Create an empty feature (for setting description upfront)."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    try:
        fm.create(name, description=description)
    except MgitError as e:
        raise click.ClickException(str(e))

    ws.set_active_feature(name)
    click.echo(f"Created feature '{name}'.")


@feature.command("start")
@click.argument("feature_name")
@click.option(
    "--repo", "-r", "repo_names", multiple=True,
    help="Repo name (can be repeated).",
)
@click.option("--branch", default=None, help="Remote target branch (default: feature name).")
@click.option("--description", "-d", default="", help="Feature description (only on creation).")
@click.option("--stash", "stash_flag", is_flag=True, help="Stash uncommitted changes before switching.")
@click.option("--carry", "carry_flag", is_flag=True, help="Carry uncommitted changes to the feature branch.")
def start(feature_name, repo_names, branch, description, stash_flag, carry_flag):
    """Start working on a feature: create/enroll repos and switch to sandbox branches.

    If no -r is given, auto-detects repo from current directory.
    If repos have uncommitted changes on non-feature branches, prompts to stash or carry
    (use --stash or --carry to skip the prompt).
    """
    if stash_flag and carry_flag:
        raise click.ClickException("Cannot use both --stash and --carry.")

    ws = Workspace.find()
    fm = FeatureManager(ws)

    repo_list = list(repo_names)

    # Auto-detect repo from cwd if none specified
    if not repo_list:
        detected = ws.detect_repo_from_cwd()
        if detected:
            repo_list = [detected]
        else:
            raise click.ClickException(
                "No repos specified and cwd is not inside a registered repo. "
                "Use -r to specify repos."
            )

    # Check for dirty repos on non-sandbox branches
    dirty_repos = find_dirty_non_sandbox_repos(ws, repo_list)
    dirty_action = _resolve_dirty_action(stash_flag, carry_flag, dirty_repos)

    try:
        feat, newly_added = fm.start(
            feature_name, repo_list,
            target_branch=branch,
            description=description,
            dirty_action=dirty_action,
        )
    except MgitError as e:
        raise click.ClickException(str(e))

    sb = sandbox_branch(feature_name)
    for repo_name in repo_list:
        if repo_name in newly_added:
            click.echo(f"  {repo_name}: enrolled, on {sb}")
        else:
            click.echo(f"  {repo_name}: on {sb}")

    click.echo(f"Active feature: {feature_name}")


@feature.command("switch")
@click.argument("name")
@click.option("--stash", "stash_flag", is_flag=True, help="Stash uncommitted changes before switching.")
@click.option("--carry", "carry_flag", is_flag=True, help="Carry uncommitted changes to the feature branch.")
def switch(name, stash_flag, carry_flag):
    """Switch all enrolled repos to feature's sandbox branches (auto-stash/unstash).

    If repos have uncommitted changes on non-feature branches, prompts to stash or carry
    (use --stash or --carry to skip the prompt).
    """
    if stash_flag and carry_flag:
        raise click.ClickException("Cannot use both --stash and --carry.")

    ws = Workspace.find()
    fm = FeatureManager(ws)

    # Load feature to get enrolled repos
    try:
        feat = fm.get(name)
    except MgitError as e:
        raise click.ClickException(str(e))

    # Check for dirty repos on non-sandbox branches
    dirty_repos = find_dirty_non_sandbox_repos(ws, list(feat.branches.keys()))
    dirty_action = _resolve_dirty_action(stash_flag, carry_flag, dirty_repos)

    try:
        switched = fm.switch(name, dirty_action=dirty_action)
    except MgitError as e:
        raise click.ClickException(str(e))

    for repo_name, branch_name in switched.items():
        click.echo(f"  {repo_name}: switched to {branch_name}")
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
            click.echo(f"  {repo:<20} -> {target}")
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
    """Delete a feature definition."""
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
    """Remove a repo from a feature."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        fm.remove_repo(feature_name, repo_name)
    except MgitError as e:
        raise click.ClickException(str(e))
    click.echo(f"Removed '{repo_name}' from feature '{feature_name}'.")
