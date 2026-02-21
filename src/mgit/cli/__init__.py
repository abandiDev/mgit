"""CLI entry point - root click group wiring all commands."""

import sys

import click

from mgit.utils.errors import MgitError


@click.group()
@click.version_option(package_name="mgit")
def main():
    """mgit - Multi-repo git workspace manager."""


def cli():
    """Entry point that handles MgitError gracefully."""
    try:
        main(standalone_mode=False)
    except MgitError as e:
        click.secho(f"Error: {e}", fg="red", err=True)
        sys.exit(2)
    except SystemExit as e:
        sys.exit(e.code)


# Import and register subcommands
from mgit.cli.workspace import init  # noqa: E402
from mgit.cli.repo import repo  # noqa: E402
from mgit.cli.feature import feature  # noqa: E402
from mgit.cli.bulk import status, pull, push, commit, exec_cmd  # noqa: E402

main.add_command(init)
main.add_command(repo)
main.add_command(feature)
main.add_command(status)
main.add_command(pull)
main.add_command(push)
main.add_command(commit)
main.add_command(exec_cmd)
