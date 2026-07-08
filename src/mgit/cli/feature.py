"""CLI commands: mgit feature {start,materialize,switch,activate,deactivate,show,list,delete,remove-repo,brief,note,plan,log,fork,tree}."""

import sys

import click

from mgit.core import memory
from mgit.core.brief import write_feature_brief
from mgit.core.feature import (
    FeatureManager,
    find_dirty_repos,
    sandbox_branch,
)
from mgit.core.workspace import Workspace
from mgit.utils import output
from mgit.utils.errors import MgitError


def _print_setup_status(ws, fm, repo_name, wt_path):
    """Print setup command result for a repo if configured."""
    repo_info = ws.get_repo(repo_name)
    if not repo_info.setup:
        return
    click.echo(f"    Running setup: {repo_info.setup}")
    ok, output = fm.run_setup(repo_name, wt_path)
    if ok:
        click.echo(f"    Setup complete.")
    else:
        click.secho(f"    Setup failed: {output}", fg="yellow", err=True)


def _require_active(ws) -> str:
    """Return the active feature name or raise a domain error."""
    active = ws.get_active_feature()
    if not active:
        raise MgitError("No active feature. Use 'mgit feature start' first.")
    return active


def _feature_or_active(ws, feature_name: str | None) -> str:
    """Resolve -f value ('.'/None mean the active feature)."""
    if feature_name and feature_name != ".":
        return feature_name
    return _require_active(ws)


def _confirm_carry(dirty: list[tuple[str, str]]) -> set[str]:
    """Prompt per dirty repo whether to carry changes.

    Headless sessions (no TTY) can't answer prompts — refuse with a clear
    instruction instead of hanging the calling agent.
    """
    if not dirty:
        return set()
    if not sys.stdin.isatty():
        names = ", ".join(name for name, _ in dirty)
        raise MgitError(
            f"Uncommitted changes in: {names}. "
            "Non-interactive session — pass --carry or --no-carry."
        )
    carry: set[str] = set()
    for name, br in dirty:
        if click.confirm(
            f"  {name} has uncommitted changes (on {br}). "
            f"Carry to feature worktree?",
            default=False,
        ):
            carry.add(name)
    return carry


@click.group()
def feature():
    """Manage cross-repo features."""


@feature.command("start")
@click.argument("feature_name")
@click.option(
    "--repo", "-r", "repo_names", multiple=True,
    help="Repo name (can be repeated). Omit to enroll all repos.",
)
@click.option("--branch", default=None, help="Remote target branch (default: feature name).")
@click.option("--description", "-d", default="", help="Feature description (only on creation).")
@click.option("--materialize", "-m", is_flag=True, default=False,
              help="Materialize worktrees immediately.")
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to worktrees (default: prompt per repo).")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def start(feature_name, repo_names, branch, description, materialize, carry, no_setup):
    """Start a feature: create/enroll repos.

    If no -r is given, enrolls all workspace repos.
    Pass --materialize to create worktrees immediately.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    repo_list = list(repo_names) if repo_names else None

    # Resolve carry_repos when materializing
    carry_repos = None
    if materialize:
        names_to_check = repo_list if repo_list is not None else list(ws.repos.keys())
        if carry is True:
            # --carry: carry all dirty repos without prompting
            carry_repos = {
                name for name, _ in find_dirty_repos(ws, names_to_check)
            }
        elif carry is False:
            # --no-carry: skip carry for all
            carry_repos = set()
        else:
            # Neither flag: prompt per dirty repo (refuses when headless)
            carry_repos = _confirm_carry(find_dirty_repos(ws, names_to_check))

    feat, newly_added = fm.start(
        feature_name, repo_list,
        target_branch=branch,
        description=description,
        materialize=materialize,
        run_setup=not no_setup,
        carry_repos=carry_repos,
    )

    total = len(feat.branches)
    new_count = len(newly_added)

    click.echo(f"Feature '{feature_name}': {new_count} newly enrolled, {total} total.")
    for repo_name in newly_added:
        if materialize:
            wt_path = ws.worktree_path(feature_name, repo_name)
            carried = carry_repos is not None and repo_name in carry_repos
            suffix = " (changes carried)" if carried else ""
            click.echo(f"  + {repo_name}: {wt_path}/{suffix}")
            if not no_setup:
                _print_setup_status(ws, fm, repo_name, wt_path)
        else:
            click.echo(f"  + {repo_name}")
    click.echo(f"Active feature: {feature_name}")
    if not materialize:
        click.echo()
        click.echo("Use 'mgit feature materialize <repo>' to materialize a worktree.")


def _materialize_handler(repo_name, carry, no_setup):
    """Shared handler for materialize and work commands."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active = _require_active(ws)

    # Auto-detect repo from cwd if not specified
    if not repo_name:
        detected = ws.detect_repo_from_cwd()
        if detected:
            repo_name = detected
        else:
            raise MgitError(
                "No repo specified and cwd is not inside a registered repo."
            )

    # Check if already materialized (setup only runs for newly created worktrees)
    already_materialized = fm.is_materialized(active, repo_name)

    # Detect dirty repo and resolve carry behavior
    do_carry = False
    if carry is not False:
        dirty = find_dirty_repos(ws, [repo_name])
        if dirty:
            if carry is None:
                # No flag — prompt (refuses when headless)
                do_carry = bool(_confirm_carry(dirty))
            else:
                do_carry = True  # --carry flag

    wt_path = fm.materialize(active, repo_name, carry=do_carry)

    suffix = " (changes carried)" if do_carry else ""
    click.echo(f"  {repo_name}: materialized -> {wt_path}/{suffix}")

    # Run setup only for newly created worktrees
    if not already_materialized and not no_setup:
        _print_setup_status(ws, fm, repo_name, wt_path)


@feature.command("materialize")
@click.argument("repo_name", required=False)
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to the feature worktree.")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def materialize_cmd(repo_name, carry, no_setup):
    """Materialize a worktree for a repo in the active feature.

    If REPO_NAME is omitted, auto-detects from current directory.
    """
    _materialize_handler(repo_name, carry, no_setup)


@feature.command("work", hidden=True)
@click.argument("repo_name", required=False)
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to the feature worktree.")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def work(repo_name, carry, no_setup):
    """Materialize a worktree for a repo in the active feature (alias for materialize)."""
    _materialize_handler(repo_name, carry, no_setup)


@feature.command("sync")
@click.option("--carry/--no-carry", default=None,
              help="Carry uncommitted changes to worktrees (default: prompt per repo).")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def sync(carry, no_setup):
    """Discover dirty repos and enroll them into the active feature.

    Scans all workspace repos for uncommitted changes, enrolls any that
    aren't already part of the active feature, and materializes worktrees.
    """
    ws = Workspace.find()
    fm = FeatureManager(ws)

    active = _require_active(ws)

    # Resolve carry_repos
    feature = fm.get(active)
    all_repo_names = list(ws.repos.keys())
    dirty = find_dirty_repos(ws, all_repo_names)
    new_dirty = [(name, br) for name, br in dirty if name not in feature.branches]

    if carry is True:
        carry_repos = {name for name, _ in new_dirty}
    elif carry is False:
        carry_repos = set()
    else:
        # Neither flag: prompt per dirty repo (refuses when headless)
        carry_repos = _confirm_carry(new_dirty)

    synced = fm.sync(active, run_setup=not no_setup, carry_repos=carry_repos)

    if synced:
        click.echo(f"Synced {len(synced)} repo(s) into feature '{active}':")
        for name in synced:
            carried = carry_repos is not None and name in carry_repos
            suffix = " (changes carried)" if carried else ""
            click.echo(f"  + {name}{suffix}")
    else:
        click.echo("No new dirty repos to sync.")


@feature.command("switch")
@click.argument("name")
def switch(name):
    """Set the active feature. Worktrees are always ready — no branch switching needed."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    wt_paths = fm.switch(name)

    for repo_name, wt_path in wt_paths.items():
        click.echo(f"  {repo_name}: {wt_path}")
    click.echo(f"Active feature: {name}")


@feature.command("activate")
@click.argument("name")
def activate(name):
    """Set the active feature without switching branches."""
    ws = Workspace.find()
    fm = FeatureManager(ws)

    # Validate feature exists
    fm.get(name)

    ws.set_active_feature(name)
    click.echo(f"Active feature: {name}")


@feature.command("deactivate")
def deactivate():
    """Clear the active feature."""
    ws = Workspace.find()
    ws.clear_active_feature()
    click.echo("No active feature.")


@feature.command("show")
@click.argument("name")
def show(name):
    """Show details of a feature."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    f = fm.get(name)

    active = ws.get_active_feature()
    active_marker = " (active)" if active == name else ""

    click.echo(f"Feature: {f.name}{active_marker}")
    if f.description:
        click.echo(f"Description: {f.description}")
    click.echo(f"Sandbox branch: {sandbox_branch(name)}")
    click.echo("Repos:")
    if f.branches:
        for repo, target in f.branches.items():
            wt_path = ws.worktree_path(name, repo)
            mat = "*" if wt_path.exists() else " "
            click.echo(f"  {mat} {repo:<20} -> {target}  ({wt_path})")
    else:
        click.echo("  (none)")


@feature.command("list")
def list_features():
    """List all features."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    features = fm.list()
    active = ws.get_active_feature()

    if not features:
        click.echo("No features defined. Use 'mgit feature start' to create one.")
        return

    for f in features:
        marker = "* " if f.name == active else "  "
        repos = ", ".join(f.branches.keys()) if f.branches else "(no repos)"
        desc = f" - {f.description}" if f.description else ""
        click.echo(f"{marker}{f.name}{desc}  [{repos}]")


@feature.command("delete")
@click.argument("name")
def delete(name):
    """Delete a feature definition and clean up its worktrees."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    fm.delete(name)
    click.echo(f"Deleted feature '{name}'.")


@feature.command("remove-repo")
@click.argument("feature_name")
@click.argument("repo_name")
def remove_repo(feature_name, repo_name):
    """Remove a repo from a feature and clean up its worktree."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    fm.remove_repo(feature_name, repo_name)
    click.echo(f"Removed '{repo_name}' from feature '{feature_name}'.")


# --- Working memory ---


@feature.command("brief")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active, '.' also means active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
@click.option("--full", is_flag=True, help="Include the entire journal, not just the tail.")
@click.option("--refresh", is_flag=True,
              help="Also regenerate the worktree CLAUDE.md/AGENTS.md brief files.")
def brief(feature_name, as_json, full, refresh):
    """One-call re-orientation: working memory + journal tail + live git facts."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)

    if refresh:
        write_feature_brief(ws, feat)

    data = memory.build_brief(ws, feat, full=full)
    if as_json:
        output.emit("feature.brief", data)
    else:
        click.echo(memory.render_brief(data), nl=False)


@feature.command("note")
@click.argument("text")
@click.option("--type", "-t", "note_type",
              type=click.Choice(memory.NOTE_TYPES), default="note",
              help="Entry type (note, decision, question, handoff).")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
def note(text, note_type, feature_name):
    """Append an agent-authored entry to the feature journal."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)  # validate the feature exists

    memory.append_event(ws, name, text, type_=note_type, actor="agent")
    write_feature_brief(ws, feat)
    click.echo(f"Noted ({note_type}) in '{name}'.")


@feature.command("plan")
@click.option("--goal", default=None, help="Set the feature goal.")
@click.option("--status", default=None, help="Set the one-line status.")
@click.option("--next", "next_steps", multiple=True,
              help="Replace next steps (repeatable).")
@click.option("--add-next", "add_next", multiple=True,
              help="Append next steps (repeatable).")
@click.option("--done", "done_n", type=int, default=None,
              help="Mark next-step N (1-based) as done and remove it.")
@click.option("--ask", "asks", multiple=True, help="Add an open question (repeatable).")
@click.option("--resolve", "resolve_n", type=int, default=None,
              help="Resolve open question N (1-based) and remove it.")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def plan(goal, status, next_steps, add_next, done_n, asks, resolve_n,
         feature_name, as_json):
    """Read or update the structured plan (goal, status, next steps, questions).

    With no flags, prints the current plan.
    """
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)

    state = memory.load_state(ws, name)
    changed = []

    if goal is not None:
        state.goal = goal
        changed.append("goal")
    if status is not None:
        state.status = status
        changed.append("status")
    if next_steps:
        state.next_steps = list(next_steps)
        changed.append("next_steps")
    if add_next:
        state.next_steps.extend(add_next)
        changed.append("next_steps")
    if done_n is not None:
        if not 1 <= done_n <= len(state.next_steps):
            raise MgitError(
                f"--done {done_n}: no such next-step (1..{len(state.next_steps)})"
            )
        done_text = state.next_steps.pop(done_n - 1)
        memory.append_event(
            ws, name, f"Done: {done_text}", event="step_done", actor="agent",
        )
        changed.append("next_steps")
    if asks:
        state.open_questions.extend(asks)
        changed.append("open_questions")
    if resolve_n is not None:
        if not 1 <= resolve_n <= len(state.open_questions):
            raise MgitError(
                f"--resolve {resolve_n}: no such question (1..{len(state.open_questions)})"
            )
        resolved = state.open_questions.pop(resolve_n - 1)
        memory.append_event(
            ws, name, f"Resolved: {resolved}", event="question_resolved", actor="agent",
        )
        changed.append("open_questions")

    if changed:
        memory.save_state(ws, name, state)
        memory.append_event(
            ws, name, f"Plan updated ({', '.join(sorted(set(changed)))})",
            event="plan_updated", actor="agent",
        )
        write_feature_brief(ws, feat)

    plan_data = {
        "feature": name,
        "goal": state.goal,
        "status": state.status,
        "next_steps": list(state.next_steps),
        "open_questions": list(state.open_questions),
        "updated_at": state.updated_at,
    }
    if as_json:
        output.emit("feature.plan", plan_data)
        return
    age = memory.relative_age(state.updated_at)
    suffix = f" (updated {age})" if age else ""
    click.echo(f"Plan for '{name}'{suffix}:")
    click.echo(f"  Goal: {state.goal or '(not set)'}")
    click.echo(f"  Status: {state.status or '(not set)'}")
    for i, step in enumerate(state.next_steps, 1):
        click.echo(f"  Next {i}. {step}")
    for i, q in enumerate(state.open_questions, 1):
        click.echo(f"  Open {i}. {q}")


@feature.command("log")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("-n", "limit", type=int, default=20, help="Number of entries (default 20).")
@click.option("--type", "type_filter", default=None,
              help="Only entries of this type (note, decision, question, handoff, event).")
@click.option("--commits", is_flag=True,
              help="Interleave journal entries with sandbox-branch commits across repos.")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def log(feature_name, limit, type_filter, commits, as_json):
    """Read the feature journal (beyond the brief's tail)."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)

    entries, errors = memory.read_journal(ws, name, type_filter=type_filter)

    if commits:
        entries = entries + _feature_commits(ws, feat)
        entries.sort(key=lambda e: e.get("ts", ""))

    if limit and len(entries) > limit:
        entries = entries[-limit:]

    if as_json:
        output.emit("feature.log", {
            "feature": name, "entries": entries, "journal_errors": errors,
        })
        return

    if not entries:
        click.echo(f"No journal entries for '{name}'.")
    for e in entries:
        who = e.get("actor", "?")
        kind = e.get("event") or e.get("type", "")
        repo = f" [{e['repo']}]" if e.get("repo") else ""
        click.echo(f"{e.get('ts', '')} {who}/{kind}{repo}: {e.get('text', '')}")
    if errors:
        click.secho(f"({errors} corrupt journal line(s) skipped)", fg="yellow", err=True)


def _feature_commits(ws, feat) -> list:
    """Sandbox-branch commits across enrolled repos as journal-shaped entries.

    Derived from git at read time — never stored. Range is fork_base..sandbox
    when the feature was forked, else merge-base(default)..sandbox.
    """
    from mgit.core import git as gitmod

    entries = []
    sb = sandbox_branch(feat.name)
    for repo_name in feat.branches:
        try:
            repo_path = ws.repo_path(repo_name)
            check = gitmod.run_git("rev-parse", "--verify", "--quiet",
                                   f"refs/heads/{sb}", cwd=repo_path, check=False)
            if check.returncode != 0:
                continue
            base = feat.fork_base.get(repo_name)
            if not base:
                default = ws.get_repo(repo_name).default_branch
                mb = gitmod.run_git("merge-base", default, sb,
                                    cwd=repo_path, check=False)
                base = mb.stdout.strip() if mb.returncode == 0 else None
            range_arg = f"{base}..{sb}" if base else sb
            result = gitmod.run_git(
                "log", "--format=%H%x00%aI%x00%s", range_arg,
                cwd=repo_path, check=False,
            )
            if result.returncode != 0:
                continue
            for line in result.stdout.splitlines():
                parts = line.split("\x00")
                if len(parts) != 3:
                    continue
                sha, ts, subject = parts
                entries.append({
                    "ts": ts, "actor": "git", "type": "commit",
                    "repo": repo_name, "text": subject,
                    "meta": {"sha": sha[:12]},
                })
        except Exception:
            continue
    return entries


# --- Branching ---


@feature.command("fork")
@click.argument("child_name")
@click.option("--from", "-p", "parent_name", default=None,
              help="Parent feature (default: active).")
@click.option("--branch", default=None,
              help="Child's remote target branch (default: child name).")
@click.option("--description", "-d", default="", help="Child description.")
@click.option("--materialize", "-m", is_flag=True, default=False,
              help="Materialize all child worktrees immediately.")
@click.option("--carry-wip", is_flag=True, default=False,
              help="Snapshot the parent's uncommitted work and re-apply it in the child.")
@click.option("--no-setup", is_flag=True, default=False,
              help="Skip running setup commands after materialization.")
def fork(child_name, parent_name, branch, description, materialize, carry_wip, no_setup):
    """Fork a feature: new variant with pinned base SHAs and inherited memory."""
    ws = Workspace.find()
    fm = FeatureManager(ws)
    parent = _feature_or_active(ws, parent_name)

    child = fm.fork(
        child_name, parent,
        target_branch=branch,
        description=description,
        materialize=materialize,
        run_setup=not no_setup,
        carry_wip=carry_wip,
    )
    write_feature_brief(ws, child)

    click.echo(f"Forked '{parent}' -> '{child_name}' ({len(child.branches)} repos).")
    for repo_name in child.branches:
        base = child.fork_base.get(repo_name, "?")[:12]
        wip = " +wip" if repo_name in child.wip_from else ""
        mat = ""
        if materialize:
            mat = f" -> {ws.worktree_path(child_name, repo_name)}/"
        click.echo(f"  + {repo_name} @ {base}{wip}{mat}")
    click.echo(f"Active feature: {child_name}")


@feature.command("tree")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def tree(as_json):
    """Show feature ancestry (forked features indented under their parent)."""
    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    features = fm.list()
    active = ws.get_active_feature()

    by_name = {f.name: f for f in features}
    children: dict = {}
    roots = []
    for f in features:
        if f.parent and f.parent in by_name:
            children.setdefault(f.parent, []).append(f.name)
        else:
            roots.append(f.name)

    if as_json:
        output.emit("feature.tree", {
            "active": active,
            "features": [
                {
                    "name": f.name,
                    "parent": f.parent,
                    "repos": len(f.branches),
                    "materialized": sum(
                        1 for r in f.branches
                        if ws.worktree_path(f.name, r).exists()
                    ),
                }
                for f in features
            ],
        })
        return

    if not features:
        click.echo("No features defined. Use 'mgit feature start' to create one.")
        return

    def render(name, depth):
        f = by_name[name]
        marker = "* " if name == active else "  "
        mat = sum(1 for r in f.branches if ws.worktree_path(name, r).exists())
        indent = "  " * depth + ("└─ " if depth else "")
        click.echo(f"{marker}{indent}{name} [{mat}/{len(f.branches)} repos]")
        for child in sorted(children.get(name, [])):
            render(child, depth + 1)

    for root in sorted(roots):
        render(root, 0)


# --- Delivery ---


def _pr_body(ws, feat, all_prs: dict[str, str], exclude_repo: str) -> str:
    """Generate a PR/MR body from the feature's working memory.

    Regenerated on every publish — the body is owned by mgit.
    """
    state = memory.load_state(ws, feat.name)
    lines = [feat.description or f"Cross-repo feature '{feat.name}'.", ""]
    if state.goal:
        lines += [f"**Goal:** {state.goal}", ""]
    if state.status:
        lines += [f"**Status:** {state.status}", ""]
    decisions, _ = memory.read_journal(ws, feat.name, limit=5, type_filter="decision")
    if decisions:
        lines.append("**Decisions:**")
        lines += [f"- {d.get('text', '')}" for d in decisions]
        lines.append("")
    related = {r: u for r, u in all_prs.items() if r != exclude_repo}
    if related:
        lines.append(f"**Related ({feat.name}):**")
        lines += [f"- {r}: {u}" for r, u in sorted(related.items())]
        lines.append("")
    lines.append(f"---\n_Published by `mgit feature publish` from feature `{feat.name}`;"
                 " this body is regenerated on each publish._")
    return "\n".join(lines)


@feature.command("publish")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--title", default=None,
              help="PR/MR title (default: feature description or name).")
@click.option("--draft", is_flag=True, default=False, help="Create as draft.")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def publish(feature_name, title, draft, as_json):
    """Publish the feature: push each repo's branch and open linked PRs/MRs.

    The forge (GitHub via gh, GitLab via glab) is auto-detected per repo from
    its remote URL. Idempotent: re-running pushes new commits and updates the
    existing PR instead of duplicating it. PR bodies are generated from the
    feature's working memory and cross-link the sibling PRs.
    """
    from mgit.core.forge import get_forge
    from mgit.core.repo import Repo
    from mgit.models.types import BulkResult, OpStatus, RepoOpResult

    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)
    if not feat.branches:
        raise MgitError(f"Feature '{name}' has no enrolled repos.")

    result = BulkResult()
    # repos touched this run that have a PR number we can edit for cross-links
    touched: dict[str, dict] = {}
    new_urls: dict[str, str] = {}

    for repo_name, target in feat.branches.items():
        repo_info = ws.get_repo(repo_name)

        if not fm.is_materialized(name, repo_name):
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.SKIPPED, message="not materialized"))
            continue

        facts = memory.repo_facts(ws, feat, repo_name)
        if facts["ahead"] == 0:
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.SKIPPED,
                message=f"up to date with {facts['ahead_base']}"))
            continue

        wt_path = ws.worktree_path(name, repo_name)
        wt_repo = Repo.at_worktree(wt_path, repo_info)
        try:
            wt_repo.push_to_target(target)
        except MgitError as e:
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.FAILED, message=str(e)))
            continue

        dirty_note = " (worktree dirty — uncommitted changes not published)" \
            if facts["dirty"] else ""

        forge = get_forge(repo_info.url)
        if forge is None:
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.SUCCESS,
                message="pushed; no PR: can't detect forge from repo url"
                        f"{dirty_note}"))
            continue

        origin_path = ws.repo_path(repo_name)
        try:
            existing = forge.find_pr(origin_path, target)
            if existing:
                touched[repo_name] = {**existing, "forge": forge}
                new_urls[repo_name] = existing["url"]
                msg = f"pushed; {forge.term} updated: {existing['url']}"
            else:
                pr_title = title or feat.description or f"Feature: {name}"
                created = forge.create_pr(
                    origin_path, target, repo_info.default_branch,
                    pr_title, "(body pending)", draft=draft,
                )
                touched[repo_name] = {**created, "forge": forge}
                if created.get("url"):
                    new_urls[repo_name] = created["url"]
                msg = f"pushed; {forge.term} created: {created.get('url', '?')}"
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.SUCCESS,
                message=msg + dirty_note))
        except MgitError as e:
            result.results.append(RepoOpResult(
                repo=repo_name, status=OpStatus.FAILED,
                message=f"pushed, but {forge.term} step failed: {e}"))

    # Persist URLs, then (re)generate every touched PR body with cross-links
    if new_urls:
        feat = fm.record_prs(name, new_urls)
    for repo_name, info in touched.items():
        if info.get("number") is None:
            continue
        try:
            info["forge"].update_body(
                ws.repo_path(repo_name), info["number"],
                _pr_body(ws, feat, feat.prs, exclude_repo=repo_name),
            )
        except MgitError:
            pass  # body update is cosmetic; the push and PR already landed

    for repo_name, url in new_urls.items():
        memory.append_event(
            ws, name, f"Published '{repo_name}' -> {url}",
            event="published", repo=repo_name,
            meta={"url": url, "target_branch": feat.branches.get(repo_name)},
        )
    if new_urls:
        write_feature_brief(ws, feat)

    if as_json:
        output.emit("feature.publish", {
            "feature": name, "prs": dict(feat.prs), **result.to_dict(),
        })
        sys.exit(result.exit_code)
    from mgit.utils.display import print_bulk_results
    print_bulk_results(result)
    sys.exit(result.exit_code)


@feature.command("checks")
@click.option("--feature", "-f", "feature_name", default=None,
              help="Feature name (default: active).")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def checks(feature_name, as_json):
    """CI, review, and merge status of the feature's PRs/MRs across repos."""
    from mgit.core.forge import get_forge

    if as_json:
        output.set_json_mode()
    ws = Workspace.find()
    fm = FeatureManager(ws)
    name = _feature_or_active(ws, feature_name)
    feat = fm.get(name)

    rows = []
    for repo_name, target in feat.branches.items():
        repo_info = ws.get_repo(repo_name)
        forge = get_forge(repo_info.url)
        if forge is None:
            if repo_name in feat.prs:
                rows.append({"repo": repo_name, "url": feat.prs[repo_name],
                             "state": "unknown (forge not detected)"})
            continue
        try:
            st = forge.status(ws.repo_path(repo_name), target)
        except MgitError as e:
            rows.append({"repo": repo_name, "state": "error", "error": str(e)})
            continue
        if st is None:
            rows.append({"repo": repo_name, "state": "no-pr"})
        else:
            rows.append({"repo": repo_name, **st})

    if as_json:
        output.emit("feature.checks", {"feature": name, "prs": rows})
        return

    if not rows:
        click.echo(f"No publishable repos with a detectable forge in '{name}'.")
        return
    for r in rows:
        if "checks" in r:
            c = r["checks"]
            checks_s = f"{c['passed']} ok / {c['failed']} failed / {c['pending']} pending"
            review = f"  review: {r['review']}" if r.get("review") else ""
            click.echo(f"  {r['repo']:<20} {r['state']:<8} checks: {checks_s}{review}")
            click.echo(f"    {r.get('url', '')}")
        else:
            detail = r.get("error") or r.get("url") or ""
            click.echo(f"  {r['repo']:<20} {r['state']}  {detail}")
