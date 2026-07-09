"""Per-feature working memory: plan state (memory.toml) + append-only journal (journal.jsonl).

The memory sidecar lives at .mgit/features/<name>/ — a directory beside the
feature's flat TOML file, so the feature file format and FeatureManager.list()
glob are untouched. Git remains the source of truth for code state: dirty
flags, ahead counts, and commit subjects are computed live at read time and
never persisted here.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import tomli_w

from mgit.core import git
from mgit.core.workspace import Workspace
from mgit.models.types import FeatureInfo, MemoryState

STATE_FILE = "memory.toml"
JOURNAL_FILE = "journal.jsonl"

# Journal entry types an agent may write; "event" is reserved for mgit itself.
# Must stay a superset-free match with skill.EVIDENCE_TYPES: the distiller
# harvests every one of these, so a type it expects but nobody can write is a
# dead branch (that is what happened to "convention").
NOTE_TYPES = ("note", "decision", "convention", "question", "handoff")


def utc_now() -> str:
    """Current UTC time as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def memory_dir(ws: Workspace, feature_name: str) -> Path:
    return ws.features_dir / feature_name


def state_path(ws: Workspace, feature_name: str) -> Path:
    return memory_dir(ws, feature_name) / STATE_FILE


def journal_path(ws: Workspace, feature_name: str) -> Path:
    return memory_dir(ws, feature_name) / JOURNAL_FILE


def load_state(ws: Workspace, feature_name: str) -> MemoryState:
    """Load the plan file, returning an empty MemoryState if absent."""
    path = state_path(ws, feature_name)
    if not path.exists():
        return MemoryState()
    import tomllib
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return MemoryState(
        goal=data.get("goal", ""),
        status=data.get("status", ""),
        next_steps=list(data.get("next_steps", [])),
        open_questions=list(data.get("open_questions", [])),
        updated_at=data.get("updated_at", ""),
    )


def save_state(ws: Workspace, feature_name: str, state: MemoryState) -> None:
    """Atomically overwrite the plan file (write-temp + os.replace)."""
    state.updated_at = utc_now()
    d = memory_dir(ws, feature_name)
    d.mkdir(parents=True, exist_ok=True)
    data = {
        "goal": state.goal,
        "status": state.status,
        "next_steps": list(state.next_steps),
        "open_questions": list(state.open_questions),
        "updated_at": state.updated_at,
    }
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".memory-", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(data, f)
        os.replace(tmp, state_path(ws, feature_name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_event(
    ws: Workspace,
    feature_name: str,
    text: str,
    *,
    type_: str = "event",
    actor: str = "mgit",
    event: str | None = None,
    repo: str | None = None,
    meta: dict | None = None,
) -> None:
    """Append one journal line. A single flushed write keeps concurrent
    appends line-atomic on local POSIX filesystems."""
    entry: dict = {"ts": utc_now(), "actor": actor, "type": type_, "text": text}
    if event:
        entry["event"] = event
    if repo:
        entry["repo"] = repo
    if meta:
        entry["meta"] = meta
    d = memory_dir(ws, feature_name)
    d.mkdir(parents=True, exist_ok=True)
    with open(journal_path(ws, feature_name), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()


def read_journal(
    ws: Workspace,
    feature_name: str,
    limit: int | None = None,
    type_filter: str | None = None,
) -> tuple[list[dict], int]:
    """Read journal entries (oldest first). Corrupt lines are skipped and
    counted rather than failing the read.

    Returns (entries, error_count); limit keeps the most recent N entries.
    """
    path = journal_path(ws, feature_name)
    if not path.exists():
        return [], 0
    entries: list[dict] = []
    errors = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            if not isinstance(entry, dict):
                errors += 1
                continue
            if type_filter and entry.get("type") != type_filter:
                continue
            entries.append(entry)
    if limit is not None and len(entries) > limit:
        entries = entries[-limit:]
    return entries, errors


def journal_tail(ws: Workspace, feature_name: str, n: int) -> tuple[list[dict], int]:
    return read_journal(ws, feature_name, limit=n)


def relative_age(iso_ts: str) -> str:
    """Human 'updated Xm ago' from an ISO-8601 UTC timestamp; '' if unparsable."""
    try:
        then = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _ahead_base(ws: Workspace, repo_name: str, target_branch: str) -> str | None:
    """Hybrid ahead-count base: origin/<target> when the remote-tracking ref
    exists locally, else the local default_branch tip. Never fetches."""
    repo_path = ws.repo_path(repo_name)
    remote_ref = f"refs/remotes/origin/{target_branch}"
    result = git.run_git("rev-parse", "--verify", "--quiet", remote_ref,
                         cwd=repo_path, check=False)
    if result.returncode == 0:
        return f"origin/{target_branch}"
    default = ws.get_repo(repo_name).default_branch
    result = git.run_git("rev-parse", "--verify", "--quiet",
                         f"refs/heads/{default}", cwd=repo_path, check=False)
    if result.returncode == 0:
        return default
    return None


def repo_facts(ws: Workspace, feature: FeatureInfo, repo_name: str) -> dict:
    """Live git facts for one enrolled repo. Every git call is guarded so a
    broken repo degrades to unknowns instead of failing the brief."""
    from mgit.core.feature import sandbox_branch

    wt_path = ws.worktree_path(feature.name, repo_name)
    facts: dict = {
        "repo": repo_name,
        "target_branch": feature.branches.get(repo_name),
        "materialized": wt_path.exists(),
        "dirty": None,
        "ahead": None,
        "ahead_base": None,
        "last_commit": None,
        "pr": feature.prs.get(repo_name),
    }
    sb = sandbox_branch(feature.name)
    try:
        repo_path = ws.repo_path(repo_name)
        if facts["materialized"]:
            result = git.run_git("status", "--porcelain", cwd=wt_path, check=False)
            if result.returncode == 0:
                facts["dirty"] = bool(result.stdout.strip())
        # Sandbox branch lives in the repo's shared ref store once materialized
        result = git.run_git("rev-parse", "--verify", "--quiet",
                             f"refs/heads/{sb}", cwd=repo_path, check=False)
        if result.returncode == 0:
            base = _ahead_base(ws, repo_name, facts["target_branch"] or "")
            if base:
                count = git.run_git("rev-list", "--count", f"{base}..{sb}",
                                    cwd=repo_path, check=False)
                if count.returncode == 0:
                    facts["ahead"] = int(count.stdout.strip())
                    facts["ahead_base"] = base
            subject = git.run_git("log", "-1", "--format=%h %s", sb,
                                  cwd=repo_path, check=False)
            if subject.returncode == 0:
                facts["last_commit"] = subject.stdout.strip()
    except Exception:
        pass
    return facts


def build_brief(
    ws: Workspace,
    feature: FeatureInfo,
    tail: int = 10,
    full: bool = False,
) -> dict:
    """Assemble the re-orientation brief: plan state + journal tail + live git facts."""
    state = load_state(ws, feature.name)
    limit = None if full else tail
    entries, errors = read_journal(ws, feature.name, limit=limit)
    return {
        "feature": feature.name,
        "description": feature.description,
        "parent": feature.parent,
        "memory": {
            "goal": state.goal,
            "status": state.status,
            "next_steps": list(state.next_steps),
            "open_questions": list(state.open_questions),
            "updated_at": state.updated_at,
        },
        "journal": entries,
        "journal_errors": errors,
        "repos": [repo_facts(ws, feature, r) for r in feature.branches],
    }


def render_brief(brief: dict) -> str:
    """Render a brief dict as compact markdown (~40 lines)."""
    lines: list[str] = []
    mem = brief["memory"]
    name = brief["feature"]
    lines.append(f"# Feature: {name}")
    if brief.get("description"):
        lines.append(f"{brief['description']}")
    if brief.get("parent"):
        lines.append(f"Forked from: {brief['parent']}")
    age = relative_age(mem["updated_at"]) if mem["updated_at"] else ""
    staleness = f" (updated {age})" if age else " (no memory recorded yet)"
    lines.append("")
    lines.append(f"## Working memory{staleness}")
    lines.append(f"Goal: {mem['goal'] or '(not set)'}")
    lines.append(f"Status: {mem['status'] or '(not set)'}")
    if mem["next_steps"]:
        lines.append("Next steps:")
        for i, step in enumerate(mem["next_steps"], 1):
            lines.append(f"  {i}. {step}")
    if mem["open_questions"]:
        lines.append("Open questions:")
        for i, q in enumerate(mem["open_questions"], 1):
            lines.append(f"  {i}. {q}")
    lines.append("")
    lines.append("## Repos")
    for facts in brief["repos"]:
        bits = []
        bits.append("materialized" if facts["materialized"] else "not materialized")
        if facts["dirty"] is not None:
            bits.append("dirty" if facts["dirty"] else "clean")
        if facts["ahead"] is not None:
            bits.append(f"{facts['ahead']} ahead of {facts['ahead_base']}")
        if facts["last_commit"]:
            bits.append(f"last: {facts['last_commit']}")
        if facts.get("pr"):
            bits.append(f"pr: {facts['pr']}")
        lines.append(f"- {facts['repo']} -> {facts['target_branch']} ({', '.join(bits)})")
    if brief["journal"]:
        lines.append("")
        lines.append("## Recent journal")
        for e in brief["journal"]:
            who = e.get("actor", "?")
            kind = e.get("event") or e.get("type", "")
            repo = f" [{e['repo']}]" if e.get("repo") else ""
            lines.append(f"- {e.get('ts', '')} {who}/{kind}{repo}: {e.get('text', '')}")
    if brief.get("journal_errors"):
        lines.append(f"({brief['journal_errors']} corrupt journal line(s) skipped)")
    return "\n".join(lines) + "\n"
