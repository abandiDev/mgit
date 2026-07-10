"""Skill distillation: durable, reviewable skills mined from working memory.

Per-feature working memory is transient — it dies with the feature. This layer
graduates the *durable* lessons out of it: it reads the steering the journals
already capture across features (decisions, handoffs, questions, notes), asks an
LLM to distill only reusable procedures/conventions under a strict schema, gates
them so a wrong skill can never slip through unreviewed, and — once a human
approves — advertises them in the ambient worktree brief every future session
already loads.

Storage lives entirely under .mgit/skills/ (no new substrate type):
    parked.jsonl        n=1 prose candidates awaiting a second occurrence
    tombstones.jsonl    rejected candidates, never re-proposed
    ledger.jsonl        distill/review decisions
    drafts/<slug>/      SKILL.md + skill.toml awaiting human review
    active/<slug>/      approved skills, advertised in the ambient brief

Skill ops are foreground and user-initiated (unlike a hook firing on every edit),
so — consistent with mgit's no-locking invariant — concurrency safety rests on
single flushed appends and temp+os.replace swaps, not flock.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from mgit.core import git, llm, memory, redact
from mgit.core.workspace import Workspace
from mgit.models.types import SkillInfo
from mgit.utils.errors import MgitError, SkillNotFoundError

DRAFTS_DIR = "drafts"
ACTIVE_DIR = "active"
PARKED_FILE = "parked.jsonl"
TOMBSTONES_FILE = "tombstones.jsonl"
LEDGER_FILE = "ledger.jsonl"
META_FILE = "skill.toml"
SKILL_MD = "SKILL.md"

# The slug becomes a directory name and is emitted by the LLM, which the schema
# only *requests* to constrain — so it is re-validated in Python before any use.
VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
MAX_DESCRIPTION_CHARS = 1024

# An anti-trigger written as an imperative rather than a situation clause.
_IMPERATIVE_ANTI_RE = re.compile(r"^(do not|don't|never|avoid)\b", re.IGNORECASE)
PARKED_MAX_AGE_DAYS = 45

# Journal entry types that carry durable steering worth distilling. mgit-authored
# "event" rows (commit/push/materialize/...) are lifecycle noise, excluded here.
EVIDENCE_TYPES = ("decision", "convention", "handoff", "question", "note")


CANDIDATES_SCHEMA: dict = {
    "type": "object",
    "required": ["candidates"],
    "additionalProperties": False,
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "slug",
                    "kind",
                    "scope_level",
                    "title",
                    "trigger_description",
                    "anti_triggers",
                    "steps",
                    "evidence",
                ],
                "additionalProperties": False,
                "properties": {
                    "slug": {
                        "type": "string",
                        "pattern": "^[a-z0-9][a-z0-9-]{2,60}$",
                        "description": "kebab-case identifier, gerund form preferred",
                    },
                    "kind": {
                        "enum": [
                            "durable-procedure",
                            "durable-convention",
                            "one-off",
                            "personal-preference",
                        ]
                    },
                    "scope_level": {
                        "enum": ["feature", "workspace", "global"],
                        "description": (
                            "feature: applies only to one area/repo; workspace: "
                            "applies across this workspace; global: a tool/framework "
                            "lesson that holds in any workspace"
                        ),
                    },
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "glob(s) the skill applies to; useful for feature scope",
                    },
                    "title": {"type": "string"},
                    "trigger_description": {
                        "type": "string",
                        "description": (
                            "retrieval-optimized: concrete commands, error strings, repo/file "
                            "names; state both what it does and when to use it"
                        ),
                    },
                    "anti_triggers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "situations where this skill must NOT be applied",
                    },
                    "preconditions": {"type": "array", "items": {"type": "string"}},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "procedure steps; abstract varying values as {placeholders}",
                    },
                    "verify_command": {
                        "type": ["string", "null"],
                        "description": "shell command proving the procedure worked (exit 0)",
                    },
                    "concrete_example": {
                        "type": "string",
                        "description": "one worked example with real values, from the evidence",
                    },
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["quote", "kind"],
                            "additionalProperties": False,
                            "properties": {
                                "quote": {
                                    "type": "string",
                                    "description": "VERBATIM quote from the journal evidence",
                                },
                                "kind": {
                                    "enum": [
                                        "note",
                                        "decision",
                                        "convention",
                                        "handoff",
                                        "question",
                                    ]
                                },
                            },
                        },
                    },
                    "recurrence_of": {
                        "type": ["string", "null"],
                        "description": "id of the parked candidate this recurs, if any",
                    },
                    "updates_existing_skill": {
                        "type": ["string", "null"],
                        "description": "name of an existing skill this should amend, if any",
                    },
                    "watched_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "globs whose change should prompt re-verification",
                    },
                },
            },
        }
    },
}


@dataclass
class SkillConfig:
    """Skill settings, read from the [skills] table of .mgit/config.toml."""

    claude_bin: str = "claude"
    model: str = ""
    # A verify_command is written by the LLM from journal content that may be
    # attacker-influenced. Executing it during distill is code execution with no
    # human present, so it is opt-in; otherwise it runs at
    # `mgit skill approve --run-verify`.
    allow_auto_verify: bool = False
    verify_timeout: int = 120
    distill_timeout: int = 600


def load_skill_config(ws: Workspace) -> SkillConfig:
    cfg = SkillConfig()
    if not ws.config_path.exists():
        return cfg
    try:
        with open(ws.config_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return cfg
    section = data.get("skills", {})
    if not isinstance(section, dict):
        return cfg
    for key, value in section.items():
        attr = key.replace("-", "_")
        if hasattr(cfg, attr) and isinstance(value, type(getattr(cfg, attr))):
            setattr(cfg, attr, value)
    return cfg


# --- paths ------------------------------------------------------------------

def _drafts_dir(ws: Workspace) -> Path:
    return ws.skills_dir / DRAFTS_DIR


def _active_dir(ws: Workspace) -> Path:
    return ws.skills_dir / ACTIVE_DIR


def _parked_path(ws: Workspace) -> Path:
    return ws.skills_dir / PARKED_FILE


def _tombstones_path(ws: Workspace) -> Path:
    return ws.skills_dir / TOMBSTONES_FILE


def _ledger_path(ws: Workspace) -> Path:
    return ws.skills_dir / LEDGER_FILE


# --- storage helpers --------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    import json

    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _append_jsonl(path: Path, obj: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()


def _update_jsonl(path: Path, mutate: Callable[[list[dict]], list[dict]]) -> None:
    """Read-modify-write via temp+os.replace. Safe for mgit's serial skill ops."""
    import json

    rows = mutate(_read_jsonl(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for obj in rows:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ledger_append(ws: Workspace, entry: dict) -> None:
    entry = {"ts": memory.utc_now(), **entry}
    _append_jsonl(_ledger_path(ws), entry)


# --- metadata sidecar (skill.toml) ------------------------------------------

def read_meta(skill_dir: Path) -> dict:
    path = skill_dir / META_FILE
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_meta(skill_dir: Path, meta: dict) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    # tomli_w rejects None; store only set keys and normalize.
    clean = {k: v for k, v in meta.items() if v is not None}
    fd, tmp = tempfile.mkstemp(dir=skill_dir, prefix=f".{META_FILE}-")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(clean, f)
        os.replace(tmp, skill_dir / META_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_meta(skill_dir: Path, **changes) -> None:
    meta = read_meta(skill_dir)
    for key, value in changes.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    write_meta(skill_dir, meta)


# --- evidence ---------------------------------------------------------------

def _feature_names(ws: Workspace) -> list[str]:
    if not ws.features_dir.is_dir():
        return []
    return sorted(p.stem for p in ws.features_dir.glob("*.toml"))


def gather_evidence(ws: Workspace, feature: str | None = None) -> list[dict]:
    """Collect durable steering entries from feature journals (scrubbed).

    One feature when `feature` is given, else every feature. mgit-authored
    lifecycle events are excluded — only agent/human durable signals qualify.
    """
    names = [feature] if feature else _feature_names(ws)
    events: list[dict] = []
    for name in names:
        entries, _ = memory.read_journal(ws, name)
        for e in entries:
            if e.get("type") not in EVIDENCE_TYPES:
                continue
            text = redact.scrub(str(e.get("text", ""))).strip()
            if not text:
                continue
            events.append({"feature": name, "type": e.get("type"), "text": text})
    return events


# --- distiller prompt -------------------------------------------------------

PROMPT_TEMPLATE = """You are mgit's skill distiller. Below are steering signals recorded across this workspace's features — the decisions, conventions, handoffs, questions and notes the developer and agents logged while working. Distill ONLY durable, reusable lessons into skill candidates. A wrong or over-general skill is worse than no skill; when unsure, classify as one-off.

## Recorded steering (verbatim, from the mgit journals)

{events_block}

## Classification rules

- kind: durable-procedure (a multi-step workflow worth re-teaching), durable-convention (a standing rule of this workspace/repo), one-off (specific to one task — most entries are this), personal-preference (how this developer likes to work).
- scope_level: "feature" if every piece of evidence touches one area/repo — set paths to the narrowest glob(s). "workspace" if the lesson is repo-wide or spans unrelated areas. "global" if it is a tool/framework lesson that would hold in a different workspace.
- Abstract varying values into {{placeholders}} ONLY where the evidence shows the value varying; never abstract by guess. This applies to `steps`, NEVER to `verify_command`.
- steps must be concrete enough to execute; anything mechanizable (a command sequence) should also produce verify_command — a single shell command that exits 0 iff the procedure worked. It must be runnable AS WRITTEN from the repo root: no {{placeholders}}, no `cd` into an unknown directory. If you cannot write a concrete command, set verify_command to null.
- verify_command must be READ-ONLY. Never write, stash, reset, checkout, or otherwise mutate the repo — it is executed against the user's real checkout.
- evidence quotes must be VERBATIM from the entries above. No paraphrase.
- anti_triggers are mandatory: when would applying this skill be wrong?

## Deduplication context

Existing skills (propose updates_existing_skill with the skill name instead of a duplicate):
{skills_block}

Parked observations from earlier runs (if a lesson here is the same one, set recurrence_of to its id):
{parked_block}

Rejected before — do NOT re-propose anything equivalent to these:
{tombstones_block}

Emit zero candidates if nothing durable was recorded. Zero is a good answer.
"""


def _events_block(events: list[dict]) -> str:
    lines = [
        f'- [{e["feature"]}/{e.get("type", "note")}] "{redact.scrub(e.get("text", ""))}"'
        for e in events
    ]
    return "\n".join(lines) or "(none)"


def _skills_block(skills: list[SkillInfo]) -> str:
    lines = [f"- {s.slug}: {s.description[:200]}" for s in skills]
    return "\n".join(lines) or "(none yet)"


def _parked_block(parked: list[dict]) -> str:
    lines = [
        f"- id={p['id']}: {p.get('title', '')} — {p.get('trigger_description', '')[:200]}"
        for p in parked
        if p.get("status") != "resolved"
    ]
    return "\n".join(lines) or "(none)"


def _tombstones_block(tombstones: list[dict]) -> str:
    lines = [f"- {t.get('slug')}: rejected because {t.get('reason', 'unspecified')}" for t in tombstones]
    return "\n".join(lines) or "(none)"


def build_prompt(
    events: list[dict],
    skills: list[SkillInfo],
    parked: list[dict],
    tombstones: list[dict],
) -> str:
    return PROMPT_TEMPLATE.format(
        events_block=_events_block(events),
        skills_block=_skills_block(skills),
        parked_block=_parked_block(parked),
        tombstones_block=_tombstones_block(tombstones),
    )


# --- SKILL.md rendering -----------------------------------------------------

def _yaml_dq(value: str) -> str:
    """A YAML double-quoted scalar. Values here are controlled, but escape anyway."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip() + '"'


def _frontmatter(name: str, description: str, paths: list[str]) -> str:
    lines = ["---", f"name: {name}", f"description: {_yaml_dq(description)}"]
    if paths:
        lines.append("paths: [" + ", ".join(_yaml_dq(p) for p in paths) + "]")
    lines.append("---")
    return "\n".join(lines)


def build_description(candidate: dict) -> str:
    desc = str(candidate.get("trigger_description", "")).strip()
    if candidate.get("scope_level") == "feature" and candidate.get("paths"):
        desc += f" Use when working under {', '.join(candidate['paths'])}."
    anti = candidate.get("anti_triggers") or []
    if anti:
        first = str(anti[0]).strip().rstrip(".")
        # The schema asks for a situation clause ("the repo has no pytest suite"),
        # which takes the prefix. Models routinely answer with an imperative
        # instead ("Do not expand this to every commit") — prefixing that yields
        # "Do not use when Do not expand...". Imperatives stand as their own
        # sentence; this string is what the ambient brief shows every session.
        if _IMPERATIVE_ANTI_RE.match(first):
            desc += f" {first}."
        elif first:
            desc += f" Do not use when {first}."
    if len(desc) > MAX_DESCRIPTION_CHARS:
        desc = desc[: MAX_DESCRIPTION_CHARS - 1] + "…"
    return desc


def render_skill_md(candidate: dict, features: list[str]) -> str:
    slug = candidate["slug"]
    description = build_description(candidate)
    paths = candidate.get("paths") or []
    fm_paths = paths if candidate.get("scope_level") == "feature" else []
    lines = [_frontmatter(slug, description, fm_paths), "", f"# {candidate.get('title', slug)}", ""]
    lines += ["## When to use", ""]
    lines.append(f"- {candidate.get('trigger_description', '')}")
    for pre in candidate.get("preconditions") or []:
        lines.append(f"- Precondition: {pre}")
    lines += ["", "## When NOT to use", ""]
    for anti in candidate.get("anti_triggers") or []:
        lines.append(f"- {anti}")
    lines.append(
        "- This skill is advisory: if its preconditions contradict the current "
        "state of the repo, prefer local evidence and say why."
    )
    lines += ["", "## Procedure", ""]
    for i, step in enumerate(candidate.get("steps") or [], 1):
        lines.append(f"{i}. {step}")
    if candidate.get("verify_command"):
        lines += ["", "## Verify", "", "```bash", candidate["verify_command"], "```"]
    if candidate.get("concrete_example"):
        lines += ["", "## Worked example", "", redact.scrub(str(candidate["concrete_example"]))]
    lines += ["", "## Provenance", ""]
    lines.append(f"Distilled from feature(s): {', '.join(features) or 'unknown'}")
    for ev in candidate.get("evidence") or []:
        quote = redact.scrub(str(ev.get("quote", ""))).strip()
        if quote:
            # The journal's own word for the entry, when the quote resolved to
            # one. Provenance that renames a convention is not provenance.
            kind = ev.get("recorded_type") or ev.get("kind") or "evidence"
            lines.append("")
            lines.append(f"> [{kind}] {quote}")
    return "\n".join(lines) + "\n"


def _contained(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _repos_of(ws: Workspace, features: list[str]) -> list[str]:
    """Registered repos enrolled in any of `features`, sorted and deduped."""
    from mgit.core.feature import FeatureManager

    fm = FeatureManager(ws)
    names: set[str] = set()
    for feature in features:
        try:
            names.update(fm.get(feature).branches)
        except Exception:
            continue
    return sorted(n for n in names if n in ws.repos)


def verify_repo(ws: Workspace, meta: dict) -> str | None:
    """The single repo a verify_command belongs to, or None if ambiguous."""
    repos = [r for r in (meta.get("repos") or []) if r in ws.repos]
    if not repos:
        # Drafts written before `repos` was recorded still know their features,
        # and falling back to the workspace root is exactly the bug this
        # function exists to fix.
        repos = _repos_of(ws, list(meta.get("features") or []))
    return repos[0] if len(repos) == 1 else None


def verify_cwd(ws: Workspace, meta: dict) -> Path:
    """The tree a verify_command validates.

    The repo it was learned in, when that is unambiguous. Otherwise the
    workspace root, which is only correct for a repo-agnostic command.
    """
    repo = verify_repo(ws, meta)
    return ws.repo_path(repo) if repo else ws.root


def render_draft(ws: Workspace, candidate: dict, features: list[str]) -> Path:
    """Render a candidate into .mgit/skills/drafts/<slug>/ (never the active dir)."""
    slug = candidate["slug"]
    draft_dir = _drafts_dir(ws) / slug
    # `slug` is model output. Nothing may be written outside drafts/ — a skill
    # planted straight into active/ would reach agents without human review.
    if not _contained(draft_dir, _drafts_dir(ws)):
        raise MgitError(f"skill slug {slug!r} escapes the drafts directory")
    draft_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(draft_dir / SKILL_MD, render_skill_md(candidate, features))
    write_meta(
        draft_dir,
        {
            "slug": slug,
            "title": candidate.get("title", slug),
            "description": build_description(candidate),
            "kind": candidate.get("kind", ""),
            "scope_level": candidate.get("scope_level", "workspace"),
            "paths": candidate.get("paths") or [],
            "watched_paths": candidate.get("watched_paths") or [],
            "verify_command": candidate.get("verify_command"),
            "status": "draft",
            "created_at": memory.utc_now(),
            "features": list(features),
            # Which repos this skill was learned in. A verify_command is written
            # against a repo's layout ("npx vitest run src/..."), so it has to
            # run there — the workspace root is a directory of symlinks, not a
            # project, and running there sweeps every repo and worktree at once.
            "repos": _repos_of(ws, features),
            "updates": candidate.get("updates_existing_skill"),
        },
    )
    return draft_dir


# --- verify -----------------------------------------------------------------

# `{project-root}`, `{test_file}` — an unsubstituted placeholder the model
# carried over from `steps`. Requires an interior `-` or `_` so real shell
# (`${VAR}`, `awk '{print}'`, `{1..3}`) is not mistaken for one.
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{[a-zA-Z][a-zA-Z0-9]*(?:[-_][a-zA-Z0-9]+)+\}")


def _normalized(text: object) -> str:
    return " ".join(str(text).split())


def _evidence_key(entry_text: str) -> str:
    """A stable id for one journal entry, independent of how it was quoted."""
    return hashlib.sha256(_normalized(entry_text).encode("utf-8")).hexdigest()[:16]


def ground_evidence(candidate: dict, events: list[dict]) -> dict:
    """Resolve each evidence quote back to the journal entry it came from.

    Stamps two fields mgit derives itself and the distiller cannot forge:
    `recorded_type` (the journal's own word for the entry) and `recorded_key`
    (which entry it was). Both were previously taken on trust from the model's
    echoed `kind`, which cannot be right: `convention` was missing from the
    schema's enum for as long as journals could record one, so a convention came
    back labelled `decision` every time -- not a model error, a schema one.

    Quotes are required to be verbatim, so this is a lookup rather than a guess.
    Match on containment: a model legitimately quotes the opening sentences of a
    long entry and stops. A quote that resolves to nothing was not verbatim, and
    grounds to nothing -- it can support no gate.

    Works on any dict carrying an `evidence` list: candidates and parked rows.
    """
    recorded = [(_normalized(e["text"]), e["type"]) for e in events]
    for item in candidate.get("evidence") or []:
        quote = _normalized(item.get("quote", ""))
        item["recorded_type"] = None
        item["recorded_key"] = None
        if not quote:
            continue
        for text, kind in recorded:
            if quote in text:
                item["recorded_type"] = kind
                item["recorded_key"] = _evidence_key(text)
                break
    return candidate


def evidence_keys(holder: dict) -> set[str]:
    """The journal entries a candidate or parked row actually rests on.

    Ungrounded quotes contribute nothing, so a candidate that cites no real
    journal text has no evidence at all — which is the honest answer.
    """
    return {
        str(item["recorded_key"])
        for item in holder.get("evidence") or []
        if item.get("recorded_key")
    }


def journal_corpus(events: list[dict]) -> set[str]:
    """Every journal entry the distiller was shown, as keys."""
    return {_evidence_key(str(e["text"])) for e in events}


def is_second_occurrence(candidate: dict, parked_row: dict) -> bool:
    """True when the lesson has been seen again since it was parked.

    The baseline is the journal as it stood at park time, NOT the quotes the
    distiller happened to cite then. Every run re-reads the whole journal, and
    the model freely re-quotes a different subset of it; measured against its
    own last citation, `moving-viz-nodes-only-from-the-timeline` swapped one of
    two quotes and read as a fresh occurrence of a lesson nothing had repeated.

    A row parked before this baseline existed cannot answer the question, so it
    is not a recurrence -- it re-parks once, records its baseline, and is
    judged properly from then on.
    """
    baseline = parked_row.get("journal_keys")
    if baseline is None:
        return False
    return bool(evidence_keys(candidate) - {str(k) for k in baseline})


def evidence_records_a_convention(candidate: dict) -> bool:
    """True when a quote resolves to a journal entry recorded as a `convention`.

    A rule is DECLARED, not detected. Writing a `convention` is how the
    developer states a standing rule, and it is the only thing that says so.

    mgit used to ask the distiller (`explicit_rule`) and then check the answer by
    grepping the quote for never/always/must. The model set the flag on eight
    consecutive design rationales, and over this workspace's own journal the
    regex scored one hit in three: it read "opposite twin edges `do not` bow
    apart" and "the first repaint `always` saw a mismatch" as standing rules,
    while the one entry it got right had already been typed a `convention`.
    Prose that describes a defect is not prose that states a law.

    Only `recorded_type` counts, never the model's `kind`: mgit stamps the
    former from its own journal, so this cannot be talked past.
    """
    return any(
        item.get("recorded_type") == "convention" for item in candidate.get("evidence") or []
    )


def runnable_verify(command: str | None) -> str | None:
    """The command mgit could actually execute, or None.

    A verify_command exists to prove a lesson mechanically, and its presence is
    enough to draft a candidate past the n=2 rule. `cd {project-root} && npx
    vitest run {test-file-path}` can never exit 0, so it proves nothing and must
    not buy that exemption.
    """
    if not command or not command.strip():
        return None
    return None if _PLACEHOLDER_RE.search(command) else command.strip()


def watched_changes(ws: Workspace, meta: dict) -> int:
    """Commits touching a skill's watched_paths since it was last verified.

    `watched_paths` was recorded on every skill and read by nothing; this is what
    it is for. A skill whose subject matter moved underneath it is a skill whose
    verification is stale, even though it once passed.
    """
    verified_at = meta.get("verified_at")
    watched = [str(p) for p in (meta.get("watched_paths") or [])]
    repo = verify_repo(ws, meta)
    if not (verified_at and watched and repo):
        return 0
    result = git.run_git(
        "log", "--oneline", f"--since={verified_at}", "--", *watched,
        cwd=ws.repo_path(repo), check=False,
    )
    if result.returncode != 0:
        return 0
    return len([line for line in result.stdout.splitlines() if line.strip()])


@dataclass
class VerifyResult:
    """Outcome of one verify_command run, and what it actually validated."""

    ok: bool
    output: str
    tree: Path  # the repo (or workspace root) the command spoke about
    ref: str = "HEAD"  # the commit-ish checked out in the sandbox
    sandboxed: bool = True


def verify_commit(ws: Workspace, repo_name: str, meta: dict, under_review: bool) -> str:
    """The commit-ish a verify_command should be checked against.

    Under review (a draft), the lesson's evidence lives on the feature branch it
    was learned on, and that branch usually carries files `main` has never seen —
    verifying against `main` fails for reasons that have nothing to do with the
    skill. Once the skill is active it is a standing claim about the repo as it
    is now, so it verifies against HEAD.
    """
    from mgit.core.feature import sandbox_branch
    from mgit.core.repo import Repo

    repo = Repo(ws.get_repo(repo_name), ws.root)
    if under_review:
        for feature in meta.get("features") or []:
            branch = sandbox_branch(str(feature))
            if repo.rev_parse(branch):
                return branch
    return "HEAD"


@contextlib.contextmanager
def verify_sandbox(
    ws: Workspace, repo_name: str, timeout: int, ref: str = "HEAD"
) -> Iterator[Path]:
    """A throwaway clone of `repo_name` at `ref`, removed afterwards.

    A clone, not a `git worktree`: linked worktrees share the repository's refs,
    so a `git stash pop` inside one consumes the real stash. A local clone gets
    its own refs (objects are hardlinked, so it is cheap), and nothing the
    command does to git state can reach the user's checkout.

    The clone has no ignored files, so a JS or Python project would have no
    dependencies installed. The repo's own `mgit repo setup` hook — the same one
    that makes a feature worktree runnable — is run to provide them.
    """
    source = ws.repo_path(repo_name).resolve()
    sha = git.run_git("rev-parse", ref, cwd=source, check=False).stdout.strip()
    if not sha:
        raise MgitError(f"repo '{repo_name}' has no commit at '{ref}'; cannot sandbox a verify")

    tmp = Path(tempfile.mkdtemp(prefix="mgit-verify-"))
    sandbox = tmp / repo_name
    try:
        git.run_git("clone", "--local", "--quiet", "--no-checkout",
                    str(source), str(sandbox))
        git.run_git("checkout", "--detach", sha, cwd=sandbox)
        setup = ws.get_repo(repo_name).setup
        if setup:
            subprocess.run(  # noqa: S602 - user-authored, same hook as materialize
                setup, shell=True, cwd=str(sandbox),
                capture_output=True, text=True, timeout=timeout,
            )
        yield sandbox
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


NOT_RUNNABLE = (
    "verify_command has unsubstituted {placeholders}; it can never pass. "
    "Re-distill the skill, or clear the command in its skill.toml."
)


def run_verify_for(
    ws: Workspace,
    command: str,
    meta: dict,
    cfg: SkillConfig,
    under_review: bool = False,
) -> VerifyResult:
    """Run a verify_command against the tree it belongs to, in isolation.

    The command is LLM-authored shell, so it never touches that tree directly —
    it runs in a disposable clone of it.
    """
    # runnable_verify() sanitises at distill time, but a skill minted before that
    # check — or one whose skill.toml was hand-edited — can still carry a command
    # full of {placeholders}. Never execute it: it cannot pass, and asking a
    # human to consent to shell that will only ever fail is worse than useless.
    if runnable_verify(command) is None:
        return VerifyResult(False, NOT_RUNNABLE, verify_cwd(ws, meta), ref="-", sandboxed=False)

    repo = verify_repo(ws, meta)
    if repo is None:
        # No single repo to clone; a repo-agnostic command runs at the root.
        ok, output = run_verify(command, ws.root, cfg.verify_timeout)
        return VerifyResult(ok, output, ws.root, ref="-", sandboxed=False)
    ref = verify_commit(ws, repo, meta, under_review)
    with verify_sandbox(ws, repo, cfg.verify_timeout, ref) as sandbox:
        ok, output = run_verify(command, sandbox, cfg.verify_timeout)
    return VerifyResult(ok, output, ws.repo_path(repo), ref=ref)


def run_verify(command: str, cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {timeout}s"
    output = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return proc.returncode == 0, output


# --- routing gates ----------------------------------------------------------

def route_candidate(
    ws: Workspace,
    candidate: dict,
    parked: list[dict],
    cfg: SkillConfig,
    scope_features: list[str],
) -> tuple[str, str]:
    """Apply the admission gates. Returns (action, detail)."""
    kind = candidate.get("kind")
    slug = candidate.get("slug", "unnamed")
    if not VALID_SLUG.match(str(slug)):
        return "drop", f"invalid slug {slug!r}"
    if kind == "one-off":
        return "drop", "classified one-off"
    if kind == "personal-preference":
        candidate["scope_level"] = "global"

    parked_rows = {p["id"]: p for p in parked if p.get("status") != "resolved"}
    recurrence = candidate.get("recurrence_of")
    if recurrence and recurrence in parked_rows:
        parked_id: str | None = str(recurrence)
    elif slug in parked_rows:
        parked_id = slug
    else:
        parked_id = None
    # The n=2 rule counts occurrences of the LESSON, not runs of the distiller.
    # Every run re-reads the whole journal and re-proposes what it parked last
    # time, so matching the parked id alone let `mgit skill distill` twice over
    # an unchanged journal promote its own backlog.
    is_recurrence = bool(parked_id) and is_second_occurrence(candidate, parked_rows[parked_id])

    # A placeholder command is not a verification. Drop it so nothing downstream
    # tries to run `cd {project-root}`.
    candidate["verify_command"] = runnable_verify(candidate.get("verify_command"))
    verify_cmd = candidate["verify_command"]
    # A verify_command says the skill is CHECKABLE. The n=2 rule asks whether the
    # lesson is DURABLE. Those are different questions, and treating the first as
    # an answer to the second let `test -f vitest.config.ts && test -d
    # node_modules/vitest` — runnable, tautological, silent about the lesson —
    # draft a first-occurrence observation. Durability is decided here; the
    # command is still carried onto the draft and gated at approve time.
    #
    # Three things, and only three, buy a first occurrence past the n=2 rule: it
    # amends a skill that already exists, it has recurred, or its author wrote it
    # down as a `convention`. Nothing is inferred from how the prose is worded.
    should_draft = (
        bool(candidate.get("updates_existing_skill"))
        or is_recurrence
        or evidence_records_a_convention(candidate)
    )
    if not should_draft:
        return "park", "prose lesson, first occurrence (n=2 rule)"

    verified = False
    if verify_cmd and cfg.allow_auto_verify:
        result = run_verify_for(
            ws,
            verify_cmd,
            {"repos": _repos_of(ws, scope_features), "features": scope_features},
            cfg,
            under_review=True,
        )
        if not result.ok:
            candidate["verify_failure"] = result.output
            return "park", f"verify_command failed: {result.output[:200]}"
        verified = True

    draft_dir = render_draft(ws, candidate, features=scope_features)
    if verify_cmd:
        # Unverified drafts carry the command forward; `mgit skill approve
        # --run-verify` runs it under the human's eye, not in this distill process.
        update_meta(
            draft_dir,
            verify_pending=not verified,
            verified_at=memory.utc_now() if verified else None,
        )
    if is_recurrence:
        resolved_id = parked_id

        def _resolve(rows: list[dict]) -> list[dict]:
            for p in rows:
                if p.get("id") == resolved_id:
                    p["status"] = "resolved"
                    p["resolved_by"] = slug
            return rows

        _update_jsonl(_parked_path(ws), _resolve)
    return "draft", str(draft_dir)


def park_candidate(
    ws: Workspace, candidate: dict, note: str, journal_keys: set[str] | None = None
) -> None:
    slug = candidate.get("slug", "unnamed")
    # The journal as it stood when this lesson was last seen. A later candidate
    # earns its second occurrence only by citing an entry written after this.
    baseline = sorted(journal_keys) if journal_keys is not None else None
    row = {
        "id": slug,
        "created": memory.utc_now(),
        "note": note,
        "status": "parked",
        "journal_keys": baseline,
        **{
            k: candidate.get(k)
            for k in ("title", "trigger_description", "scope_level", "paths", "steps",
                      "evidence", "verify_command")
        },
    }

    def _merge(rows: list[dict]) -> list[dict]:
        for existing in rows:
            # A recurring lesson must not accumulate one parked row per run.
            if existing.get("id") == slug and existing.get("status") != "resolved":
                existing["note"] = note
                existing["last_seen"] = memory.utc_now()
                if baseline is not None:
                    existing["journal_keys"] = baseline
                return rows
        rows.append(row)
        return rows

    _update_jsonl(_parked_path(ws), _merge)


# --- scanning ---------------------------------------------------------------

def parse_skill(skill_dir: Path) -> SkillInfo | None:
    if not (skill_dir / SKILL_MD).is_file():
        return None
    meta = read_meta(skill_dir)
    return SkillInfo(
        slug=meta.get("slug", skill_dir.name),
        title=meta.get("title", ""),
        description=meta.get("description", ""),
        kind=meta.get("kind", ""),
        scope_level=meta.get("scope_level", "workspace"),
        status=meta.get("status", "draft"),
        paths=list(meta.get("paths", [])),
        watched_paths=list(meta.get("watched_paths", [])),
        verify_command=meta.get("verify_command"),
        verify_pending=bool(meta.get("verify_pending", False)),
        verified_at=meta.get("verified_at"),
        updates=meta.get("updates"),
        features=list(meta.get("features", [])),
        created_at=meta.get("created_at", ""),
        approved_at=meta.get("approved_at", ""),
    )


def _scan_dir(base: Path) -> list[SkillInfo]:
    if not base.is_dir():
        return []
    out = []
    for skill_md in sorted(base.glob("*/SKILL.md")):
        parsed = parse_skill(skill_md.parent)
        if parsed:
            out.append(parsed)
    return out


def list_drafts(ws: Workspace) -> list[SkillInfo]:
    return _scan_dir(_drafts_dir(ws))


def scan_active(ws: Workspace) -> list[SkillInfo]:
    return _scan_dir(_active_dir(ws))


def find_active_dir(ws: Workspace, name: str) -> Path | None:
    candidate = _active_dir(ws) / name
    if (candidate / SKILL_MD).is_file():
        return candidate
    for skill in scan_active(ws):
        if skill.slug == name or skill.title == name:
            return _active_dir(ws) / skill.slug
    return None


# --- review -----------------------------------------------------------------

class SkillManager:
    """Foreground skill operations: distill, review, approve, reject, doctor."""

    def __init__(self, ws: Workspace, cfg: SkillConfig | None = None):
        self.ws = ws
        self.cfg = cfg or load_skill_config(ws)

    # -- distill --
    def distill(self, feature: str | None = None, dry_run: bool = False) -> list[str]:
        ws, cfg = self.ws, self.cfg
        events = gather_evidence(ws, feature)
        if not events:
            scope = f"feature '{feature}'" if feature else "any feature"
            return [f"no durable steering found in {scope}; nothing to distill"]
        scope_features = sorted({e["feature"] for e in events})
        existing = scan_active(ws)
        parked = _read_jsonl(_parked_path(ws))
        tombstones = _read_jsonl(_tombstones_path(ws))
        prompt = build_prompt(events, existing, parked, tombstones)
        if dry_run:
            return ["--- dry-run distiller prompt ---", prompt]

        structured, diag = llm.invoke_structured(
            prompt,
            CANDIDATES_SCHEMA,
            claude_bin=cfg.claude_bin,
            model=cfg.model,
            cwd=ws.root,
            timeout=cfg.distill_timeout,
        )
        if structured is None:
            raise MgitError(f"distill failed: {diag}")
        candidates = structured.get("candidates") or []
        _ledger_append(ws, {"type": "distill_run", "features": scope_features,
                            "candidates": len(candidates)})
        results: list[str] = []
        # Re-read parked per candidate so recurrence resolution within one run is seen.
        corpus = journal_corpus(events)
        for candidate in candidates:
            parked = _read_jsonl(_parked_path(ws))
            ground_evidence(candidate, events)
            action, detail = route_candidate(ws, candidate, parked, cfg, scope_features)
            if action == "park":
                park_candidate(ws, candidate, detail, journal_keys=corpus)
            _ledger_append(ws, {"type": action, "slug": candidate.get("slug"),
                                "detail": detail[:300]})
            results.append(f"{candidate.get('slug')}: {action} ({detail[:120]})")
        if not candidates:
            results.append("no durable lessons found")
        return results

    # -- approve --
    def approve(self, slug: str) -> Path:
        ws = self.ws
        draft_dir = _drafts_dir(ws) / slug
        if not (draft_dir / SKILL_MD).is_file():
            raise SkillNotFoundError(f"no skill draft named '{slug}'")
        meta = read_meta(draft_dir)
        updates = meta.get("updates")

        dest: Path | None = None
        if updates:
            # An amendment lands on the skill it names, not at active/<slug> —
            # which would install a duplicate sibling instead of amending.
            dest = find_active_dir(ws, str(updates))
        if dest is None:
            dest = _active_dir(ws) / slug

        if dest.exists():
            # Consult the destination's provenance; the draft's own meta always
            # says status="draft", so a draft-side check could never refuse.
            dest_managed = bool(read_meta(dest))
            if not (updates or dest_managed):
                raise MgitError(f"skill '{dest.name}' already exists and was not created by mgit")
            backup = ws.skills_dir / "replaced" / f"{dest.name}-{memory.utc_now().replace(':', '')}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(backup))

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(draft_dir), str(dest))
        update_meta(dest, status="active", approved_at=memory.utc_now())
        _ledger_append(ws, {"type": "approve", "slug": slug, "dest": str(dest)})
        _regen_all_briefs(ws)
        return dest

    # -- reject --
    def reject(self, slug: str, reason: str) -> None:
        ws = self.ws
        draft_dir = _drafts_dir(ws) / slug
        if not draft_dir.is_dir():
            raise SkillNotFoundError(f"no skill draft named '{slug}'")
        _append_jsonl(_tombstones_path(ws), {"ts": memory.utc_now(), "slug": slug, "reason": reason})
        _ledger_append(ws, {"type": "reject", "slug": slug, "reason": reason})
        shutil.rmtree(draft_dir)

    def pending_verify(self, slug: str) -> tuple[str, Path] | None:
        """The (command, cwd) a --run-verify would execute, without running it.

        The command is written by an LLM from journal text, so the human must be
        able to read it before it runs as shell in their repo.
        """
        ws = self.ws
        meta = read_meta(_drafts_dir(ws) / slug)
        command = runnable_verify(meta.get("verify_command"))
        if not command or not meta.get("verify_pending"):
            return None
        return command, verify_cwd(ws, meta)

    # -- run a draft's pending verify with the human present --
    def run_pending_verify(self, slug: str) -> VerifyResult | None:
        ws, cfg = self.ws, self.cfg
        draft_dir = _drafts_dir(ws) / slug
        meta = read_meta(draft_dir)
        command = meta.get("verify_command")
        if not command or not meta.get("verify_pending"):
            return None
        # A draft is under review: verify the branch the lesson was learned on.
        result = run_verify_for(ws, str(command), meta, cfg, under_review=True)
        if result.ok:
            update_meta(draft_dir, verify_pending=False, verified_at=memory.utc_now())
        return result

    # -- re-run an ACTIVE skill's verify against the repo as it is now --
    def verify_active(self, slug: str | None = None) -> list[tuple[str, VerifyResult]]:
        """Re-verify approved skills. An active skill is a standing claim about
        the repo, so it is checked against HEAD, not against the branch it was
        learned on."""
        ws, cfg = self.ws, self.cfg
        skills = scan_active(ws)
        if slug is not None:
            skills = [s for s in skills if s.slug == slug]
            if not skills:
                raise SkillNotFoundError(f"no active skill named '{slug}'")

        results: list[tuple[str, VerifyResult]] = []
        for s in skills:
            if not s.verify_command:
                continue
            skill_dir = _active_dir(ws) / s.slug
            meta = read_meta(skill_dir)
            result = run_verify_for(ws, s.verify_command, meta, cfg, under_review=False)
            if result.ok:
                update_meta(skill_dir, verify_pending=False, verified_at=memory.utc_now())
            _ledger_append(ws, {"type": "verify", "slug": s.slug, "ok": result.ok})
            results.append((s.slug, result))
        return results

    def pending_active_verify(self, slug: str | None = None) -> list[tuple[str, str, Path]]:
        """(slug, command, tree) for each active skill `verify_active` would RUN.

        Skills whose command is not runnable are excluded: they are reported as
        failures without executing, so there is nothing to consent to.
        """
        ws = self.ws
        out = []
        for s in scan_active(ws):
            if slug is not None and s.slug != slug:
                continue
            command = runnable_verify(s.verify_command)
            if not command:
                continue
            meta = read_meta(_active_dir(ws) / s.slug)
            out.append((s.slug, command, verify_cwd(ws, meta)))
        return out

    def broken_active_verify(self, slug: str | None = None) -> list[str]:
        """Active skills carrying a verify_command that can never pass."""
        return [
            s.slug
            for s in scan_active(self.ws)
            if (slug is None or s.slug == slug)
            and s.verify_command
            and runnable_verify(s.verify_command) is None
        ]

    # -- doctor --
    def doctor(self) -> list[str]:
        ws = self.ws
        lines: list[str] = []
        active = scan_active(ws)
        drafts = list_drafts(ws)
        parked = [p for p in _read_jsonl(_parked_path(ws)) if p.get("status") == "parked"]
        lines.append(f"active skills: {len(active)}")
        for s in active:
            if s.verify_command and runnable_verify(s.verify_command) is None:
                lines.append(
                    f"  {s.slug}: verify_command has unsubstituted {{placeholders}} and can "
                    f"never pass — re-distill it, or clear the command in its skill.toml"
                )
                continue
            if s.verify_pending:
                remedy = f" — run: mgit skill verify {s.slug}" if s.verify_command else ""
                lines.append(f"  {s.slug}: approved but verify never ran{remedy}")
                continue
            changed = watched_changes(ws, read_meta(_active_dir(ws) / s.slug))
            if changed:
                lines.append(
                    f"  {s.slug}: {changed} commit(s) touched its watched paths since "
                    f"it was verified — run: mgit skill verify {s.slug}"
                )
        lines.append(f"drafts awaiting review: {len(drafts)}")
        for d in drafts:
            lines.append(f"  {d.slug} ({d.scope_level})")
        lines.append(f"parked (n=2 rule): {len(parked)}")
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=PARKED_MAX_AGE_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        old = [p for p in parked if str(p.get("created", "")) < cutoff]
        if old:
            lines.append(
                f"  {len(old)} parked > {PARKED_MAX_AGE_DAYS} days (likely one-offs): "
                + ", ".join(str(p.get("id")) for p in old[:10])
            )
        return lines


# --- ambient brief integration ----------------------------------------------

def render_active_block(ws: Workspace) -> str:
    """A markdown section listing active skills, for the worktree brief.

    Empty string when there are no active skills, so the ambient brief does not
    grow a hollow section on day one.
    """
    active = scan_active(ws)
    if not active:
        return ""
    lines = [
        "## Learned skills (mgit)",
        "_Distilled from steering across features; full definitions in "
        "`.mgit/skills/active/<slug>/SKILL.md`._",
        "",
    ]
    for s in active:
        scope = s.scope_level
        note = f"{scope}: {', '.join(s.paths)}" if scope == "feature" and s.paths else scope
        lines.append(f"- **{s.slug}** ({note}) — {s.description}")
    return "\n".join(lines) + "\n"


def _regen_all_briefs(ws: Workspace) -> None:
    """Refresh every feature's ambient brief so the learned-skills block updates.

    Guarded: a brief-render failure must never break skill approval.
    """
    try:
        from mgit.core import brief, config

        for path in ws.features_dir.glob("*.toml"):
            try:
                feature = config.dict_to_feature(config.read_toml(path))
            except (OSError, KeyError, ValueError):
                continue
            brief.write_feature_brief(ws, feature)
    except Exception:
        pass
