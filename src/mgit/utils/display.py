"""Terminal formatting, colors, and tables for output."""

from __future__ import annotations

import click

from mgit.models.types import BulkResult, OpStatus, RepoOpResult


STATUS_COLORS = {
    OpStatus.SUCCESS: "green",
    OpStatus.SKIPPED: "yellow",
    OpStatus.FAILED: "red",
}

STATUS_SYMBOLS = {
    OpStatus.SUCCESS: "+",
    OpStatus.SKIPPED: "~",
    OpStatus.FAILED: "!",
}


def print_repo_result(result: RepoOpResult, verbose: bool = True) -> None:
    """Print a single repo operation result."""
    symbol = STATUS_SYMBOLS[result.status]
    color = STATUS_COLORS[result.status]
    click.secho(f"  [{symbol}] {result.repo}", fg=color, nl=False)
    if result.message:
        click.echo(f": {result.message}")
    else:
        click.echo()
    if verbose and result.output and result.output.strip():
        for line in result.output.strip().splitlines():
            click.echo(f"      {line}")


def print_bulk_summary(bulk: BulkResult) -> None:
    """Print summary line for a bulk operation."""
    parts = []
    if bulk.succeeded:
        parts.append(click.style(f"{len(bulk.succeeded)} succeeded", fg="green"))
    if bulk.skipped:
        parts.append(click.style(f"{len(bulk.skipped)} skipped", fg="yellow"))
    if bulk.failed:
        parts.append(click.style(f"{len(bulk.failed)} failed", fg="red"))

    click.echo()
    click.echo(f"Summary: {', '.join(parts)}")


def print_bulk_results(bulk: BulkResult, verbose: bool = True) -> None:
    """Print all results and summary."""
    for result in bulk.results:
        print_repo_result(result, verbose=verbose)
    print_bulk_summary(bulk)
