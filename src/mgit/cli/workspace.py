"""CLI commands: mgit init, mgit remove, mgit upgrade."""

from pathlib import Path

import click

from mgit.core.workspace import Workspace


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

    ws, found_repos = Workspace.init(path, name=name, scan=True, auto_add=False)

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
    click.echo("  mgit feature start <name>        Start a new cross-repo feature")
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
    ws = Workspace.find()

    if not force:
        click.echo(f"This will remove the mgit workspace at {ws.root}")
        click.echo("  - Deletes .mgit/ (config, features, active state)")
        click.echo("  - Strips mgit's block from AGENT.md / AGENTS.md")
        click.echo("  - Repos and other files are NOT touched")
        if not click.confirm("Continue?"):
            click.echo("Aborted.")
            return

    ws.remove()
    click.echo(f"Removed mgit workspace from {ws.root}.")


@click.command()
@click.option("--fix", "apply_fix", is_flag=True, default=False,
              help="Apply the safe automatic fixes for detected legacy artifacts.")
def upgrade(apply_fix):
    """Refresh generated files and check for artifacts left by older versions.

    Rewrites AGENT.md/AGENTS.md from the current template (backing up a
    locally modified AGENT.md to AGENT.md.bak), regenerates the ambient
    brief files for every feature, then runs health checks for legacy state:
    bogus repo registrations, orphaned sandbox branches and pinning refs of
    deleted features, mixed target branches, orphaned sidecars. Mechanical
    fixes are applied with --fix; everything else is reported with the
    manual remedy. Code-behavior fixes need none of this — they apply the
    moment the binary is upgraded.
    """
    from mgit.core.brief import write_feature_brief
    from mgit.core.doctor import run_checks
    from mgit.core.feature import FeatureManager, validate_feature_name
    from mgit.utils.errors import MgitError

    ws = Workspace.find()
    fm = FeatureManager(ws)

    results = ws.write_agent_md()
    click.echo(
        f"Refreshed {', '.join(f'{n} ({a})' for n, a in results)} in {ws.root}."
    )
    for name, action in results:
        if action == "merged":
            click.echo(f"  ({name} had other content; mgit's block was appended)")

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

    findings = run_checks(ws)
    if not findings:
        click.echo("Health checks: no legacy issues found.")
        return

    click.echo(f"Health checks: {len(findings)} issue(s) found.")
    fixable = [f for f in findings if f.fixable]
    for f in findings:
        click.secho(f"  warn  {f.message}", fg="yellow")
        if f.fixable and apply_fix:
            try:
                f.fix()
                click.echo(f"  fixed {f.fix_description}")
            except Exception as e:
                click.secho(f"  FAILED to fix ({e})", fg="red", err=True)
        elif f.fixable:
            click.echo(f"        [--fix would: {f.fix_description}]")
    if fixable and not apply_fix:
        click.echo(f"Run 'mgit upgrade --fix' to apply {len(fixable)} safe fix(es).")
