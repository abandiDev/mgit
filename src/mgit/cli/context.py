"""CLI command: mgit context — machine-readable workspace state."""

from __future__ import annotations

import json

import click

from mgit.core import memory
from mgit.core.checkpoint import CheckpointManager
from mgit.core.feature import FeatureManager, sandbox_branch
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.utils import output as output_mod


def _memory_block(ws, name: str) -> dict:
    state = memory.load_state(ws, name)
    tail, _ = memory.journal_tail(ws, name, 5)
    return {
        "goal": state.goal,
        "status": state.status,
        "next_steps": list(state.next_steps),
        "open_questions": list(state.open_questions),
        "updated_at": state.updated_at,
        "journal_tail": tail,
    }


def _checkpoint_block(ws, name: str) -> dict:
    cps = CheckpointManager(ws).list(name)
    last = cps[-1] if cps else None
    return {
        "checkpoint_count": len(cps),
        "last_checkpoint": (
            {"id": last.id, "label": last.label, "created_at": last.created_at}
            if last else None
        ),
    }


def _feature_block(ws, fm, name: str, deep: bool = False) -> dict:
    af = fm.get(name)
    wt_paths = fm.get_worktree_paths(name)
    block = {
        "name": af.name,
        "description": af.description,
        "parent": af.parent,
        "sandbox_branch": sandbox_branch(af.name),
        "repos": dict(af.branches),
        "worktree_paths": {n: str(p) for n, p in wt_paths.items()},
        "materialized": {n: p.exists() for n, p in wt_paths.items()},
        "memory": _memory_block(ws, name),
        **_checkpoint_block(ws, name),
    }
    if deep:
        # Live per-repo git facts (dirty, ahead-of-base, last commit)
        block["repo_facts"] = [memory.repo_facts(ws, af, r) for r in af.branches]
    return block


@click.command("context")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
@click.option("--feature", "-f", "feature_scope", default=None,
              help="Deep read of one feature ('.' = active) instead of the full workspace.")
def context(pretty, feature_scope):
    """Output workspace state as JSON for agent discovery."""
    output_mod.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active_name = ws.get_active_feature()
    current_repo = ws.detect_repo_from_cwd()

    if feature_scope:
        name = ws.resolve_feature(feature_scope)
        if not name:
            from mgit.utils.errors import MgitError
            raise MgitError("No feature in scope. Pass -f <name>.")
        out = {
            "mgit_schema": output_mod.MGIT_SCHEMA,
            "workspace": {"name": ws.name, "root": str(ws.root)},
            "current_repo": current_repo,
            "feature": _feature_block(ws, fm, name, deep=True),
        }
        click.echo(json.dumps(out, indent=2 if pretty else None))
        return

    # Build active feature info
    active_feature_data = None
    if active_name:
        try:
            active_feature_data = _feature_block(ws, fm, active_name)
        except Exception as e:
            active_feature_data = {
                "name": active_name,
                "error": f"{type(e).__name__}: {e}",
            }

    # Build repos info
    repos_data = {}
    for name, repo_info in ws.repos.items():
        repo = Repo(repo_info, ws.root)
        try:
            branch = repo.current_branch()
            dirty = repo.is_dirty()
        except Exception:
            branch = "unknown"
            dirty = False

        repos_data[name] = {
            "path": repo_info.path,
            "url": repo_info.url,
            "default_branch": repo_info.default_branch,
            "current_branch": branch,
            "dirty": dirty,
        }

    # Build features list
    features_list = []
    for f in fm.list():
        wt_paths = fm.get_worktree_paths(f.name)
        state = memory.load_state(ws, f.name)
        features_list.append({
            "name": f.name,
            "description": f.description,
            "parent": f.parent,
            "status": state.status,
            "updated_at": state.updated_at,
            "repos": list(f.branches.keys()),
            "worktree_paths": {
                name: str(path) for name, path in wt_paths.items()
            },
            "materialized": {
                name: path.exists() for name, path in wt_paths.items()
            },
        })

    output = {
        "mgit_schema": output_mod.MGIT_SCHEMA,
        "workspace": {
            "name": ws.name,
            "root": str(ws.root),
        },
        "current_repo": current_repo,
        "active_feature": active_feature_data,
        "repos": repos_data,
        "features": features_list,
    }

    indent = 2 if pretty else None
    click.echo(json.dumps(output, indent=indent))
