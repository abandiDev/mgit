"""CLI commands: mgit checkpoint {save,restore,list,show,delete}."""

import click

from mgit.core.checkpoint import CheckpointManager
from mgit.core.feature import FeatureManager
from mgit.core.workspace import Workspace
from mgit.utils import output
from mgit.utils.errors import MgitError


def _feature_or_active(ws, feature_name):
    if feature_name and feature_name != ".":
        return feature_name
    resolved = ws.resolve_feature(feature_name)
    if not resolved:
        raise MgitError(
            "No feature in scope. Pass -f <name>, export MGIT_FEATURE, "
            "run inside a feature worktree, or use 'mgit feature start'."
        )
    return resolved


def _cp_to_dict(cp) -> dict:
    return {
        "id": cp.id,
        "feature": cp.feature,
        "label": cp.label,
        "created_at": cp.created_at,
        "repos": {
            name: {
                "head": st.head,
                "snapshot": st.snapshot,
                "branch": st.branch,
                "dirty": st.dirty,
            }
            for name, st in cp.repos.items()
        },
    }


@click.group()
def checkpoint():
    """Cross-repo checkpoints: save/restore a whole feature (code + memory)."""


@checkpoint.command("save")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active, '.' also means active).")
@click.option("--label", default="", help="Human label for this checkpoint.")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def save(feature_name, label, as_json):
    """Save a checkpoint: pin HEAD per materialized repo; snapshot dirty trees
    non-destructively (working trees are never touched)."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)

    cp = CheckpointManager(ws).save(fm, name, label=label)

    if as_json:
        output.emit("checkpoint.save", _cp_to_dict(cp))
        return
    if not cp.repos:
        click.echo(f"Checkpoint {cp.id} saved for '{name}' (no materialized repos).")
        return
    click.echo(f"Checkpoint {cp.id} saved for '{name}'" +
               (f" — {cp.label}" if cp.label else "") + ":")
    for repo_name, st in cp.repos.items():
        wip = " +wip" if st.dirty else ""
        click.echo(f"  {repo_name} @ {st.head[:12]}{wip}")


@checkpoint.command("restore")
@click.argument("cp_id")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def restore(cp_id, feature_name, as_json):
    """Restore a checkpoint across all its repos.

    A safety checkpoint of the current state is always saved first.
    WIP captured at save time re-materializes as uncommitted changes.
    """
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)

    cp, safety, untouched = CheckpointManager(ws).restore(fm, name, cp_id)

    if as_json:
        output.emit("checkpoint.restore", {
            "restored": _cp_to_dict(cp),
            "safety_checkpoint": safety.id,
            "untouched_repos": untouched,
        })
        return
    click.echo(f"Restored checkpoint {cp.id} for '{name}' "
               f"(safety backup: {safety.id}).")
    for repo_name, st in cp.repos.items():
        wip = " +wip re-materialized" if st.dirty else ""
        click.echo(f"  {repo_name} -> {st.head[:12]}{wip}")
    if untouched:
        click.secho(
            f"  Not covered by this checkpoint (left untouched): {', '.join(untouched)}",
            fg="yellow",
        )


@checkpoint.command("list")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def list_checkpoints(feature_name, as_json):
    """List checkpoints for a feature."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    name = _feature_or_active(ws, feature_name)

    checkpoints = CheckpointManager(ws).list(name)

    if as_json:
        output.emit("checkpoint.list", {
            "feature": name,
            "checkpoints": [_cp_to_dict(c) for c in checkpoints],
        })
        return
    if not checkpoints:
        click.echo(f"No checkpoints for '{name}'.")
        return
    for cp in checkpoints:
        dirty = sum(1 for st in cp.repos.values() if st.dirty)
        label = f" — {cp.label}" if cp.label else ""
        click.echo(f"{cp.id}  {cp.created_at}  "
                   f"[{len(cp.repos)} repos, {dirty} with wip]{label}")


@checkpoint.command("show")
@click.argument("cp_id")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def show(cp_id, feature_name, as_json):
    """Show one checkpoint's full manifest."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    name = _feature_or_active(ws, feature_name)

    cp = CheckpointManager(ws).get(name, cp_id)

    if as_json:
        output.emit("checkpoint.show", _cp_to_dict(cp))
        return
    click.echo(f"Checkpoint {cp.id} for '{cp.feature}'")
    if cp.label:
        click.echo(f"Label: {cp.label}")
    click.echo(f"Created: {cp.created_at}")
    click.echo("Repos:")
    for repo_name, st in cp.repos.items():
        click.echo(f"  {repo_name:<20} head {st.head[:12]}  "
                   f"snapshot {st.snapshot[:12]}  {st.branch}"
                   f"{'  (dirty)' if st.dirty else ''}")


@checkpoint.command("delete")
@click.argument("cp_id")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
def delete(cp_id, feature_name):
    """Delete a checkpoint (unpins its refs so snapshots become gc-able)."""
    ws = Workspace.find()
    name = _feature_or_active(ws, feature_name)
    CheckpointManager(ws).delete(name, cp_id)
    click.echo(f"Deleted checkpoint {cp_id} of '{name}'.")
