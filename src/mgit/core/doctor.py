"""Workspace health checks for 'mgit upgrade'.

Upgrading the mgit binary fixes behavior immediately, but state created by
older versions can linger: repo registrations from the old nested-scan bug,
sandbox branches left by old feature deletes, pinning refs for features that
no longer exist, mixed target branches from the old sync, and orphaned
sidecar directories. These checks name what lingers; the mechanical, safe
cases carry a fix that 'mgit upgrade --fix' applies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from mgit.core import git
from mgit.core.feature import FeatureManager
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace


@dataclass
class Finding:
    """One health finding; fix is set only when auto-fixing is safe."""

    message: str
    fix_description: str = ""
    fix: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def fixable(self) -> bool:
        return self.fix is not None


def run_checks(ws: Workspace) -> list[Finding]:
    """Detect legacy artifacts left by older mgit versions."""
    findings: list[Finding] = []
    fm = FeatureManager(ws)
    features = fm.list()
    feature_names = {f.name for f in features}

    # 1. Repo registrations that aren't git repo roots (old scan bug
    #    registered plain folders inside an outer repo)
    for name in sorted(ws.repos):
        path = ws.repo_path(name)
        if not path.exists():
            findings.append(Finding(
                f"repo '{name}' path is missing ({path}) — if intentional, "
                f"run 'mgit repo remove {name}'"
            ))
            continue
        root = git.repo_root(path)
        if root is None or root.resolve() != path.resolve():
            inside = f" (it is inside the repo at {root})" if root else ""
            findings.append(Finding(
                f"repo '{name}' is registered but is not a git repo root"
                f"{inside} — likely registered by an older mgit's scan",
                fix_description=f"unregister repo '{name}' (files untouched)",
                fix=lambda n=name: ws.remove_repo(n),
            ))

    # 2. Features whose repos target diverging remote branches (old sync
    #    enrolled repos with the feature name, ignoring a custom --branch)
    for f in features:
        targets = set(f.branches.values())
        if len(targets) > 1:
            detail = ", ".join(f"{r} -> {t}" for r, t in sorted(f.branches.items()))
            findings.append(Finding(
                f"feature '{f.name}' has mixed target branches ({detail}) — "
                f"if unintended, edit .mgit/features/{f.name}.toml"
            ))

    # 3./4. Orphaned sandbox branches and pinning refs for features that no
    #    longer exist (old delete left both behind)
    for name in sorted(ws.repos):
        path = ws.repo_path(name)
        if not path.exists():
            continue
        try:
            repo = Repo(ws.get_repo(name), ws.root)
            heads = repo.list_refs("refs/heads/mgit/")
            mgit_refs = repo.list_refs("refs/mgit/")
        except Exception:
            continue

        for ref in sorted(heads):
            feat = ref.removeprefix("refs/heads/mgit/")
            if feat in feature_names:
                continue

            def _delete_branch(r=repo, branch=f"mgit/{feat}"):
                git.run_git("worktree", "prune", cwd=r.path, check=False)
                git.run_git("branch", "-D", branch, cwd=r.path, check=False)

            findings.append(Finding(
                f"{name}: orphaned sandbox branch 'mgit/{feat}' — feature no "
                f"longer exists; a recreated feature named '{feat}' would "
                f"resurrect its commits",
                fix_description=f"delete branch 'mgit/{feat}' in '{name}'",
                fix=_delete_branch,
            ))

        for ref in sorted(mgit_refs):
            # refs/mgit/checkpoint/<feature>/<cp-id> | refs/mgit/wip/<feature>/<repo>
            parts = ref.split("/")
            feat = parts[3] if len(parts) > 3 else None
            if not feat or feat in feature_names:
                continue
            findings.append(Finding(
                f"{name}: orphaned ref '{ref}' pins objects for a deleted feature",
                fix_description=f"delete ref '{ref}' in '{name}'",
                fix=lambda r=repo, rf=ref: r.delete_ref(rf),
            ))

    # 5. Sidecar/checkpoint directories whose feature file is gone
    if ws.features_dir.exists():
        for d in sorted(ws.features_dir.iterdir()):
            if d.is_dir() and d.name not in feature_names:
                findings.append(Finding(
                    f"orphaned memory sidecar '{d}' (feature deleted) — "
                    f"contains its journal; remove manually if unwanted"
                ))
    if ws.checkpoints_dir.exists():
        for d in sorted(ws.checkpoints_dir.iterdir()):
            if d.is_dir() and d.name not in feature_names:
                findings.append(Finding(
                    f"orphaned checkpoint manifests '{d}' (feature deleted) — "
                    f"remove manually if unwanted"
                ))

    return findings
