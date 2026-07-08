"""CLI commands: mgit init, mgit remove, mgit upgrade."""

from pathlib import Path

import click

from mgit.core.workspace import Workspace
from mgit.utils.errors import WorkspaceExistsError, WorkspaceNotFoundError


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


@click.command()
@click.option("--force", is_flag=True, help="Skip confirmation prompt.")
def remove(force):
    """Remove the mgit workspace (deletes .mgit/ and AGENT.md).

    Repos and other files are left untouched.
    """
    try:
        ws = Workspace.find()
    except WorkspaceNotFoundError as e:
        raise click.ClickException(str(e))

    if not force:
        click.echo(f"This will remove the mgit workspace at {ws.root}")
        click.echo("  - Deletes .mgit/ (config, features, active state)")
        click.echo("  - Deletes AGENT.md")
        click.echo("  - Repos and other files are NOT touched")
        if not click.confirm("Continue?"):
            click.echo("Aborted.")
            return

    ws.remove()
    click.echo(f"Removed mgit workspace from {ws.root}.")


@click.command()
def upgrade():
    """Refresh generated files after an mgit version upgrade.

    Rewrites AGENT.md/AGENTS.md from the current template (backing up a
    locally modified AGENT.md to AGENT.md.bak) and regenerates the ambient
    brief files for every feature. Everything else upgrades lazily —
    memory sidecars and lineage fields appear as features are used.
    """
    from mgit.core.brief import write_feature_brief
    from mgit.core.feature import FeatureManager, validate_feature_name
    from mgit.utils.errors import MgitError

    ws = Workspace.find()
    fm = FeatureManager(ws)

    written = ws.write_agent_md(backup=True)
    click.echo(f"Regenerated {', '.join(written)} in {ws.root}.")
    if (ws.root / "AGENT.md.bak").exists():
        click.echo("  (previous AGENT.md backed up to AGENT.md.bak)")

    count = 0
    for feat in fm.list():
        write_feature_brief(ws, feat)
        count += 1
        try:
            validate_feature_name(feat.name)
        except MgitError:
            click.secho(
                f"  Warning: feature '{feat.name}' has a name that can't be used "
                "in checkpoint refs — consider recreating it under a valid name.",
                fg="yellow", err=True,
            )
    click.echo(f"Regenerated ambient briefs for {count} feature(s).")
