"""CLI entry point - root click group wiring all commands."""

import sys

import click

from mgit.utils import output
from mgit.utils.errors import MgitError

# Exit-code contract (frozen, see AGENT.md):
#   0 success | 1 bulk partial failure | 2 domain error (MgitError)
#   3 usage error | 4 unexpected internal error


@click.group()
@click.version_option(package_name="mgit")
def main():
    """mgit - Multi-repo git workspace manager."""


def cli():
    """Entry point mapping exceptions to the frozen exit-code table."""
    try:
        main(standalone_mode=False)
    except MgitError as e:
        if output.json_mode():
            output.emit_error(e)
        else:
            click.secho(f"Error: {e}", fg="red", err=True)
        sys.exit(2)
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(3)
    except click.ClickException as e:  # includes UsageError
        e.show()
        sys.exit(3)
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        if output.json_mode():
            output.emit_error(e)
        else:
            click.secho(f"Internal error: {type(e).__name__}: {e}", fg="red", err=True)
        sys.exit(4)


# Import and register subcommands
from mgit.cli.workspace import init, remove, upgrade  # noqa: E402
from mgit.cli.repo import repo  # noqa: E402
from mgit.cli.feature import feature  # noqa: E402
from mgit.cli.context import context  # noqa: E402
from mgit.cli.checkpoint import checkpoint  # noqa: E402
from mgit.cli.skill import skill  # noqa: E402
from mgit.cli.bulk import status, pull, push, commit, exec_cmd, setup_cmd  # noqa: E402

main.add_command(init)
main.add_command(remove)
main.add_command(upgrade)
main.add_command(repo)
main.add_command(feature)
main.add_command(context)
main.add_command(checkpoint)
main.add_command(skill)
main.add_command(status)
main.add_command(pull)
main.add_command(push)
main.add_command(commit)
main.add_command(exec_cmd)
main.add_command(setup_cmd)
