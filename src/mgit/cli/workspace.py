"""CLI command: mgit init."""

from pathlib import Path

import click

from mgit.core.workspace import Workspace
from mgit.utils.errors import WorkspaceExistsError


@click.command()
@click.argument("name", required=False)
@click.option("--no-interactive", is_flag=True, help="Auto-add all found repos without prompting.")
def init(name, no_interactive):
    """Initialize a new mgit workspace.

    If NAME is given, creates a new directory with that name.
    Otherwise, initializes in the current directory.
    """
    if name:
        path = Path.cwd() / name
    else:
        path = Path.cwd()

    try:
        ws, found_repos = Workspace.init(path, name=name, scan=True, auto_add=False)
    except WorkspaceExistsError as e:
        raise click.ClickException(str(e))

    click.echo(f"Initializing mgit workspace in {ws.root}...")
    click.echo()

    if found_repos:
        click.echo("Scanning for existing git repos...")
        click.echo(f"Found {len(found_repos)} git {'repository' if len(found_repos) == 1 else 'repositories'}:")
        for i, repo in enumerate(found_repos, 1):
            origin = repo.url or "[local]"
            click.echo(f"  {i}. {repo.name:<20} ({repo.default_branch})  {origin}")
        click.echo()

        if no_interactive:
            choice = "y"
        else:
            choice = click.prompt(
                "Add all found repos to workspace? [Y/n/select]",
                default="Y",
                show_default=False,
            ).strip().lower()

        if choice in ("y", ""):
            for repo in found_repos:
                ws.repos[repo.name] = repo
            ws.save()
            click.echo(f"Registered {len(found_repos)} repos.")
        elif choice == "select":
            _interactive_select(ws, found_repos)
        else:
            click.echo("Skipped. Starting with empty workspace.")
    else:
        click.echo("No existing git repos found in subdirectories.")

    click.echo()
    click.echo("Workspace ready! Here's what you can do next:")
    click.echo()
    click.echo("  mgit repo add <url>              Clone another repo into this workspace")
    click.echo("  mgit feature create <name>       Start a new cross-repo feature")
    click.echo("  mgit status                      See status across all repos")
    click.echo("  mgit --help                      See all commands")
    click.echo()


def _interactive_select(ws: Workspace, repos):
    """Let the user pick specific repos to add."""
    click.echo("Enter repo numbers to add (comma-separated), or 'done' to finish:")
    for i, repo in enumerate(repos, 1):
        click.echo(f"  [{i}] {repo.name}")

    selection = click.prompt("Selection").strip()
    if selection.lower() == "done":
        return

    added = 0
    for part in selection.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(repos):
                ws.repos[repos[idx].name] = repos[idx]
                added += 1

    ws.save()
    click.echo(f"Registered {added} repos.")
