"""CLI commands: mgit repo {add,remove,list}."""

from pathlib import Path

import click

from mgit.core.workspace import Workspace
from mgit.core import repo as repo_ops
from mgit.utils.errors import MgitError


@click.group()
def repo():
    """Manage repos in the workspace."""


@repo.command("add")
@click.argument("url_or_path")
@click.option("--name", default=None, help="Override repo alias name.")
def add(url_or_path, name):
    """Add a repo by cloning a URL or symlinking a local path."""
    ws = Workspace.find()

    # Determine if it's a URL or a local path
    if url_or_path.startswith(("http://", "https://", "git@", "ssh://", "git://")):
        click.echo(f"Cloning {url_or_path}...")
        info = repo_ops.add_repo_from_url(ws.root, url_or_path, name=name)
    else:
        local = Path(url_or_path).resolve()
        if not local.is_dir():
            raise MgitError(f"Path not found: {local}")
        click.echo(f"Linking {local}...")
        try:
            info = repo_ops.add_repo_from_path(ws.root, local, name=name)
        except ValueError as e:
            raise MgitError(str(e))

    ws.add_repo(info)

    click.echo(f"Added repo '{info.name}' ({info.default_branch})")


@repo.command("remove")
@click.argument("name")
def remove(name):
    """Remove a repo from the workspace (does not delete files)."""
    ws = Workspace.find()
    ws.remove_repo(name)
    click.echo(f"Removed repo '{name}' from workspace.")


@repo.command("setup")
@click.argument("name")
@click.argument("command", required=False)
@click.option("--clear", is_flag=True, help="Remove the setup command.")
def setup(name, command, clear):
    """Show, set, or clear a repo's setup command.

    \b
    Examples:
      mgit repo setup frontend               # show current setup
      mgit repo setup frontend "npm install"  # set setup command
      mgit repo setup frontend --clear        # remove setup command
    """
    ws = Workspace.find()
    repo_info = ws.get_repo(name)

    if clear:
        repo_info.setup = None
        ws.save()
        click.echo(f"Cleared setup command for '{name}'.")
    elif command:
        repo_info.setup = command
        ws.save()
        click.echo(f"Setup for '{name}': {command}")
    else:
        if repo_info.setup:
            click.echo(f"Setup for '{name}': {repo_info.setup}")
        else:
            click.echo(f"No setup command configured for '{name}'.")


@repo.command("list")
def list_repos():
    """List all registered repos."""
    ws = Workspace.find()
    if not ws.repos:
        click.echo("No repos registered. Use 'mgit repo add' to add one.")
        return

    click.echo(f"Workspace: {ws.name}")
    click.echo()
    for name, info in sorted(ws.repos.items()):
        origin = info.url or "[local]"
        repo_path = ws.root / info.path
        if repo_path.exists() or (repo_path.is_symlink() and repo_path.resolve().exists()):
            from mgit.core.git import get_current_branch
            actual = repo_path.resolve() if repo_path.is_symlink() else repo_path
            branch = get_current_branch(actual)
        else:
            branch = info.default_branch
        click.echo(f"  {name:<20} ({branch})  {origin}")
