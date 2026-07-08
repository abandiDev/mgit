"""TOML read/write helpers for workspace and feature configs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

from mgit.models.types import CheckpointInfo, CheckpointRepoState, FeatureInfo, RepoInfo


def read_toml(path: Path) -> dict:
    """Read a TOML file and return the parsed dict."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def write_toml(path: Path, data: dict) -> None:
    """Write a dict to a TOML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def workspace_config_to_dict(
    name: str, repos: dict[str, RepoInfo]
) -> dict:
    """Build the workspace config dict for serialization."""
    data: dict = {
        "workspace": {"name": name},
        "repos": {},
    }
    for repo_name, repo in repos.items():
        entry: dict[str, str] = {"path": repo.path}
        if repo.url:
            entry["url"] = repo.url
        entry["default_branch"] = repo.default_branch
        if repo.setup:
            entry["setup"] = repo.setup
        data["repos"][repo_name] = entry
    return data


def dict_to_repos(repos_dict: dict) -> dict[str, RepoInfo]:
    """Parse the [repos] section of config into RepoInfo objects."""
    repos = {}
    for name, entry in repos_dict.items():
        repos[name] = RepoInfo(
            name=name,
            path=entry["path"],
            url=entry.get("url"),
            default_branch=entry.get("default_branch", "main"),
            setup=entry.get("setup"),
        )
    return repos


def feature_to_dict(feature: FeatureInfo) -> dict:
    """Build the feature config dict for serialization.

    Lineage fields (parent, fork_base, wip_from) are omitted when empty so
    feature files created before they existed stay byte-identical on rewrite.
    """
    data: dict = {
        "feature": {
            "name": feature.name,
            "description": feature.description,
        },
        "branches": dict(feature.branches),
    }
    if feature.parent:
        data["feature"]["parent"] = feature.parent
    if feature.fork_base:
        data["fork_base"] = dict(feature.fork_base)
    if feature.wip_from:
        data["wip_from"] = dict(feature.wip_from)
    return data


def dict_to_feature(data: dict) -> FeatureInfo:
    """Parse a feature TOML dict into a FeatureInfo object."""
    feat = data["feature"]
    return FeatureInfo(
        name=feat["name"],
        description=feat.get("description", ""),
        branches=dict(data.get("branches", {})),
        parent=feat.get("parent"),
        fork_base=dict(data.get("fork_base", {})),
        wip_from=dict(data.get("wip_from", {})),
    )


def checkpoint_to_dict(cp: CheckpointInfo) -> dict:
    """Build the checkpoint manifest dict for serialization."""
    data: dict = {
        "checkpoint": {
            "id": cp.id,
            "feature": cp.feature,
            "label": cp.label,
            "created_at": cp.created_at,
        },
        "repos": {},
    }
    for name, st in cp.repos.items():
        data["repos"][name] = {
            "head": st.head,
            "snapshot": st.snapshot,
            "branch": st.branch,
            "dirty": st.dirty,
        }
    return data


def dict_to_checkpoint(data: dict) -> CheckpointInfo:
    """Parse a checkpoint manifest TOML dict into a CheckpointInfo object."""
    cp = data["checkpoint"]
    repos = {}
    for name, entry in data.get("repos", {}).items():
        repos[name] = CheckpointRepoState(
            head=entry["head"],
            snapshot=entry.get("snapshot", entry["head"]),
            branch=entry.get("branch", ""),
            dirty=entry.get("dirty", False),
        )
    return CheckpointInfo(
        id=cp["id"],
        feature=cp["feature"],
        label=cp.get("label", ""),
        created_at=cp.get("created_at", ""),
        repos=repos,
    )
