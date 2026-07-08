"""Versioned JSON envelope for agent-facing output.

Contract (mgit_schema 1, additive-only evolution):
  {"mgit_schema": 1, "ok": bool, "command": str, "data": {...}, "error": null|{...}}

Exit codes: 0 success, 1 bulk partial failure, 2 domain error (MgitError),
3 usage error, 4 unexpected internal error.
"""

from __future__ import annotations

import json

import click

MGIT_SCHEMA = 1

_json_mode = False


def set_json_mode(on: bool = True) -> None:
    """Mark this invocation as JSON-emitting so errors use the envelope too."""
    global _json_mode
    _json_mode = on


def json_mode() -> bool:
    return _json_mode


def emit(command: str, data: dict) -> None:
    """Print a success envelope on stdout."""
    click.echo(json.dumps({
        "mgit_schema": MGIT_SCHEMA,
        "ok": True,
        "command": command,
        "data": data,
        "error": None,
    }))


def error_object(exc: BaseException) -> dict:
    """Build a typed error object from an exception."""
    err: dict = {"type": type(exc).__name__, "message": str(exc)}
    details: dict = {}
    returncode = getattr(exc, "returncode", None)
    if returncode is not None:
        details["returncode"] = returncode
    stderr = getattr(exc, "stderr", "")
    if stderr:
        details["stderr"] = stderr
    dirty = getattr(exc, "dirty_repos", None)
    if dirty:
        details["dirty_repos"] = dirty
    if details:
        err["details"] = details
    return err


def emit_error(exc: BaseException) -> None:
    """Print an error envelope on stdout."""
    click.echo(json.dumps({
        "mgit_schema": MGIT_SCHEMA,
        "ok": False,
        "command": None,
        "data": None,
        "error": error_object(exc),
    }))
