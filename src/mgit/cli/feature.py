"""CLI commands: mgit feature {create,delete,list,show,switch,add-repo,remove-repo}."""

import click

from mgit.core.feature import FeatureManager
from mgit.core.workspace import Workspace
from mgit.utils.errors import MgitError


def _parse_repo_branch(value: str) -> tuple[str, str]:
    """Parse 'repo:branch' string."""
    if ":" not in value:
        raise click.BadParameter(
            f"Expected 'repo:branch' format, got '{value}'"
        )
    repo, branch = value.split(":", 1)
    return repo.strip(), branch.strip()


@click.group()
def feature():
    """Manage cross-repo features."""


@feature.command("create")
@click.argument("name")
@click.option(
    "--repo", "-r", "repo_branches", multiple=True, required=True,
    help="Repo:branch pair (can be repeated).",
)
@click.option("--description", "-d", default="", help="Feature description.")
def create(name, repo_branches, description):
    """Create a new feature with repo:branch mappings."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    branches = {}
    for rb in repo_branches:
        try:
            repo, branch = _parse_repo_branch(rb)
        except click.BadParameter as e:
            raise click.ClickException(str(e))
        branches[repo] = branch

    try:
        fm.create(name, branches, description=description)
    except MgitError as e:
        raise click.ClickException(str(e))

    click.echo(f"Created feature '{name}' with {len(branches)} repo(s).")


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


@feature.command("list")
def list_features():
    """List all features."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    features = fm.list()
    if not features:
        click.echo("No features defined. Use 'mgit feature create' to create one.")
        return

    for f in features:
        repos = ", ".join(f.branches.keys())
        desc = f" - {f.description}" if f.description else ""
        click.echo(f"  {f.name}{desc}  [{repos}]")


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

    click.echo(f"Feature: {f.name}")
    if f.description:
        click.echo(f"Description: {f.description}")
    click.echo("Branches:")
    for repo, branch in f.branches.items():
        click.echo(f"  {repo:<20} -> {branch}")


@feature.command("switch")
@click.argument("name")
def switch(name):
    """Switch all repos in a feature to their feature branches."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        switched = fm.switch(name)
    except MgitError as e:
        raise click.ClickException(str(e))

    for repo, branch in switched.items():
        click.echo(f"  {repo}: switched to {branch}")
    click.echo(f"Switched {len(switched)} repo(s).")


@feature.command("add-repo")
@click.argument("feature_name")
@click.argument("repo_branch")
def add_repo(feature_name, repo_branch):
    """Add a repo:branch mapping to a feature."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    try:
        repo, branch = _parse_repo_branch(repo_branch)
        fm.add_repo(feature_name, repo, branch)
    except (MgitError, click.BadParameter) as e:
        raise click.ClickException(str(e))
    click.echo(f"Added {repo}:{branch} to feature '{feature_name}'.")


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
