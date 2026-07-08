"""CheckpointManager: non-destructive cross-repo save-points for a feature.

A checkpoint pins, per materialized repo, the branch tip (head) and — when
the tree was dirty — a WIP snapshot commit created via a temporary index, so
the working tree and real index are never touched by a save. Snapshot commits
are anchored by refs/mgit/checkpoint/<feature>/<cp-id> in each repo's shared
ref store: visible from all worktrees, immune to gc, never pushed (bulk push
uses explicit refspecs).

Manifests live at .mgit/checkpoints/<feature>/cp-NNNN.toml with a sibling
cp-NNNN.memory.toml copy so working memory rewinds together with code.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mgit.core import config, memory
from mgit.core.repo import Repo
from mgit.core.workspace import Workspace
from mgit.models.types import CheckpointInfo, CheckpointRepoState
from mgit.utils.errors import MgitError

CHECKPOINT_REF_PREFIX = "refs/mgit/checkpoint"


def checkpoint_ref(feature_name: str, cp_id: str) -> str:
    return f"{CHECKPOINT_REF_PREFIX}/{feature_name}/{cp_id}"


class CheckpointManager:
    """Manages checkpoint lifecycle for features in a workspace."""

    def __init__(self, workspace: Workspace):
        self.ws = workspace

    def _dir(self, feature_name: str) -> Path:
        return self.ws.checkpoints_dir / feature_name

    def _manifest_path(self, feature_name: str, cp_id: str) -> Path:
        return self._dir(feature_name) / f"{cp_id}.toml"

    def _memory_copy_path(self, feature_name: str, cp_id: str) -> Path:
        return self._dir(feature_name) / f"{cp_id}.memory.toml"

    def _alloc_id(self, feature_name: str) -> str:
        """Allocate the next checkpoint id race-safely via O_CREAT|O_EXCL."""
        d = self._dir(feature_name)
        d.mkdir(parents=True, exist_ok=True)
        n = 1
        while True:
            cp_id = f"cp-{n:04d}"
            try:
                fd = os.open(d / f"{cp_id}.toml", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return cp_id
            except FileExistsError:
                n += 1

    def save(self, fm, feature_name: str, label: str = "") -> CheckpointInfo:
        """Checkpoint every materialized repo of a feature (non-destructive)."""
        feature = fm.get(feature_name)
        cp_id = self._alloc_id(feature_name)
        try:
            repos: dict[str, CheckpointRepoState] = {}
            for repo_name in feature.branches:
                if not fm.is_materialized(feature_name, repo_name):
                    continue
                wt_path = self.ws.worktree_path(feature_name, repo_name)
                wt_repo = Repo.at_worktree(wt_path, self.ws.get_repo(repo_name))
                head = wt_repo.rev_parse("HEAD")
                if head is None:
                    continue
                dirty = wt_repo.is_dirty()
                snapshot = head
                if dirty:
                    snapshot = wt_repo.snapshot_commit(
                        f"mgit checkpoint {feature_name}/{cp_id}"
                    )
                repos[repo_name] = CheckpointRepoState(
                    head=head,
                    snapshot=snapshot,
                    branch=wt_repo.current_branch(),
                    dirty=dirty,
                )
                # Anchor in the origin repo's shared ref store (gc-immune,
                # survives worktree removal)
                origin = Repo(self.ws.get_repo(repo_name), self.ws.root)
                origin.update_ref(checkpoint_ref(feature_name, cp_id), snapshot)

            info = CheckpointInfo(
                id=cp_id,
                feature=feature_name,
                label=label,
                created_at=memory.utc_now(),
                repos=repos,
            )
            config.write_toml(
                self._manifest_path(feature_name, cp_id),
                config.checkpoint_to_dict(info),
            )

            # Snapshot the working memory alongside the code
            state_file = memory.state_path(self.ws, feature_name)
            if state_file.exists():
                shutil.copyfile(state_file, self._memory_copy_path(feature_name, cp_id))

            text = f"Checkpoint {cp_id} saved" + (f": {label}" if label else "")
            memory.append_event(
                self.ws, feature_name, text, event="checkpoint",
                meta={"cp_id": cp_id, "repos": sorted(repos)},
            )
            return info
        except BaseException:
            # Don't leave an empty placeholder manifest behind
            self._manifest_path(feature_name, cp_id).unlink(missing_ok=True)
            raise

    def get(self, feature_name: str, cp_id: str) -> CheckpointInfo:
        path = self._manifest_path(feature_name, cp_id)
        if not path.exists():
            raise MgitError(
                f"Checkpoint '{cp_id}' not found for feature '{feature_name}'"
            )
        return config.dict_to_checkpoint(config.read_toml(path))

    def list(self, feature_name: str) -> list[CheckpointInfo]:
        """List checkpoints (oldest first); unparsable manifests are skipped."""
        d = self._dir(feature_name)
        if not d.exists():
            return []
        checkpoints = []
        for path in sorted(d.glob("cp-*.toml")):
            if path.name.endswith(".memory.toml"):
                continue
            try:
                checkpoints.append(config.dict_to_checkpoint(config.read_toml(path)))
            except Exception:
                continue
        return checkpoints

    def restore(
        self, fm, feature_name: str, cp_id: str
    ) -> tuple[CheckpointInfo, CheckpointInfo, list[str]]:
        """Restore a feature to a checkpoint.

        ALWAYS saves a safety checkpoint of the current state first — the
        destructive path (clean -fd + reset --hard) is unreachable without a
        way back.

        Returns (restored_checkpoint, safety_checkpoint, untouched_repos)
        where untouched_repos are materialized repos the checkpoint doesn't
        cover (materialized after it was saved).
        """
        info = self.get(feature_name, cp_id)
        feature = fm.get(feature_name)

        safety = self.save(
            fm, feature_name, label=f"auto-backup before restore of {cp_id}"
        )

        for repo_name, st in info.repos.items():
            if repo_name not in feature.branches:
                continue
            if not fm.is_materialized(feature_name, repo_name):
                # Re-materialize so the restore can land
                fm.materialize(feature_name, repo_name)
            wt_path = self.ws.worktree_path(feature_name, repo_name)
            wt_repo = Repo.at_worktree(wt_path, self.ws.get_repo(repo_name))
            wt_repo.restore_to(st.head, st.snapshot)

        # Rewind the working memory to the checkpointed copy
        mem_copy = self._memory_copy_path(feature_name, cp_id)
        if mem_copy.exists():
            memory.memory_dir(self.ws, feature_name).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(mem_copy, memory.state_path(self.ws, feature_name))

        untouched = [
            r for r in feature.branches
            if fm.is_materialized(feature_name, r) and r not in info.repos
        ]

        memory.append_event(
            self.ws, feature_name,
            f"Restored checkpoint {cp_id} (safety backup: {safety.id})",
            event="restored",
            meta={"cp_id": cp_id, "safety_cp_id": safety.id,
                  "untouched": untouched},
        )
        return info, safety, untouched

    def delete(self, feature_name: str, cp_id: str) -> None:
        """Delete a checkpoint manifest and unpin its refs (snapshots become gc-able)."""
        info = self.get(feature_name, cp_id)
        for repo_name in info.repos:
            if repo_name not in self.ws.repos:
                continue
            try:
                repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                repo.delete_ref(checkpoint_ref(feature_name, cp_id))
            except Exception:
                continue
        self._manifest_path(feature_name, cp_id).unlink(missing_ok=True)
        self._memory_copy_path(feature_name, cp_id).unlink(missing_ok=True)

    def delete_all_for_feature(self, feature_name: str, repo_names: list[str]) -> None:
        """Feature-delete cleanup: remove all manifests and per-repo refs."""
        for repo_name in repo_names:
            if repo_name not in self.ws.repos:
                continue
            try:
                repo = Repo(self.ws.get_repo(repo_name), self.ws.root)
                for ref in repo.list_refs(f"{CHECKPOINT_REF_PREFIX}/{feature_name}/"):
                    repo.delete_ref(ref)
                for ref in repo.list_refs(f"refs/mgit/wip/{feature_name}/"):
                    repo.delete_ref(ref)
            except Exception:
                continue
        d = self._dir(feature_name)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
