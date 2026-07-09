"""Tests for the skill distillation subsystem (mgit skill)."""

import json

import pytest
from click.testing import CliRunner

from mgit.cli import cli as cli_entry
from mgit.cli import main
from mgit.core import memory, redact, skill
from mgit.core.feature import FeatureManager
from mgit.core.skill import SkillConfig, SkillManager
from mgit.utils.errors import MgitError, SkillNotFoundError


def _candidate(**over):
    c = {
        "slug": "seed-before-e2e",
        "kind": "durable-convention",
        "scope_level": "workspace",
        "paths": [],
        "title": "Seed before e2e",
        "trigger_description": "Run the seed script before end-to-end tests",
        "anti_triggers": ["running unit tests only"],
        "preconditions": [],
        "steps": ["Run ./scripts/seed.sh"],
        "verify_command": None,
        "concrete_example": "",
        "evidence": [{"quote": "no, run the seed script first", "kind": "decision"}],
        "explicit_rule": False,
        "recurrence_of": None,
        "updates_existing_skill": None,
        "watched_paths": [],
    }
    c.update(over)
    return c


def _feature_with_journal(ws, name="feat-x", entries=None):
    fm = FeatureManager(ws)
    fm.create(name)
    for text, type_ in (entries or [("no, run the seed script first", "decision")]):
        memory.append_event(ws, name, text, type_=type_, actor="agent")
    return name


def _stub_claude(ws, structured):
    """Install a fake `claude` binary that prints a canned JSON envelope."""
    from mgit.core import config

    payload = ws.root / ".claude-payload.json"
    payload.write_text(json.dumps({"result": "", "structured_output": structured}))
    stub = ws.root / ".claude-stub"
    stub.write_text(f"#!/bin/sh\ncat {payload}\n")
    stub.chmod(0o755)
    data = config.read_toml(ws.config_path)
    data["skills"] = {"claude_bin": str(stub)}
    config.write_toml(ws.config_path, data)
    return str(stub)


# --- redaction --------------------------------------------------------------

def test_scrub_bearer_and_quoted_values():
    token = "s3cr3tOpaqueTokenValue123456"
    assert token not in redact.scrub(f"Authorization: Bearer {token}")
    out = redact.scrub('{"password": "hunter2", "user": "bob"}')
    assert "hunter2" not in out and "bob" in out


def test_scrub_preserves_git_shas():
    sha = "a" * 40
    text = f"fixed in {sha}, run pytest"
    assert redact.scrub(text) == text


# --- evidence gathering -----------------------------------------------------

def test_gather_evidence_filters_and_scrubs(initialized_workspace):
    ws = initialized_workspace
    name = _feature_with_journal(ws, entries=[
        ("always use API_KEY=sk-abcdefghijklmnopqrst here", "decision"),
        ("committed the fix", "note"),
    ])
    # mgit-authored lifecycle events must not count as steering.
    memory.append_event(ws, name, "commit", type_="event", event="commit")
    events = skill.gather_evidence(ws)
    texts = [e["text"] for e in events]
    assert len(events) == 2  # decision + note, not the event
    assert all("sk-abcdefghijklmnopqrst" not in t for t in texts)


# --- routing gates ----------------------------------------------------------

def test_gate_one_off_dropped(initialized_workspace):
    ws = initialized_workspace
    action, _ = skill.route_candidate(ws, _candidate(kind="one-off"), [], SkillConfig(), ["f"])
    assert action == "drop"


def test_gate_prose_first_occurrence_parks(initialized_workspace):
    ws = initialized_workspace
    action, _ = skill.route_candidate(ws, _candidate(), [], SkillConfig(), ["f"])
    assert action == "park"


def test_gate_explicit_rule_drafts(initialized_workspace):
    ws = initialized_workspace
    action, _ = skill.route_candidate(ws, _candidate(explicit_rule=True), [], SkillConfig(), ["f"])
    assert action == "draft"
    assert (ws.skills_dir / "drafts" / "seed-before-e2e" / "SKILL.md").is_file()


def test_gate_invalid_slug_dropped(initialized_workspace):
    ws = initialized_workspace
    action, detail = skill.route_candidate(
        ws, _candidate(slug="../../../../evil", explicit_rule=True), [], SkillConfig(), ["f"]
    )
    assert action == "drop" and "invalid slug" in detail
    assert not (ws.skills_dir / "active" / "evil").exists()


def test_render_draft_rejects_slug_escaping(initialized_workspace):
    ws = initialized_workspace
    with pytest.raises(MgitError, match="escapes"):
        skill.render_draft(ws, _candidate(slug="../../../evil"), features=["f"])


# --- verify-command safety (the RCE fix) ------------------------------------

def test_verify_command_not_executed_by_default(initialized_workspace, tmp_path):
    ws = initialized_workspace
    marker = tmp_path / "pwned"
    action, _ = skill.route_candidate(
        ws, _candidate(verify_command=f"touch {marker}"), [], SkillConfig(), ["f"]
    )
    assert action == "draft"
    assert not marker.exists(), "distiller executed an unreviewed model command"
    meta = skill.read_meta(ws.skills_dir / "drafts" / "seed-before-e2e")
    assert meta["verify_pending"] is True
    assert "verified_at" not in meta


def test_verify_command_runs_when_opted_in(initialized_workspace):
    ws = initialized_workspace
    cfg = SkillConfig(allow_auto_verify=True)
    action, _ = skill.route_candidate(ws, _candidate(verify_command="true"), [], cfg, ["f"])
    assert action == "draft"
    meta = skill.read_meta(ws.skills_dir / "drafts" / "seed-before-e2e")
    assert meta["verify_pending"] is False
    assert meta["verified_at"]


def test_verify_fail_parks_when_opted_in(initialized_workspace):
    ws = initialized_workspace
    cfg = SkillConfig(allow_auto_verify=True)
    action, detail = skill.route_candidate(ws, _candidate(verify_command="false"), [], cfg, ["f"])
    assert action == "park" and "verify_command failed" in detail


# --- parking dedup + recurrence ---------------------------------------------

def test_park_candidate_merges_instead_of_duplicating(initialized_workspace):
    ws = initialized_workspace
    for _ in range(3):
        skill.park_candidate(ws, _candidate(), "prose lesson")
    rows = skill._read_jsonl(skill._parked_path(ws))
    assert len(rows) == 1


def test_recurrence_promotes_and_resolves_parked(initialized_workspace):
    ws = initialized_workspace
    skill._append_jsonl(skill._parked_path(ws), {
        "id": "seed-before-e2e", "status": "parked", "created": memory.utc_now()
    })
    parked = skill._read_jsonl(skill._parked_path(ws))
    action, _ = skill.route_candidate(ws, _candidate(), parked, SkillConfig(), ["f"])
    assert action == "draft"
    assert skill._read_jsonl(skill._parked_path(ws))[0]["status"] == "resolved"


# --- review -----------------------------------------------------------------

def test_approve_activates_and_updates_ambient_brief(initialized_workspace):
    ws = initialized_workspace
    name = _feature_with_journal(ws)
    skill.render_draft(ws, _candidate(explicit_rule=True), features=[name])
    dest = SkillManager(ws).approve("seed-before-e2e")
    assert dest == ws.skills_dir / "active" / "seed-before-e2e"
    assert skill.read_meta(dest)["status"] == "active"
    # The learned-skills block reaches the ambient worktree brief.
    text = (ws.worktrees_dir / name / "CLAUDE.md").read_text()
    assert "Learned skills (mgit)" in text
    assert "seed-before-e2e" in text


def test_approve_amends_named_skill_not_a_sibling(initialized_workspace):
    ws = initialized_workspace
    existing = ws.skills_dir / "active" / "commit-style"
    skill.write_meta(existing, {"slug": "commit-style", "status": "active", "description": "old"})
    (existing / "SKILL.md").write_text("---\nname: commit-style\n---\nold\n")

    skill.render_draft(
        ws, _candidate(slug="commit-style-v2", updates_existing_skill="commit-style"),
        features=["f"],
    )
    dest = SkillManager(ws).approve("commit-style-v2")
    assert dest == existing
    assert not (ws.skills_dir / "active" / "commit-style-v2").exists()


def test_reject_tombstones_and_removes_draft(initialized_workspace):
    ws = initialized_workspace
    skill.render_draft(ws, _candidate(explicit_rule=True), features=["f"])
    SkillManager(ws).reject("seed-before-e2e", "too specific")
    assert not (ws.skills_dir / "drafts" / "seed-before-e2e").exists()
    tomb = skill._read_jsonl(skill._tombstones_path(ws))
    assert tomb and tomb[0]["slug"] == "seed-before-e2e"


def test_approve_missing_draft_raises(initialized_workspace):
    ws = initialized_workspace
    with pytest.raises(SkillNotFoundError):
        SkillManager(ws).approve("nope")


# --- distill end-to-end with a stub claude ----------------------------------

def test_distill_end_to_end_with_stub(initialized_workspace):
    ws = initialized_workspace
    _feature_with_journal(ws)
    _stub_claude(ws, {"candidates": [
        _candidate(slug="keep-me", explicit_rule=True),
        _candidate(slug="drop-me", kind="one-off"),
        _candidate(slug="park-me"),
    ]})
    results = SkillManager(ws).distill()
    joined = "\n".join(results)
    assert "keep-me: draft" in joined
    assert "drop-me: drop" in joined
    assert "park-me: park" in joined
    assert (ws.skills_dir / "drafts" / "keep-me" / "SKILL.md").is_file()
    ledger = skill._read_jsonl(skill._ledger_path(ws))
    assert any(e.get("type") == "distill_run" for e in ledger)


def test_distill_no_evidence_is_a_clean_noop(initialized_workspace):
    ws = initialized_workspace
    FeatureManager(ws).create("empty-feat")
    results = SkillManager(ws).distill()
    assert results == ["no durable steering found in any feature; nothing to distill"]


def test_distill_dry_run_emits_prompt(initialized_workspace):
    ws = initialized_workspace
    _feature_with_journal(ws)
    results = SkillManager(ws).distill(dry_run=True)
    assert results[0].startswith("--- dry-run")
    assert "no, run the seed script first" in "\n".join(results)


# --- CLI contract -----------------------------------------------------------

def test_cli_skill_list_json_envelope(initialized_workspace, monkeypatch):
    ws = initialized_workspace
    monkeypatch.chdir(ws.root)
    runner = CliRunner()
    result = runner.invoke(main, ["skill", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mgit_schema"] == 1
    assert payload["ok"] is True
    assert payload["command"] == "skill.list"
    assert payload["data"]["skills"] == []


def test_cli_skill_reject_missing_exits_2(initialized_workspace, monkeypatch, capsys):
    ws = initialized_workspace
    monkeypatch.chdir(ws.root)
    monkeypatch.setattr("sys.argv", ["mgit", "skill", "reject", "nope", "--reason", "x"])
    code = None
    try:
        cli_entry()
    except SystemExit as e:
        code = e.code
    assert code == 2  # domain error (MgitError) -> exit 2


class TestBuildDescription:
    """The description is what the ambient brief shows every session."""

    def test_situation_clause_anti_trigger_takes_the_prefix(self):
        desc = skill.build_description({
            "trigger_description": "Before publishing a feature.",
            "anti_triggers": ["the repo has no pytest suite."],
        })
        assert desc.endswith("Do not use when the repo has no pytest suite.")

    def test_imperative_anti_trigger_is_not_double_prefixed(self):
        # Verbatim from a live distiller run; the schema asks for a situation
        # clause but models answer with an imperative.
        desc = skill.build_description({
            "trigger_description": "Before publishing a feature.",
            "anti_triggers": ["Do not expand this into running the suite on every commit."],
        })
        assert "Do not use when Do not" not in desc
        assert desc.endswith("Do not expand this into running the suite on every commit.")

    @pytest.mark.parametrize("anti", ["Never publish on red.", "Don't do it.", "Avoid this."])
    def test_other_imperative_forms(self, anti):
        desc = skill.build_description({"trigger_description": "T.", "anti_triggers": [anti]})
        assert "Do not use when" not in desc

    def test_empty_anti_trigger_adds_no_dangling_clause(self):
        desc = skill.build_description({"trigger_description": "T.", "anti_triggers": [""]})
        assert desc == "T."

    def test_description_is_truncated(self):
        desc = skill.build_description({"trigger_description": "x" * 2000, "anti_triggers": []})
        assert len(desc) <= skill.MAX_DESCRIPTION_CHARS
