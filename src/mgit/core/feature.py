"""FeatureManager: CRUD operations and worktree-based workflow for cross-repo features."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from mgit.core import config, git, memory
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import FeatureInfo
from mgit.utils.errors import (
    FeatureExistsError,
    FeatureNotFoundError,
    MgitError,
    RepoNotFoundError,
)

# FeatureManager.list() shadows the builtin inside that class's body, so
# annotations there cannot spell list[str] directly.
StrList = list[str]

# Feature names become branch refs (mgit/<name>), checkpoint refs, and
# directory names — reject anything path- or ref-hostile.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_feature_name(name: str) -> None:
    """Raise MgitError if a feature name can't be used in refs and paths."""
    if (not _NAME_RE.match(name) or ".." in name
            or name.endswith((".", ".lock", ".toml"))):
        raise MgitError(
            f"Invalid feature name '{name}': use letters, digits, '.', '_', '-' "
            "(no '/', no leading '-' or '.', no '..', no '.toml' suffix)"
        )


def sandbox_branch(feature_name: str) -> str:
    """Return the sandbox branch name for a feature."""
    return f"mgit/{feature_name}"


# Removing a worktree destroys whatever is in it. Before that happens the tree
# is pinned here, in the origin repo's shared ref store: gc-immune, survives
# the worktree, and — unlike checkpoint refs — NOT swept by feature deletion.
RESCUE_REF_PREFIX = "refs/mgit/rescue"


def rescue_ref(feature_name: str, repo_name: str) -> str:
    """Ref pinning a worktree's last state before mgit destroyed it."""
    return f"{RESCUE_REF_PREFIX}/{feature_name}/{repo_name}"


def find_dirty_repos(ws: Workspace, repo_names: list[str]) -> list[tuple[str, str]]:
    """Find repos with uncommitted changes.

    Returns list of (repo_name, current_branch) tuples.
    """
    dirty = []
    for name in repo_names:
        if name not in ws.repos:
            continue
        repo = Repo(ws.get_repo(name), ws.root)
        if repo.is_dirty():
            dirty.append((name, repo.current_branch()))
    return dirty


class FeatureManager:
    """Manages feature lifecycle within a workspace."""

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def _feature_path(self, name: str) -> Path:
        return self.ws.features_dir / f"{name}.toml"

    def _save_feature(self, feature: FeatureInfo) -> None:
        config.write_toml(
            self._feature_path(feature.name),
            config.feature_to_dict(feature),
        )

    def _regen_brief(self, feature: FeatureInfo) -> None:
        """Regenerate the ambient CLAUDE.md/AGENTS.md brief (never raises)."""
        from mgit.core.brief import write_feature_brief
        write_feature_brief(self.ws, feature)

    def create(
        self,
        name: str,
        description: str = "",
    ) -> FeatureInfo:
        """Create a new empty feature definition.

        Args:
            name: Feature name (used as filename).
            description: Optional description.

        Raises:
            FeatureExistsError: If feature already exists.
        """
        validate_feature_name(name)
        path = self._feature_path(name)
        if path.exists():
            raise FeatureExistsError(f"Feature '{name}' already exists")

        feature = FeatureInfo(
            name=name,
            description=description,
            branches={},
        )
        config.write_toml(path, config.feature_to_dict(feature))
        memory.append_event(
            self.ws, name, f"Feature '{name}' created", event="created",
            meta={"description": description} if description else None,
        )
        return feature

    def _unmerged_count(self, wt_path: Path, base: str) -> int | None:
        """Sandbox commits not reachable from `base`.

        None when base can't be resolved — treated as at-risk, because "I could
        not check" must never read as "there is nothing here".
        """
        result = git.run_git(
            "rev-list", "--count", f"{base}..HEAD", cwd=wt_path, check=False
        )
        if result.returncode != 0:
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def at_risk_repos(self, feature_name: str) -> list[tuple[str, str]]:
        """Repos whose worktree holds work that removing it would orphan.

        Two ways to lose work: an uncommitted (or untracked) worktree, and
        sandbox commits that exist on no other branch. The base for that second
        check is where the sandbox branched from — the pinned fork base, else
        the repo's default branch. Note feature.branches[repo] is the *target*
        (publish) branch, which typically has no local ref; it is not the base.
        """
        feature = self.get(feature_name)
        at_risk: list[tuple[str, str]] = []
        for repo_name in feature.branches:
            if repo_name not in self.ws.repos:
                continue
            wt_path = self.ws.worktree_path(feature_name, repo_name)
            if not wt_path.exists():
                continue
            repo_info = self.ws.get_repo(repo_name)
            try:
                wt_repo = Repo.at_worktree(wt_path, repo_info)
            except Exception:
                continue
            reasons = []
            if wt_repo.is_dirty():
                reasons.append("uncommitted changes")
            base = feature.fork_base.get(repo_name) or repo_info.default_branch
            unmerged = self._unmerged_count(wt_path, base)
            if unmerged is None:
                reasons.append(f"merge status vs '{base}' unverifiable")
            elif unmerged:
                reasons.append(f"{unmerged} unmerged commit(s)")
            if reasons:
                at_risk.append((repo_name, " and ".join(reasons)))
        return at_risk

    def rescue_worktree(self, feature_name: str, repo_name: str) -> str | None:
        """Pin a worktree's full state to a gc-immune ref before destroying it.

        snapshot_commit() captures staged + unstaged + untracked via a temp
        index, and returns HEAD unchanged when the tree is clean — so this ref
        keeps unmerged sandbox commits reachable either way. Returns the ref.
        """
        wt_path = self.ws.worktree_path(feature_name, repo_name)
        if not wt_path.exists() or repo_name not in self.ws.repos:
            return None
        try:
            wt_repo = Repo.at_worktree(wt_path, self.ws.get_repo(repo_name))
            if wt_repo.rev_parse("HEAD") is None:
                return None
            sha = wt_repo.snapshot_commit(f"mgit rescue {feature_name}/{repo_name}")
            origin = Repo(self.ws.get_repo(repo_name), self.ws.root)
            ref = rescue_ref(feature_name, repo_name)
            origin.update_ref(ref, sha)
            return ref
        except Exception:
            return None

    def _guard_and_rescue(
        self, feature_name: str, force: bool, only_repo: str | None = None
    ) -> StrList:
        """Refuse to destroy at-risk worktrees unless forced; always rescue first."""
        risky = self.at_risk_repos(feature_name)
        if only_repo is not None:
            risky = [(r, why) for r, why in risky if r == only_repo]
        if risky and not force:
            detail = "; ".join(f"{r} ({why})" for r, why in risky)
            raise MgitError(
                f"Refusing to destroy work in feature '{feature_name}': {detail}. "
                f"Commit or push it, or re-run with --force — mgit pins a rescue "
                f"snapshot before removing either way."
            )
        rescued = []
        for repo_name, _ in risky:
            ref = self.rescue_worktree(feature_name, repo_name)
            if ref:
                rescued.append(f"{repo_name}: {ref}")
        return rescued

    def delete(self, name: str, force: bool = False) -> StrList:
        """Delete a feature definition and clean up its worktrees.

        Returns the rescue refs pinned for any worktree that held work.
        """
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")

        rescued = self._guard_and_rescue(name, force)

        # Remove worktrees for all enrolled repos
        feature = self.get(name)
        for repo_name in feature.branches:
            wt_path = self.ws.worktree_path(name, repo_name)
            if wt_path.exists():
                try:
                    repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                    repo.remove_worktree(wt_path)
                except Exception:
                    # If git worktree remove fails, clean up manually
                    shutil.rmtree(wt_path, ignore_errors=True)

        # Delete the sandbox branch in each repo — a later feature with the
        # same name must start fresh, not resurrect this feature's commits
        sb = sandbox_branch(name)
        for repo_name in feature.branches:
            if repo_name not in self.ws.repos:
                continue
            try:
                repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                git.run_git("worktree", "prune", cwd=repo.path, check=False)
                git.run_git("branch", "-D", sb, cwd=repo.path, check=False)
            except Exception:
                continue

        # Clean up feature's worktree directory (includes generated briefs)
        feature_wt_dir = self.ws.worktrees_dir / name
        if feature_wt_dir.exists():
            shutil.rmtree(feature_wt_dir, ignore_errors=True)

        # Clean up the memory sidecar directory
        mem_dir = memory.memory_dir(self.ws, name)
        if mem_dir.exists():
            shutil.rmtree(mem_dir, ignore_errors=True)

        # Clean up checkpoint manifests and pinning refs (incl. WIP refs)
        from mgit.core.checkpoint import CheckpointManager
        CheckpointManager(self.ws).delete_all_for_feature(name, list(feature.branches))

        # Delete feature file
        path.unlink()

        # Clear active if this was the active feature
        if self.ws.get_active_feature() == name:
            self.ws.clear_active_feature()

        return rescued

    def get(self, name: str) -> FeatureInfo:
        """Load a feature by name."""
        path = self._feature_path(name)
        if not path.exists():
            raise FeatureNotFoundError(f"Feature '{name}' not found")
        data = config.read_toml(path)
        return config.dict_to_feature(data)

    def list(self) -> list[FeatureInfo]:
        """List all features."""
        features: list[FeatureInfo] = []
        if not self.ws.features_dir.exists():
            return features
        for path in sorted(self.ws.features_dir.glob("*.toml")):
            if not path.is_file():
                continue
            data = config.read_toml(path)
            features.append(config.dict_to_feature(data))
        return features

    def remove_repo(
        self, feature_name: str, repo_name: str, force: bool = False
    ) -> tuple[FeatureInfo, StrList]:
        """Remove a repo from a feature and clean up its worktree.

        Despite the name this is destructive: it force-removes the worktree and
        force-deletes the sandbox branch. Guarded like delete(); returns the
        feature and any rescue refs pinned.
        """
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not in feature '{feature_name}'"
            )

        rescued = self._guard_and_rescue(feature_name, force, only_repo=repo_name)

        # Remove worktree
        wt_path = self.ws.worktree_path(feature_name, repo_name)
        if wt_path.exists():
            try:
                repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                repo.remove_worktree(wt_path)
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)

        del feature.branches[repo_name]
        feature.fork_base.pop(repo_name, None)
        feature.wip_from.pop(repo_name, None)
        feature.prs.pop(repo_name, None)
        self._save_feature(feature)

        # Prune this repo's sandbox branch and checkpoint/WIP pinning refs
        try:
            repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
            git.run_git("worktree", "prune", cwd=repo.path, check=False)
            git.run_git("branch", "-D", sandbox_branch(feature_name),
                        cwd=repo.path, check=False)
            for ref in repo.list_refs(f"refs/mgit/checkpoint/{feature_name}/"):
                repo.delete_ref(ref)
            for ref in repo.list_refs(f"refs/mgit/wip/{feature_name}/"):
                repo.delete_ref(ref)
        except Exception:
            pass
        memory.append_event(
            self.ws, feature_name, f"Removed repo '{repo_name}'",
            event="repo_removed", repo=repo_name,
        )
        self._regen_brief(feature)
        return feature, rescued

    def run_setup(self, repo_name: str, cwd: Path) -> tuple[bool, str]:
        """Run the configured setup command for a repo.

        Args:
            repo_name: Name of the repo.
            cwd: Directory to run the command in.

        Returns:
            Tuple of (success, output). Returns (True, "") if no setup configured.
        """
        repo_info = self.ws.get_repo(repo_name)
        if not repo_info.setup:
            return True, ""

        try:
            result = subprocess.run(
                repo_info.setup,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = result.stdout + result.stderr
            return result.returncode == 0, output.strip()
        except subprocess.TimeoutExpired:
            return False, "Setup command timed out (>300s)"

    def start(
        self,
        feature_name: str,
        repo_names: StrList | None = None,
        target_branch: str | None = None,
        description: str = "",
        materialize: bool = False,
        run_setup: bool = True,
        carry_repos: set[str] | None = None,
        activate: bool = True,
    ) -> tuple[FeatureInfo, StrList]:
        """Start working on a feature: create if needed, enroll repos.

        Args:
            feature_name: Name of the feature.
            repo_names: List of repo names to enroll. None = enroll all workspace repos.
            target_branch: Remote target branch (default = feature name).
            description: Description (only used on initial creation).
            materialize: If True, create worktrees for newly enrolled repos
                immediately.
            run_setup: If True, run configured setup commands after materialization.
            carry_repos: Set of repo names to carry dirty changes for.
                When None (default), auto-detects dirty repos and carries all.
                When provided, only repos in this set are carried.

        Returns:
            Tuple of (feature, list_of_newly_added_repo_names).
        """
        target = target_branch or feature_name

        # Default to all workspace repos
        names = repo_names if repo_names is not None else list(self.ws.repos.keys())

        # Validate BEFORE creating anything — a typo'd repo name must not
        # leave a half-created feature behind
        validate_feature_name(feature_name)
        for repo_name in names:
            if repo_name not in self.ws.repos:
                raise RepoNotFoundError(
                    f"Repo '{repo_name}' not found in workspace"
                )

        # Get or create feature
        try:
            feature = self.get(feature_name)
        except FeatureNotFoundError:
            feature = self.create(feature_name, description=description)

        newly_added: list[str] = []

        for repo_name in names:
            # Enroll in feature if not already
            if repo_name not in feature.branches:
                feature.branches[repo_name] = target
                newly_added.append(repo_name)

        # Save feature state
        self._save_feature(feature)

        for repo_name in newly_added:
            memory.append_event(
                self.ws, feature_name, f"Enrolled repo '{repo_name}'",
                event="repo_enrolled", repo=repo_name,
                meta={"target_branch": target},
            )

        # Set as active feature (skippable so parallel sessions can start
        # features without flipping the shared pointer under each other)
        if activate:
            self.ws.set_active_feature(feature_name)

        # Materialize worktrees for newly enrolled repos if requested
        if materialize and newly_added:
            if carry_repos is None:
                # Auto-detect: carry all dirty repos (backwards compat)
                carry_repos = {
                    name for name, _ in find_dirty_repos(self.ws, newly_added)
                }
            for repo_name in newly_added:
                wt_path = self.materialize(feature_name, repo_name, carry=repo_name in carry_repos)
                if run_setup:
                    self.run_setup(repo_name, wt_path)

        self._regen_brief(feature)
        return feature, newly_added

    def materialize(self, feature_name: str, repo_name: str, carry: bool = False) -> Path:
        """Materialize a single repo's worktree for an existing feature.

        Args:
            feature_name: Name of the feature.
            repo_name: Name of the repo to materialize.
            carry: If True, stash uncommitted changes from original repo
                and pop them into the new worktree.

        Returns:
            Path to the worktree directory.

        Raises:
            FeatureNotFoundError: If feature doesn't exist.
            RepoNotFoundError: If repo is not enrolled in the feature.
        """
        feature = self.get(feature_name)
        if repo_name not in feature.branches:
            raise RepoNotFoundError(
                f"Repo '{repo_name}' not enrolled in feature '{feature_name}'"
            )

        wt_path = self.ws.worktree_path(feature_name, repo_name)

        # Idempotent: if already materialized, return existing path
        if wt_path.exists():
            return wt_path

        sb = sandbox_branch(feature_name)
        repo = Repo(self.ws.get_repo(repo_name), self.ws.root)

        # Stash changes before creating worktree if carrying
        stashed = False
        if carry:
            stashed = repo.stash_push(f"mgit-carry:{feature_name}")

        # Forked features branch from the SHA pinned at fork time
        start_point = feature.fork_base.get(repo_name)

        try:
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            repo.add_worktree(wt_path, sb, start_point=start_point)
        except BaseException as e:
            if stashed:
                # Put the user's changes back where they were; if even that
                # fails, at least say where they went
                try:
                    repo.stash_pop()
                except Exception:
                    raise MgitError(
                        f"Worktree creation failed and restoring your changes "
                        f"also failed — they are in '{repo_name}' stash "
                        f"'mgit-carry:{feature_name}': {e}"
                    ) from e
            raise

        # Pop stash in the worktree if we stashed something
        if stashed:
            git.run_git("stash", "pop", cwd=wt_path)

        # Re-materialize parent WIP carried by fork --carry-wip
        wip_sha = feature.wip_from.get(repo_name)
        if wip_sha:
            self._apply_wip_snapshot(wt_path, wip_sha)

        meta: dict = {"carried": bool(stashed)}
        if start_point:
            meta["from"] = start_point[:12]
        memory.append_event(
            self.ws, feature_name, f"Materialized worktree for '{repo_name}'",
            event="materialized", repo=repo_name, meta=meta,
        )

        self._regen_brief(feature)
        return wt_path

    def _apply_wip_snapshot(self, wt_path: Path, snapshot_sha: str) -> None:
        """Re-materialize a WIP snapshot commit as dirty files in a fresh worktree.

        The worktree branch starts at the snapshot's parent commit, so moving
        the tree to the snapshot and the branch back is conflict-free.
        """
        head = git.run_git("rev-parse", "HEAD", cwd=wt_path).stdout.strip()
        git.run_git("reset", "--hard", snapshot_sha, cwd=wt_path)
        git.run_git("reset", head, cwd=wt_path)

    def sync(
        self,
        feature_name: str,
        run_setup: bool = True,
        carry_repos: set[str] | None = None,
    ) -> StrList:
        """Discover dirty repos and enroll + materialize them into the feature.

        Scans all workspace repos for uncommitted changes, enrolls any that
        aren't already part of the feature, and materializes worktrees.

        Args:
            feature_name: Name of the feature to sync into.
            run_setup: If True, run configured setup commands after materialization.
            carry_repos: Set of repo names to carry dirty changes for.
                When None (default), carries all dirty repos (current behavior).
                When provided, only repos in this set are carried.

        Returns:
            List of newly synced repo names.
        """
        feature = self.get(feature_name)

        all_repo_names = list(self.ws.repos.keys())
        dirty = find_dirty_repos(self.ws, all_repo_names)

        # Filter out repos already enrolled in the feature
        new_dirty = [(name, branch) for name, branch in dirty if name not in feature.branches]

        # Newly synced repos inherit the feature's existing target branch when
        # it's uniform (a feature started with --branch X must not silently
        # enroll sync'd repos targeting the feature name instead of X)
        existing_targets = set(feature.branches.values())
        inherited = existing_targets.pop() if len(existing_targets) == 1 else None

        synced: list[str] = []
        for repo_name, _ in new_dirty:
            # activate=False: sync is enrollment maintenance, not a context
            # switch — it must not move the shared active pointer under
            # other sessions
            self.start(feature_name, [repo_name], target_branch=inherited,
                       run_setup=False, activate=False)
            do_carry = carry_repos is None or repo_name in carry_repos
            wt_path = self.materialize(feature_name, repo_name, carry=do_carry)
            if run_setup:
                self.run_setup(repo_name, wt_path)
            synced.append(repo_name)

        if synced:
            memory.append_event(
                self.ws, feature_name,
                f"Synced {len(synced)} dirty repo(s): {', '.join(synced)}",
                event="synced", meta={"repos": synced},
            )
            self._regen_brief(self.get(feature_name))

        return synced

    def record_prs(self, feature_name: str, prs: dict[str, str]) -> FeatureInfo:
        """Persist published PR/MR URLs (repo -> url) into the feature file."""
        feature = self.get(feature_name)
        feature.prs.update(prs)
        self._save_feature(feature)
        return feature

    def is_materialized(self, feature_name: str, repo_name: str) -> bool:
        """Check if a repo's worktree has been materialized for a feature."""
        return self.ws.worktree_path(feature_name, repo_name).exists()

    def switch(self, name: str) -> dict[str, Path]:
        """Set the active feature. Worktrees are always ready.

        Args:
            name: Feature name to switch to.

        Returns:
            Dict of repo_name -> worktree_path for each enrolled repo.
        """
        feature = self.get(name)

        # Set as active feature
        self.ws.set_active_feature(name)

        memory.append_event(
            self.ws, name, f"Switched to feature '{name}'", event="switched",
        )
        self._regen_brief(feature)

        return self.get_worktree_paths(name)

    def fork(
        self,
        child_name: str,
        parent_name: str,
        *,
        target_branch: str | None = None,
        description: str = "",
        materialize: bool = False,
        run_setup: bool = True,
        carry_wip: bool = False,
        activate: bool = True,
    ) -> FeatureInfo:
        """Fork a feature: new child with pinned per-repo base SHAs and inherited memory.

        The child enrolls the same repos as the parent. Each repo's base is
        pinned to the parent's sandbox-branch tip (or the local default branch
        tip if the parent was never materialized), so lazy child worktrees
        branch from the fork point even after the parent advances.

        Args:
            child_name: Name of the new feature.
            parent_name: Feature to fork from.
            target_branch: Child's remote target branch (default: child name).
            description: Child description (defaults to parent's).
            materialize: If True, materialize all child worktrees immediately.
            run_setup: If True, run setup commands after materialization.
            carry_wip: If True, snapshot the parent's dirty worktrees and
                re-materialize the WIP in the child on materialize.

        Returns:
            The new child FeatureInfo.
        """
        parent = self.get(parent_name)
        validate_feature_name(child_name)
        if self._feature_path(child_name).exists():
            raise FeatureExistsError(f"Feature '{child_name}' already exists")

        target = target_branch or child_name
        fork_base: dict[str, str] = {}
        wip_from: dict[str, str] = {}
        parent_sb = sandbox_branch(parent_name)

        enrollable = [r for r in parent.branches if r in self.ws.repos]
        for repo_name in enrollable:
            repo_path = self.ws.repo_path(repo_name)
            sha = None
            for ref in (f"refs/heads/{parent_sb}",
                        f"refs/heads/{self.ws.get_repo(repo_name).default_branch}",
                        "HEAD"):
                result = git.run_git("rev-parse", "--verify", "--quiet", ref,
                                     cwd=repo_path, check=False)
                if result.returncode == 0:
                    sha = result.stdout.strip()
                    break
            if sha:
                fork_base[repo_name] = sha

            if carry_wip and self.is_materialized(parent_name, repo_name):
                wt_path = self.ws.worktree_path(parent_name, repo_name)
                repo = Repo.at_worktree(wt_path, self.ws.get_repo(repo_name))
                if repo.is_dirty():
                    snap = repo.snapshot_commit(f"mgit-wip: fork {child_name}")
                    if snap and snap != fork_base.get(repo_name):
                        wip_from[repo_name] = snap
                        repo.update_ref(f"refs/mgit/wip/{child_name}/{repo_name}", snap)

        child = self.create(child_name, description=description or parent.description)
        child.parent = parent_name
        child.branches = {repo_name: target for repo_name in enrollable}
        child.fork_base = fork_base
        child.wip_from = wip_from
        self._save_feature(child)

        # Seed child memory from the parent's plan file
        parent_state = memory.load_state(self.ws, parent_name)
        memory.save_state(self.ws, child_name, parent_state)
        memory.append_event(
            self.ws, child_name, f"Forked from '{parent_name}'",
            event="branched_from",
            meta={"parent": parent_name, "fork_base": fork_base},
        )
        memory.append_event(
            self.ws, parent_name, f"Forked into '{child_name}'",
            event="forked", meta={"child": child_name},
        )

        if activate:
            self.ws.set_active_feature(child_name)

        if materialize:
            for repo_name in child.branches:
                wt_path = self.materialize(child_name, repo_name)
                if run_setup:
                    self.run_setup(repo_name, wt_path)

        self._regen_brief(child)
        return child

    def get_worktree_paths(self, feature_name: str) -> dict[str, Path]:
        """Get worktree paths for each enrolled repo in a feature.

        Returns:
            Dict of repo_name -> worktree_path.
        """
        feature = self.get(feature_name)
        paths = {}
        for repo_name in feature.branches:
            paths[repo_name] = self.ws.worktree_path(feature_name, repo_name)
        return paths
