"""Tests for the skill distillation subsystem (mgit skill)."""

import json
import subprocess
from pathlib import Path

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
    # explicit_rule only counts when a verbatim quote states the rule
    candidate = _candidate(explicit_rule=True, evidence=[{"quote": "always run the seed script first, no exceptions", "kind": "decision"}])
    action, _ = skill.route_candidate(ws, candidate, [], SkillConfig(), ["f"])
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
        ws,
        _candidate(verify_command=f"touch {marker}", explicit_rule=True, evidence=[{"quote": "always run the seed script first, no exceptions", "kind": "decision"}]),
        [],
        SkillConfig(),
        ["f"],
    )
    assert action == "draft"
    assert not marker.exists(), "distiller executed an unreviewed model command"
    meta = skill.read_meta(ws.skills_dir / "drafts" / "seed-before-e2e")
    assert meta["verify_pending"] is True
    assert "verified_at" not in meta


def test_verify_command_runs_when_opted_in(initialized_workspace):
    ws = initialized_workspace
    cfg = SkillConfig(allow_auto_verify=True)
    candidate = _candidate(verify_command="true", explicit_rule=True, evidence=[{"quote": "always run the seed script first, no exceptions", "kind": "decision"}])
    action, _ = skill.route_candidate(ws, candidate, [], cfg, ["f"])
    assert action == "draft"
    meta = skill.read_meta(ws.skills_dir / "drafts" / "seed-before-e2e")
    assert meta["verify_pending"] is False
    assert meta["verified_at"]


def test_verify_fail_parks_when_opted_in(initialized_workspace):
    ws = initialized_workspace
    cfg = SkillConfig(allow_auto_verify=True)
    candidate = _candidate(verify_command="false", explicit_rule=True, evidence=[{"quote": "always run the seed script first, no exceptions", "kind": "decision"}])
    action, detail = skill.route_candidate(ws, candidate, [], cfg, ["f"])
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
    skill.render_draft(ws, _candidate(explicit_rule=True, evidence=[{"quote": "always run the seed script first, no exceptions", "kind": "decision"}]), features=[name])
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
        _candidate(
            slug="keep-me",
            explicit_rule=True,
            evidence=[{"quote": "never skip the seed script", "kind": "decision"}],
        ),
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


class TestEvidenceTypesAreWritable:
    """The distiller harvested a journal type nobody could write.

    EVIDENCE_TYPES listed "convention" while memory.NOTE_TYPES did not, so
    `mgit feature note --type convention` was rejected by click and the
    distiller's convention branch was unreachable.
    """

    def test_every_harvested_type_can_be_written(self):
        from mgit.core import memory, skill

        unwritable = set(skill.EVIDENCE_TYPES) - set(memory.NOTE_TYPES)
        assert not unwritable, f"distiller harvests types no CLI can emit: {unwritable}"

    def test_convention_notes_reach_the_distiller(self, initialized_workspace, monkeypatch):
        from click.testing import CliRunner

        from mgit.cli import main
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        FeatureManager(ws).start("feat", ["repo-a"])

        result = CliRunner().invoke(
            main, ["feature", "note", "always guard property reads", "--type", "convention"]
        )
        assert result.exit_code == 0, result.output

        evidence = skill.gather_evidence(ws)
        assert any(e["type"] == "convention" for e in evidence), evidence


class TestVerifyRunsInTheRepo:
    """A verify_command is written against a repo's layout ("npx vitest run
    src/..."). Running it at the workspace root — a directory of symlinks and
    worktrees — could sweep every repo at once and pass while the real target
    failed."""

    def test_verify_cwd_is_the_single_source_repo(self, initialized_workspace):
        from mgit.core import skill

        ws = initialized_workspace
        cwd = skill.verify_cwd(ws, {"repos": ["repo-a"]})
        assert cwd == ws.repo_path("repo-a")
        assert cwd != ws.root

    def test_verify_cwd_falls_back_to_root_when_ambiguous(self, initialized_workspace):
        from mgit.core import skill

        ws = initialized_workspace
        assert skill.verify_cwd(ws, {"repos": ["repo-a", "repo-b"]}) == ws.root
        assert skill.verify_cwd(ws, {"repos": []}) == ws.root
        assert skill.verify_cwd(ws, {}) == ws.root

    def test_verify_cwd_ignores_unregistered_repos(self, initialized_workspace):
        from mgit.core import skill

        ws = initialized_workspace
        assert skill.verify_cwd(ws, {"repos": ["repo-a", "gone"]}) == ws.repo_path("repo-a")

    def test_legacy_draft_without_repos_resolves_via_its_features(self, initialized_workspace):
        """Drafts written before `repos` existed must not fall back to the root."""
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        ws = initialized_workspace
        FeatureManager(ws).start("feat", ["repo-a"])
        assert skill.verify_cwd(ws, {"features": ["feat"]}) == ws.repo_path("repo-a")
        assert skill.verify_cwd(ws, {"features": ["feat"]}) != ws.root

    def test_draft_records_the_repos_it_came_from(self, initialized_workspace):
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        ws = initialized_workspace
        FeatureManager(ws).start("feat", ["repo-a"])
        draft = skill.render_draft(
            ws,
            {
                "slug": "some-rule",
                "kind": "durable-convention",
                "scope_level": "feature",
                "trigger_description": "t",
                "anti_triggers": ["never"],
                "steps": ["do it"],
                "verify_command": "true",
                "evidence": [{"quote": "q", "kind": "decision"}],
            },
            features=["feat"],
        )
        assert skill.read_meta(draft)["repos"] == ["repo-a"]

    def test_pending_verify_runs_in_the_repo_not_the_root(self, initialized_workspace):
        from mgit.core import skill
        from mgit.core.feature import FeatureManager
        from mgit.core.skill import SkillManager

        ws = initialized_workspace
        FeatureManager(ws).start("feat", ["repo-a"])
        # A committed marker that exists only inside repo-a, so `test -f` can
        # only pass when the command runs against that repo (in a clone of it).
        repo = ws.repo_path("repo-a")
        (repo / "only-here.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "marker"], cwd=repo, check=True)
        skill.render_draft(
            ws,
            {
                "slug": "cwd-rule",
                "kind": "durable-convention",
                "scope_level": "feature",
                "trigger_description": "t",
                "anti_triggers": ["never"],
                "steps": ["do it"],
                "verify_command": "test -f only-here.txt",
                "evidence": [{"quote": "q", "kind": "decision"}],
            },
            features=["feat"],
        )
        skill.update_meta(ws.skills_dir / "drafts" / "cwd-rule", verify_pending=True)

        result = SkillManager(ws).run_pending_verify("cwd-rule")
        assert result.ok, "verify must run against repo-a, where the marker lives"
        assert result.tree == ws.repo_path("repo-a")


class TestVerifyCommandNeedsConsent:
    """`--run-verify` executes LLM-authored shell in a real repo. One live
    distiller run wrote `git stash push … ; git stash pop`, which in a repo with
    an unrelated stash would have popped it into the working tree."""

    def _draft(self, ws, command):
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        FeatureManager(ws).start("feat", ["repo-a"])
        skill.render_draft(
            ws,
            {
                "slug": "risky",
                "kind": "durable-procedure",
                "scope_level": "feature",
                "trigger_description": "t",
                "anti_triggers": ["never"],
                "steps": ["s"],
                "verify_command": command,
                "evidence": [{"quote": "q", "kind": "decision"}],
            },
            features=["feat"],
        )
        skill.update_meta(ws.skills_dir / "drafts" / "risky", verify_pending=True)

    def test_pending_verify_exposes_the_command_without_running_it(self, initialized_workspace):
        from mgit.core.skill import SkillManager

        ws = initialized_workspace
        marker = ws.root / "ran.txt"
        self._draft(ws, f"touch {marker}")

        command, cwd = SkillManager(ws).pending_verify("risky")
        assert "touch" in command
        assert cwd == ws.repo_path("repo-a")
        assert not marker.exists(), "inspecting the command must not execute it"

    def test_non_tty_refuses_without_yes_and_does_not_execute(
        self, initialized_workspace, monkeypatch
    ):
        from click.testing import CliRunner

        from mgit.cli import main

        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        marker = ws.root / "ran.txt"
        self._draft(ws, f"touch {marker}")

        result = CliRunner().invoke(main, ["skill", "approve", "risky", "--run-verify"])
        assert result.exit_code != 0
        assert not marker.exists(), "command must not run without consent"
        assert (ws.skills_dir / "drafts" / "risky").is_dir(), "draft must survive"

    def test_json_mode_does_not_imply_consent(self, initialized_workspace, monkeypatch):
        from click.testing import CliRunner

        from mgit.cli import main

        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        marker = ws.root / "ran.txt"
        self._draft(ws, f"touch {marker}")

        result = CliRunner().invoke(
            main, ["skill", "approve", "risky", "--run-verify", "--json"]
        )
        assert result.exit_code != 0
        assert not marker.exists()

    def test_yes_executes_but_the_repo_is_never_touched(self, initialized_workspace, monkeypatch):
        from click.testing import CliRunner

        from mgit.cli import main

        ws = initialized_workspace
        monkeypatch.chdir(ws.root)
        marker = ws.repo_path("repo-a") / "ran.txt"
        # exits 0 (so approval proceeds) and writes a file (which must not survive)
        self._draft(ws, "touch ran.txt")

        result = CliRunner().invoke(
            main, ["skill", "approve", "risky", "--run-verify", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert (ws.skills_dir / "active" / "risky").is_dir(), "verify passed, so it approves"
        assert not marker.exists(), "the command ran in a throwaway clone, not the repo"


class TestVerifyCommandDoesNotRoute:
    """A verify_command says a skill is CHECKABLE; the n=2 rule asks whether the
    lesson is DURABLE. It used to answer the second question with the first.

    Live runs showed both failure modes: `cd {project-root} && npx vitest run
    {test-file-path}` (never exits 0, proves nothing) and `test -f
    vitest.config.ts && test -d node_modules/vitest` (runnable, tautological,
    silent about the lesson). Both drafted first-occurrence observations.
    """

    def test_placeholder_commands_are_not_runnable(self):
        from mgit.core.skill import runnable_verify

        assert runnable_verify("cd {project-root} && npx vitest run {test-file-path}") is None
        assert runnable_verify("npx vitest run {test_file}") is None
        assert runnable_verify(None) is None
        assert runnable_verify("   ") is None

    def test_real_shell_is_not_mistaken_for_a_placeholder(self):
        from mgit.core.skill import runnable_verify

        for cmd in [
            "npx tsc --noEmit && npx eslint",
            "echo ${HOME}",
            "awk '{print $1}' file",
            "printf '%s\\n' {1..3}",
            "grep -qE 'Test Files.*\\(1\\)'",
        ]:
            assert runnable_verify(cmd) == cmd.strip(), cmd

    def _candidate(self, **over):
        c = {
            "slug": "prose-lesson",
            "kind": "durable-convention",
            "scope_level": "global",
            "trigger_description": "t",
            "anti_triggers": ["never"],
            "steps": ["s"],
            "evidence": [{"quote": "q", "kind": "note"}],
            "explicit_rule": False,
            "recurrence_of": None,
            "updates_existing_skill": None,
        }
        c.update(over)
        return c

    def test_a_placeholder_command_is_stripped_from_the_candidate(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        candidate = self._candidate(verify_command="cd {project-root} && npx vitest run {t-f}")
        action, detail = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
        assert action == "park", detail
        assert candidate["verify_command"] is None, "placeholder command must be dropped"

    def test_even_a_real_verify_does_not_buy_a_draft(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        candidate = self._candidate(verify_command="npx tsc --noEmit")
        action, detail = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
        assert action == "park", detail
        # ...but the command is carried onto the parked candidate for later
        assert candidate["verify_command"] == "npx tsc --noEmit"

    def test_a_recurrence_with_a_verify_still_drafts(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        parked = [{"id": "prose-lesson", "status": "parked"}]
        candidate = self._candidate(verify_command="npx tsc --noEmit")
        action, _ = route_candidate(ws, candidate, parked, SkillConfig(), ["feat"])
        assert action == "draft"


class TestExplicitRuleMustRestOnEvidence:
    """`explicit_rule` exempts a candidate from the n=2 rule. It is the model's
    own claim about its evidence, and across five live distills the model set it
    for design rationales -- eight candidates, none ever parked."""

    def _candidate(self, quotes, **over):
        c = {
            "slug": "some-lesson",
            "kind": "durable-convention",
            "scope_level": "feature",
            "trigger_description": "t",
            "anti_triggers": ["never do x"],  # anti_triggers must not count
            "steps": ["s"],
            "evidence": [{"quote": q, "kind": "note"} for q in quotes],
            "explicit_rule": True,
            "recurrence_of": None,
            "updates_existing_skill": None,
            "verify_command": None,
        }
        c.update(over)
        return c

    def test_rationale_without_rule_language_parks(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        candidate = self._candidate(
            ["identity should be keyed by the invocation, not by the variable's name"]
        )
        action, detail = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
        assert action == "park", detail
        assert candidate["explicit_rule"] is False

    def test_a_real_stated_rule_still_drafts(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        for quote in [
            "snapshot() must never throw on user values",
            "Hard rule for src/engine/: capture visible() first",
            "always run tsc before committing, no exceptions",
            "do not commit on a green vitest alone",
        ]:
            candidate = self._candidate([quote])
            action, _ = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
            assert action == "draft", quote
            assert candidate["explicit_rule"] is True

    def test_anti_triggers_do_not_count_as_evidence_of_a_rule(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        # "never do x" lives in anti_triggers, not in a verbatim evidence quote
        candidate = self._candidate(["we discussed keying by callId"])
        action, _ = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
        assert action == "park"

    def test_a_runnable_verify_no_longer_rescues_a_rationale(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, route_candidate

        ws = initialized_workspace
        candidate = self._candidate(["we settled on this"], verify_command="npx tsc --noEmit")
        action, _ = route_candidate(ws, candidate, [], SkillConfig(), ["feat"])
        assert action == "park"


class TestVerifySandboxIsolatesGitState:
    """The verify_command is LLM-authored shell. One live run produced
    `git stash push … ; git stash pop`, which would have popped an unrelated
    stash into the working tree.

    A `git worktree` does NOT isolate this: linked worktrees share refs/stash,
    so `git stash pop` inside one consumes the real stash. A local clone gets
    its own refs, so it does.
    """

    def _stash_something(self, repo):
        (repo / "README.md").write_text("PRECIOUS uncommitted work")
        subprocess.run(["git", "stash", "push", "-q", "-m", "precious"], cwd=repo, check=True)

    def test_a_stash_popping_command_cannot_touch_the_real_stash(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        repo = ws.repo_path("repo-a")
        self._stash_something(repo)
        before = subprocess.run(
            ["git", "stash", "list"], cwd=repo, capture_output=True, text=True
        ).stdout

        result = run_verify_for(
            ws, "git stash pop -q || true", {"repos": ["repo-a"]}, SkillConfig()
        )
        assert result.ok
        assert result.tree == repo, "it reports the tree it validated"

        after = subprocess.run(
            ["git", "stash", "list"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert after == before, "the real stash survived"
        assert "precious" in after

    def test_a_destructive_command_cannot_touch_the_working_tree(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        repo = ws.repo_path("repo-a")
        assert (repo / "README.md").exists()

        result = run_verify_for(
            ws, "rm -rf README.md && git checkout --orphan wiped", {"repos": ["repo-a"]}, SkillConfig()
        )
        assert result.ok
        assert (repo / "README.md").exists(), "the file survived; the command hit a clone"
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        assert branch != "wiped", "the real repo's HEAD was not moved"

    def test_the_sandbox_sees_the_repo_at_head(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        result = run_verify_for(ws, "test -f README.md", {"repos": ["repo-a"]}, SkillConfig())
        assert result.ok, "committed content is present in the sandbox"

    def test_the_sandbox_is_removed_afterwards(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        result = run_verify_for(ws, "pwd", {"repos": ["repo-a"]}, SkillConfig())
        assert result.ok
        sandbox = result.output.strip().splitlines()[-1]
        assert not Path(sandbox).exists(), f"sandbox {sandbox} outlived the verify"

    def test_the_repo_setup_hook_provisions_the_sandbox(self, initialized_workspace):
        """A clone has no ignored files, so dependencies must be re-provided."""
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        info = ws.get_repo("repo-a")
        info.setup = "mkdir -p node_modules && touch node_modules/.installed"
        ws.save()

        result = run_verify_for(
            ws, "test -f node_modules/.installed", {"repos": ["repo-a"]}, SkillConfig()
        )
        assert result.ok, "the repo's setup hook ran inside the sandbox"
        assert not (ws.repo_path("repo-a") / "node_modules").exists()


class TestVerifyValidatesTheRightTree:
    """A draft's evidence lives on the branch the lesson was learned on, and that
    branch usually carries files `main` has never seen. Verifying a draft against
    `main` fails for reasons that have nothing to do with the skill. An ACTIVE
    skill is a standing claim about the repo now, so it verifies against HEAD."""

    def _feature_with_a_new_file(self, ws, feature="feat", repo="repo-a"):
        from mgit.core.feature import FeatureManager

        fm = FeatureManager(ws)
        fm.start(feature, [repo])
        wt = fm.materialize(feature, repo)
        (wt / "only-on-branch.txt").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-qm", "branch-only file"], cwd=wt, check=True)
        return fm

    def test_a_draft_verifies_the_branch_it_was_learned_on(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        self._feature_with_a_new_file(ws)
        meta = {"repos": ["repo-a"], "features": ["feat"]}

        under_review = run_verify_for(
            ws, "test -f only-on-branch.txt", meta, SkillConfig(), under_review=True
        )
        assert under_review.ok, "the draft's own branch has the file"
        assert under_review.ref == "mgit/feat"

    def test_an_active_skill_verifies_head(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        self._feature_with_a_new_file(ws)
        meta = {"repos": ["repo-a"], "features": ["feat"]}

        active = run_verify_for(
            ws, "test -f only-on-branch.txt", meta, SkillConfig(), under_review=False
        )
        assert not active.ok, "the file is not merged into HEAD yet"
        assert active.ref == "HEAD"

    def test_falls_back_to_head_when_the_feature_is_gone(self, initialized_workspace):
        from mgit.core.skill import SkillConfig, run_verify_for

        ws = initialized_workspace
        fm = self._feature_with_a_new_file(ws)
        fm.delete("feat", force=True)  # branch deleted with the feature

        result = run_verify_for(
            ws, "test -f README.md", {"repos": ["repo-a"], "features": ["feat"]},
            SkillConfig(), under_review=True,
        )
        assert result.ok
        assert result.ref == "HEAD", "no branch left to verify against"


class TestSkillVerifyCommand:
    """There was no way to re-run an active skill's verify_command: `approve`
    resolves its slug against the drafts directory only."""

    def _active_skill(self, ws, command, watched=None):
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        FeatureManager(ws).start("feat", ["repo-a"])
        skill.render_draft(
            ws,
            {
                "slug": "checkable",
                "kind": "durable-procedure",
                "scope_level": "feature",
                "trigger_description": "t",
                "anti_triggers": ["never"],
                "steps": ["s"],
                "verify_command": command,
                "watched_paths": watched or [],
                "evidence": [{"quote": "q", "kind": "decision"}],
            },
            features=["feat"],
        )
        return SkillManager(ws).approve("checkable")

    def test_verify_active_reruns_and_stamps(self, initialized_workspace):
        ws = initialized_workspace
        dest = self._active_skill(ws, "test -f README.md")
        skill.update_meta(dest, verify_pending=True)

        results = SkillManager(ws).verify_active()
        assert [(s, r.ok) for s, r in results] == [("checkable", True)]
        meta = skill.read_meta(dest)
        assert meta["verify_pending"] is False
        assert meta["verified_at"]

    def test_verify_active_reports_failure_without_activating_anything(self, initialized_workspace):
        ws = initialized_workspace
        self._active_skill(ws, "test -f nope.txt")
        results = SkillManager(ws).verify_active()
        assert [(s, r.ok) for s, r in results] == [("checkable", False)]

    def test_verify_active_rejects_an_unknown_slug(self, initialized_workspace):
        ws = initialized_workspace
        self._active_skill(ws, "true")
        with pytest.raises(SkillNotFoundError):
            SkillManager(ws).verify_active("no-such-skill")

    def test_cli_verify_needs_consent_and_does_not_run(self, initialized_workspace, monkeypatch):
        from click.testing import CliRunner

        from mgit.cli import main

        ws = initialized_workspace
        marker = ws.root / "verify-ran.txt"
        self._active_skill(ws, f"touch {marker}")
        monkeypatch.chdir(ws.root)

        result = CliRunner().invoke(main, ["skill", "verify"])
        assert result.exit_code != 0
        assert not marker.exists()

    def test_cli_verify_with_yes_runs_and_exits_1_on_failure(
        self, initialized_workspace, monkeypatch
    ):
        from click.testing import CliRunner

        from mgit.cli import main

        ws = initialized_workspace
        self._active_skill(ws, "test -f nope.txt")
        monkeypatch.chdir(ws.root)

        result = CliRunner().invoke(main, ["skill", "verify", "--yes"])
        assert result.exit_code == 1, result.output  # bulk partial failure


class TestWatchedPathsDriveStaleness:
    """`watched_paths` was written to every skill.toml and read by nothing."""

    def test_no_change_means_not_stale(self, initialized_workspace):
        from mgit.core.skill import watched_changes

        ws = initialized_workspace
        meta = {"repos": ["repo-a"], "verified_at": "2099-01-01T00:00:00Z",
                "watched_paths": ["README.md"]}
        assert watched_changes(ws, meta) == 0

    def test_a_commit_touching_a_watched_path_makes_it_stale(self, initialized_workspace):
        from mgit.core.skill import watched_changes

        ws = initialized_workspace
        # repo-a's fixture commit touched README.md, and 1970 predates it
        meta = {"repos": ["repo-a"], "verified_at": "1970-01-01T00:00:00Z",
                "watched_paths": ["README.md"]}
        assert watched_changes(ws, meta) >= 1

    def test_a_commit_elsewhere_does_not(self, initialized_workspace):
        from mgit.core.skill import watched_changes

        ws = initialized_workspace
        meta = {"repos": ["repo-a"], "verified_at": "1970-01-01T00:00:00Z",
                "watched_paths": ["src/never-touched/**"]}
        assert watched_changes(ws, meta) == 0

    def test_missing_verified_at_or_paths_is_never_stale(self, initialized_workspace):
        from mgit.core.skill import watched_changes

        ws = initialized_workspace
        assert watched_changes(ws, {"repos": ["repo-a"], "watched_paths": ["README.md"]}) == 0
        assert watched_changes(ws, {"repos": ["repo-a"], "verified_at": "1970-01-01T00:00:00Z"}) == 0

    def test_doctor_surfaces_staleness_and_names_the_fix(self, initialized_workspace):
        from mgit.core import skill
        from mgit.core.feature import FeatureManager

        ws = initialized_workspace
        FeatureManager(ws).start("feat", ["repo-a"])
        dest = skill.render_draft(
            ws,
            {
                "slug": "watchful", "kind": "durable-procedure", "scope_level": "feature",
                "trigger_description": "t", "anti_triggers": ["never"], "steps": ["s"],
                "verify_command": "true", "watched_paths": ["README.md"],
                "evidence": [{"quote": "q", "kind": "decision"}],
            },
            features=["feat"],
        )
        skill.update_meta(dest, verify_pending=False, verified_at="1970-01-01T00:00:00Z")
        SkillManager(ws).approve("watchful")

        report = "\n".join(SkillManager(ws).doctor())
        assert "touched its watched paths" in report
        assert "mgit skill verify watchful" in report


def test_cli_verify_failure_with_no_output_still_exits_1(initialized_workspace, monkeypatch):
    """`test -f nope` prints nothing; formatting its "last line" was an IndexError
    that surfaced as exit 4 (internal error) instead of 1 (partial failure)."""
    from click.testing import CliRunner

    from mgit.cli import main
    from mgit.core.feature import FeatureManager

    ws = initialized_workspace
    FeatureManager(ws).start("feat", ["repo-a"])
    skill.render_draft(
        ws,
        {
            "slug": "silent-fail", "kind": "durable-procedure", "scope_level": "feature",
            "trigger_description": "t", "anti_triggers": ["never"], "steps": ["s"],
            "verify_command": "test -f definitely-not-here.txt",
            "evidence": [{"quote": "q", "kind": "decision"}],
        },
        features=["feat"],
    )
    SkillManager(ws).approve("silent-fail")
    monkeypatch.chdir(ws.root)

    result = CliRunner().invoke(main, ["skill", "verify", "--yes"])
    assert result.exit_code == 1, result.output
    assert "no output" in result.output
