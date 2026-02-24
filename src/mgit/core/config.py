"""TOML read/write helpers for workspace and feature configs."""

from __future__ import annotations

import tomllib
from pathlib import Path

import tomli_w

from mgit.models.types import FeatureInfo, RepoInfo


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
    """Build the feature config dict for serialization."""
    data: dict = {
        "feature": {
            "name": feature.name,
            "description": feature.description,
        },
        "branches": dict(feature.branches),
    }
    return data


def dict_to_feature(data: dict) -> FeatureInfo:
    """Parse a feature TOML dict into a FeatureInfo object."""
    feat = data["feature"]
    return FeatureInfo(
        name=feat["name"],
        description=feat.get("description", ""),
        branches=dict(data.get("branches", {})),
    )
