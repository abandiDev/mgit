#!/usr/bin/env bash
# End-to-end test for skill synthesis — mgit dogfooding mgit.
#
# Creates an mgit workspace whose enrolled repo is a clone of THIS mgit repo,
# records realistic development steering into working memory, and drives the
# full skill lifecycle against the real mgit binary:
#
#   distill -> park (n=1 prose) -> recurrence promotes (n=2) -> draft ->
#   approve -> ambient brief; plus reject/tombstone, and the verify-command
#   safety gate (never executed headlessly; runs only at approve --run-verify).
#
# The only faked boundary is the `claude` CLI: a stub returns canned structured
# candidates, so the pipeline is exercised deterministically with no network.
# Everything else is the real mgit managing a real mgit checkout.
#
# Usage: scripts/e2e-skill.sh        (uses .venv/bin/mgit, else mgit on PATH)
#        MGIT=/path/to/mgit scripts/e2e-skill.sh
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MGIT="${MGIT:-$ROOT/.venv/bin/mgit}"
if [ ! -x "$MGIT" ]; then
    MGIT="$(command -v mgit || true)"
fi
if [ -z "$MGIT" ]; then
    echo "mgit binary not found (set MGIT=...)" >&2
    exit 1
fi
if [ -x "$(dirname "$MGIT")/python" ]; then
    PY="${PY:-$(dirname "$MGIT")/python}"
else
    PY="${PY:-python3}"
fi

SANDBOX="$(mktemp -d -t mgit-e2e-skill-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
WS="$SANDBOX/workspace"
mkdir -p "$WS"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }

check() {  # check <desc> <cmd...>  — passes if the command exits 0
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$desc"; else fail "$desc"; fi
}
expect_exit() {  # expect_exit <desc> <want-code> <cmd...>
    local desc="$1" want="$2"; shift 2
    "$@" >/dev/null 2>&1
    local got=$?
    if [ "$got" -eq "$want" ]; then ok "$desc"; else fail "$desc (exit $got, wanted $want)"; fi
}
json_check() {  # json_check <desc> <python-expr over obj from stdin>
    local desc="$1" expr="$2"
    if $PY -c "import sys, json; obj = json.load(sys.stdin); sys.exit(0 if ($expr) else 1)" 2>/dev/null; then
        ok "$desc"
    else
        fail "$desc"
    fi
}

# Rewrite the stub claude's canned response, so successive distills can return
# different candidates (park -> recurrence).
set_candidates() {  # set_candidates <json-array-of-candidates>
    cat > "$SANDBOX/claude_payload.json" <<EOF
{"result": "", "structured_output": {"candidates": $1}}
EOF
}

# ---------- setup: mgit manages a clone of mgit ----------
section "setup: mgit dogfooding — enrolled repo is a clone of mgit itself"
git clone -q "$ROOT" "$WS/mgit"
# The clone carries the .venv (gitignored won't be cloned); confirm it's mgit.
check "enrolled repo is the mgit source" grep -q 'name = "mgit"' "$WS/mgit/pyproject.toml"

FAKE_BIN="$SANDBOX/bin"
mkdir -p "$FAKE_BIN"
cat > "$FAKE_BIN/claude" <<FAKE
#!/bin/bash
cat "$SANDBOX/claude_payload.json"
FAKE
chmod +x "$FAKE_BIN/claude"
export PATH="$FAKE_BIN:$PATH"

cd "$WS"
check "mgit init (mgit auto-enrolled)" "$MGIT" init --no-interactive
$MGIT context > "$SANDBOX/ctx.json" 2>&1
json_check "workspace enrolled the mgit repo" \
    "'mgit' in obj['repos']" < "$SANDBOX/ctx.json"

# Point the skill distiller at the stub claude.
$PY - <<EOF
import tomllib, tomli_w
with open(".mgit/config.toml", "rb") as f:
    data = tomllib.load(f)
data.setdefault("skills", {})["claude_bin"] = "claude"
with open(".mgit/config.toml", "wb") as f:
    tomli_w.dump(data, f)
EOF

# ---------- record real development steering ----------
section "record steering into working memory"
check "feature start dev -m" "$MGIT" feature start dev -r mgit -m --no-carry
check "worktree materialized" test -d .mgit/worktrees/dev/mgit
check "decision note" "$MGIT" feature note \
    "always run .venv/bin/ruff check src tests before committing mgit" --type decision -f dev
check "convention note" "$MGIT" feature note \
    "skill evidence must be scrubbed before it reaches claude -p" --type decision -f dev
$MGIT feature brief > "$SANDBOX/brief.txt" 2>&1
check "brief shows recorded decision" grep -q "ruff check" "$SANDBOX/brief.txt"

# ---------- n=1 prose parks ----------
section "distill: first occurrence of a prose lesson parks (n=2 rule)"
set_candidates '[
 {"slug":"run-ruff-before-commit","kind":"durable-convention","scope_level":"workspace","paths":[],
  "title":"Run ruff before committing","trigger_description":"Run ruff check before every mgit commit",
  "anti_triggers":["docs-only changes"],"steps":["Run .venv/bin/ruff check src tests"],
  "verify_command":null,"evidence":[{"quote":"always run .venv/bin/ruff check src tests before committing mgit","kind":"decision"}],
  "explicit_rule":false,"recurrence_of":null,"updates_existing_skill":null,"watched_paths":[]}]'
$MGIT skill distill > "$SANDBOX/d1.txt" 2>&1
check "first occurrence parks, not drafts" grep -q "run-ruff-before-commit: park" "$SANDBOX/d1.txt"
check "no draft yet" test ! -d .mgit/skills/drafts/run-ruff-before-commit
$MGIT skill doctor > "$SANDBOX/doc1.txt" 2>&1
check "doctor reports 1 parked" grep -q "parked (n=2 rule): 1" "$SANDBOX/doc1.txt"

# ---------- n=2 recurrence promotes to a draft ----------
section "distill: recurrence of the parked lesson promotes to a draft"
set_candidates '[
 {"slug":"run-ruff-before-commit","kind":"durable-convention","scope_level":"workspace","paths":[],
  "title":"Run ruff before committing","trigger_description":"Run ruff check before every mgit commit",
  "anti_triggers":["docs-only changes"],"steps":["Run .venv/bin/ruff check src tests"],
  "verify_command":null,"evidence":[{"quote":"always run .venv/bin/ruff check src tests before committing mgit","kind":"decision"}],
  "explicit_rule":false,"recurrence_of":"run-ruff-before-commit","updates_existing_skill":null,"watched_paths":[]}]'
$MGIT skill distill > "$SANDBOX/d2.txt" 2>&1
check "recurrence drafts the skill" grep -q "run-ruff-before-commit: draft" "$SANDBOX/d2.txt"
check "draft written" test -f .mgit/skills/drafts/run-ruff-before-commit/SKILL.md
$MGIT skill drafts > "$SANDBOX/drafts.txt" 2>&1
check "draft awaiting review" grep -q run-ruff-before-commit "$SANDBOX/drafts.txt"

# ---------- approve -> ambient brief ----------
section "approve promotes the skill into the ambient brief"
check "skill approve" "$MGIT" skill approve run-ruff-before-commit
$MGIT skill list --json > "$SANDBOX/list.json" 2>&1
json_check "skill is active" \
    "len(obj['data']['skills']) == 1 and obj['data']['skills'][0]['status'] == 'active'" < "$SANDBOX/list.json"
check "learned-skills block in ambient CLAUDE.md" \
    grep -q "Learned skills (mgit)" .mgit/worktrees/dev/CLAUDE.md
check "skill named in ambient AGENTS.md" \
    grep -q "run-ruff-before-commit" .mgit/worktrees/dev/AGENTS.md
check "SKILL.md scrubs nothing spurious but carries the evidence" \
    grep -q "ruff check" .mgit/skills/active/run-ruff-before-commit/SKILL.md

# ---------- reject -> tombstone ----------
section "reject tombstones a draft"
set_candidates '[
 {"slug":"delete-node-modules","kind":"durable-procedure","scope_level":"workspace","paths":[],
  "title":"Delete node_modules","trigger_description":"rm -rf node_modules when installs act up",
  "anti_triggers":["never"],"steps":["rm -rf node_modules"],"verify_command":null,
  "evidence":[{"quote":"skill evidence must be scrubbed before it reaches claude -p","kind":"decision"}],
  "explicit_rule":true,"recurrence_of":null,"updates_existing_skill":null,"watched_paths":[]}]'
$MGIT skill distill >/dev/null 2>&1
check "draft to reject exists" test -d .mgit/skills/drafts/delete-node-modules
check "skill reject" "$MGIT" skill reject delete-node-modules --reason "too destructive to auto-suggest"
check "rejected draft removed" test ! -d .mgit/skills/drafts/delete-node-modules
check "tombstone recorded" grep -q delete-node-modules .mgit/skills/tombstones.jsonl

# ---------- verify-command safety gate ----------
section "verify_command is never executed by the headless distiller"
MARKER="$SANDBOX/verify_ran"
set_candidates "[
 {\"slug\":\"typecheck-mgit\",\"kind\":\"durable-procedure\",\"scope_level\":\"workspace\",\"paths\":[],
  \"title\":\"Typecheck mgit\",\"trigger_description\":\"Run mypy on the mgit package\",
  \"anti_triggers\":[\"none\"],\"steps\":[\"Run mypy\"],\"verify_command\":\"touch $MARKER\",
  \"evidence\":[{\"quote\":\"always run .venv/bin/ruff check src tests before committing mgit\",\"kind\":\"decision\"}],
  \"explicit_rule\":false,\"recurrence_of\":null,\"updates_existing_skill\":null,\"watched_paths\":[]}]"
$MGIT skill distill >/dev/null 2>&1
check "verify_command drafted, not executed" test ! -f "$MARKER"
check "draft flagged verify-pending" bash -c \
    "grep -q 'verify_pending = true' .mgit/skills/drafts/typecheck-mgit/skill.toml"
# The command is LLM-authored shell: a headless --run-verify must refuse without
# consent, and must not have executed anything.
expect_exit "headless --run-verify refuses without --yes (exit 2)" 2 \
    "$MGIT" skill approve typecheck-mgit --run-verify
check "refused consent did not execute the command" test ! -f "$MARKER"
check "draft survives a refused consent" test -d .mgit/skills/drafts/typecheck-mgit

# --run-verify --yes runs it under the human's hand; a passing command stamps + approves.
check "approve --run-verify --yes executes the command" \
    "$MGIT" skill approve typecheck-mgit --run-verify --yes
check "verify actually ran on explicit review" test -f "$MARKER"

# A failing verify under --run-verify refuses approval (exit 2) and keeps the draft.
set_candidates '[
 {"slug":"bad-check","kind":"durable-procedure","scope_level":"workspace","paths":[],
  "title":"Bad check","trigger_description":"a check that fails","anti_triggers":["none"],
  "steps":["nope"],"verify_command":"false",
  "evidence":[{"quote":"skill evidence must be scrubbed before it reaches claude -p","kind":"decision"}],
  "explicit_rule":false,"recurrence_of":null,"updates_existing_skill":null,"watched_paths":[]}]'
$MGIT skill distill >/dev/null 2>&1
expect_exit "failing --run-verify refuses approval (exit 2)" 2 \
    "$MGIT" skill approve bad-check --run-verify --yes
check "refused draft stays a draft" test -d .mgit/skills/drafts/bad-check
check "refused skill is not active" test ! -d .mgit/skills/active/bad-check

# ---------- path-traversal / review-gate guard ----------
section "malicious slug can never bypass review"
set_candidates '[
 {"slug":"../../../../evil","kind":"durable-convention","scope_level":"workspace","paths":[],
  "title":"evil","trigger_description":"x","anti_triggers":["none"],"steps":["x"],
  "verify_command":null,"evidence":[{"quote":"skill evidence must be scrubbed before it reaches claude -p","kind":"decision"}],
  "explicit_rule":true,"recurrence_of":null,"updates_existing_skill":null,"watched_paths":[]}]'
$MGIT skill distill > "$SANDBOX/evil.txt" 2>&1
check "invalid slug dropped at the gate" grep -q "drop (invalid slug" "$SANDBOX/evil.txt"
check "nothing planted outside the workspace skills dir" test ! -e "$WS/evil"

# ---------- summary ----------
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
