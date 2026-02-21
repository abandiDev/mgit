"""CLI command: mgit context — machine-readable workspace state."""

from __future__ import annotations

import json

import click

from mgit.core.feature import FeatureManager, sandbox_branch
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace


@click.command("context")
@click.option("--pretty", is_flag=True, help="Pretty-print JSON output.")
def context(pretty):
    """Output workspace state as JSON for agent discovery."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active_name = ws.get_active_feature()
    current_repo = ws.detect_repo_from_cwd()

    # Build active feature info
    active_feature_data = None
    if active_name:
        try:
            af = fm.get(active_name)
            wt_paths = fm.get_worktree_paths(active_name)
            active_feature_data = {
                "name": af.name,
                "description": af.description,
                "sandbox_branch": sandbox_branch(af.name),
                "repos": dict(af.branches),
                "worktree_paths": {
                    name: str(path) for name, path in wt_paths.items()
                },
                "materialized": {
                    name: path.exists() for name, path in wt_paths.items()
                },
            }
        except Exception:
            pass

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
        features_list.append({
            "name": f.name,
            "repos": list(f.branches.keys()),
            "worktree_paths": {
                name: str(path) for name, path in wt_paths.items()
            },
            "materialized": {
                name: path.exists() for name, path in wt_paths.items()
            },
        })

    output = {
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
