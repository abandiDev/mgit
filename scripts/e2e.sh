#!/usr/bin/env bash
# End-to-end smoke test for mgit.
#
# Drives the real mgit binary through the full lifecycle — init, feature
# start/materialize, working memory, bulk commit, fork (incl. --carry-wip),
# checkpoint save/restore, publish, checks, upgrade, delete — against real
# git repos pushing to local bare origins. The only faked boundary is the
# forge CLI (gh/glab shims log invocations and return canned JSON), so the
# script needs no network and no credentials.
#
# Usage: scripts/e2e.sh            (uses .venv/bin/mgit, else mgit on PATH)
#        MGIT=/path/to/mgit scripts/e2e.sh
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
# Prefer the interpreter next to the mgit binary — it has mgit's deps
# (tomli_w) installed; fall back to system python3.
if [ -x "$(dirname "$MGIT")/python" ]; then
    PY="${PY:-$(dirname "$MGIT")/python}"
else
    PY="${PY:-python3}"
fi

SANDBOX="$(mktemp -d -t mgit-e2e-XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
WS="$SANDBOX/workspace"
mkdir -p "$WS"

PASS=0
FAIL=0
ok()   { PASS=$((PASS + 1)); printf '  ok    %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL  %s\n' "$1"; }
section() { printf '\n== %s ==\n' "$1"; }

# check <desc> <cmd...> — passes if the command exits 0
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then ok "$desc"; else fail "$desc"; fi
}
# expect_exit <desc> <want-code> <cmd...>
expect_exit() {
    local desc="$1" want="$2"; shift 2
    "$@" >/dev/null 2>&1
    local got=$?
    if [ "$got" -eq "$want" ]; then ok "$desc"; else fail "$desc (exit $got, wanted $want)"; fi
}
# json_check <desc> <python-expr reading obj from stdin JSON>
json_check() {
    local desc="$1" expr="$2"
    if $PY -c "import sys, json; obj = json.load(sys.stdin); sys.exit(0 if ($expr) else 1)" 2>/dev/null; then
        ok "$desc"
    else
        fail "$desc"
    fi
}

make_repo() { # make_repo <dir>
    mkdir -p "$1"
    git -C "$1" init -q
    git -C "$1" config user.email e2e@test
    git -C "$1" config user.name E2E
    echo "# $(basename "$1")" > "$1/README.md"
    git -C "$1" add -A
    git -C "$1" commit -qm init
}

commit_in() { # commit_in <dir> <file> <msg>
    echo "content-$RANDOM" > "$1/$2"
    git -C "$1" add -A
    git -C "$1" -c user.email=e2e@test -c user.name=E2E commit -qm "$3"
}

# ---------- sandbox setup ----------
section "setup: repos, bare origins, fake forge CLIs"

for r in svc-api web-ui; do
    make_repo "$WS/$r"
    git init -q --bare "$SANDBOX/$r-origin.git"
    git -C "$WS/$r" remote add origin "$SANDBOX/$r-origin.git"
done

FAKE_BIN="$SANDBOX/bin"
export FAKE_DIR="$SANDBOX/fakes"
export FAKE_LOG="$SANDBOX/forge.log"
mkdir -p "$FAKE_BIN" "$FAKE_DIR"
: > "$FAKE_LOG"
cat > "$FAKE_BIN/gh" <<'FAKE'
#!/bin/bash
echo "gh $*" >> "$FAKE_LOG"
case "$1 $2" in
  "pr list") cat "$FAKE_DIR/gh_pr_list.json" 2>/dev/null || echo "[]";;
  "pr create") echo "https://github.com/e2e/svc-api/pull/42";;
  "pr edit") :;;
  "pr view") cat "$FAKE_DIR/gh_pr_view.json" 2>/dev/null || { echo "no pull requests found" >&2; exit 1; };;
esac
FAKE
cat > "$FAKE_BIN/glab" <<'FAKE'
#!/bin/bash
echo "glab $*" >> "$FAKE_LOG"
case "$1 $2" in
  "mr list") cat "$FAKE_DIR/glab_mr_list.json" 2>/dev/null || echo "[]";;
  "mr create") echo "https://gitlab.com/e2e/web-ui/-/merge_requests/9";;
  "mr update") :;;
  "mr view") cat "$FAKE_DIR/glab_mr_view.json" 2>/dev/null || exit 1;;
esac
FAKE
chmod +x "$FAKE_BIN/gh" "$FAKE_BIN/glab"
export PATH="$FAKE_BIN:$PATH"
ok "sandbox at $SANDBOX"

cd "$WS"

# ---------- init ----------
section "init"
check "mgit init" "$MGIT" init --no-interactive
check "AGENT.md generated" test -f AGENT.md
check "AGENTS.md generated" test -f AGENTS.md
check "ritual documented in AGENT.md" grep -q "feature brief" AGENT.md

# Recorded URLs drive forge detection (push still goes to the local bares)
$PY - <<EOF
import tomllib, tomli_w
with open(".mgit/config.toml", "rb") as f:
    data = tomllib.load(f)
data["repos"]["svc-api"]["url"] = "https://github.com/e2e/svc-api.git"
data["repos"]["web-ui"]["url"] = "https://gitlab.com/e2e/web-ui.git"
with open(".mgit/config.toml", "wb") as f:
    tomli_w.dump(data, f)
EOF

# ---------- feature + memory ----------
section "feature start, working memory, ambient briefs"
check "feature start -m" "$MGIT" feature start pay-flow -r svc-api -r web-ui -m --no-carry
check "worktrees materialized" test -d .mgit/worktrees/pay-flow/svc-api
check "ambient CLAUDE.md generated" test -f .mgit/worktrees/pay-flow/CLAUDE.md
check "ambient AGENTS.md generated" test -f .mgit/worktrees/pay-flow/AGENTS.md

check "plan set" "$MGIT" feature plan --goal "Ship payment flow" --status "wiring" --next "api first" --next "then ui"
check "note decision" "$MGIT" feature note "tokens live in svc-api only" --type decision
"$MGIT" feature brief > "$SANDBOX/brief.txt" 2>&1
check "brief shows goal" grep -q "Ship payment flow" "$SANDBOX/brief.txt"
check "brief shows decision" grep -q "tokens live in svc-api only" "$SANDBOX/brief.txt"
check "ambient brief carries memory" grep -q "Ship payment flow" .mgit/worktrees/pay-flow/CLAUDE.md

# ---------- bulk commit auto-journal ----------
section "bulk commit + auto-journal"
echo "pay()" > .mgit/worktrees/pay-flow/svc-api/pay.py
echo "<Pay/>" > .mgit/worktrees/pay-flow/web-ui/pay.jsx
check "mgit commit -f ." "$MGIT" commit -m "payment skeleton" -f .
"$MGIT" feature log --json -n 50 > "$SANDBOX/log.json"
json_check "commit events journaled for both repos" \
    "len([e for e in obj['data']['entries'] if e.get('event') == 'commit']) == 2" < "$SANDBOX/log.json"

# ---------- checkpoint save/restore ----------
section "checkpoint: save, wreck, restore"
WT=.mgit/worktrees/pay-flow/svc-api
echo "wip" > "$WT/experiment.py"
check "checkpoint save" "$MGIT" checkpoint save --label "pre-refactor"
rm "$WT/experiment.py" "$WT/pay.py"
echo junk > "$WT/junk.txt"
check "checkpoint restore" "$MGIT" checkpoint restore cp-0001
check "WIP re-materialized" grep -q wip "$WT/experiment.py"
check "committed file restored" test -f "$WT/pay.py"
check "junk cleaned" test ! -f "$WT/junk.txt"
check "safety backup exists" "$MGIT" checkpoint show cp-0002

# ---------- fork ----------
section "fork with --carry-wip, pinned base"
PINNED=$(git -C "$WT" rev-parse HEAD)
check "fork --carry-wip" "$MGIT" feature fork pay-flow-v2 --carry-wip
commit_in "$WT" later.py "parent advances"
check "child materialize" "$MGIT" feature materialize svc-api
CHILD_WT=.mgit/worktrees/pay-flow-v2/svc-api
[ "$(git -C "$CHILD_WT" rev-parse HEAD)" = "$PINNED" ] \
    && ok "child branched at pinned SHA" || fail "child branched at pinned SHA"
check "child has no post-fork commit" test ! -f "$CHILD_WT/later.py"
check "parent WIP carried into child" grep -q wip "$CHILD_WT/experiment.py"
"$MGIT" feature tree > "$SANDBOX/tree.txt"
check "tree shows ancestry" grep -q "pay-flow-v2" "$SANDBOX/tree.txt"

# ---------- parallel-session isolation ----------
section "parallel sessions: cwd/env pin the feature"
# Active is pay-flow-v2 (fork switched it). A session inside the PARENT's
# worktree must write to the parent's memory, not the active feature's.
( cd "$WT" && "$MGIT" feature note "parent-session note" >/dev/null )
check "note from parent worktree lands in parent journal" \
    grep -q "parent-session note" .mgit/features/pay-flow/journal.jsonl
check "child journal untouched" \
    bash -c '! grep -q "parent-session note" .mgit/features/pay-flow-v2/journal.jsonl'
check "MGIT_FEATURE pins from workspace root" \
    env MGIT_FEATURE=pay-flow-v2 "$MGIT" feature note "env-pinned note"
check "env-pinned note in child journal" \
    grep -q "env-pinned note" .mgit/features/pay-flow-v2/journal.jsonl
"$MGIT" feature start side-quest -r svc-api --no-activate >/dev/null
[ "$(cat .mgit/active)" = "pay-flow-v2" ] \
    && ok "--no-activate leaves active pointer alone" \
    || fail "--no-activate leaves active pointer alone"
"$MGIT" feature delete side-quest >/dev/null

# ---------- publish + checks ----------
section "publish + checks (mixed forges)"
check "switch back to parent" "$MGIT" feature switch pay-flow
"$MGIT" feature publish --json > "$SANDBOX/publish.json" 2>&1
json_check "publish succeeded for both repos" \
    "obj['ok'] and obj['data']['summary']['succeeded'] == 2" < "$SANDBOX/publish.json"
json_check "PR URLs recorded per forge" \
    "'github.com' in obj['data']['prs']['svc-api'] and 'gitlab.com' in obj['data']['prs']['web-ui']" < "$SANDBOX/publish.json"
check "branch landed in svc-api origin" \
    git -C "$SANDBOX/svc-api-origin.git" rev-parse --verify refs/heads/pay-flow
check "branch landed in web-ui origin" \
    git -C "$SANDBOX/web-ui-origin.git" rev-parse --verify refs/heads/pay-flow
check "gh used for the github repo" grep -q "gh pr create" "$FAKE_LOG"
check "glab used for the gitlab repo" grep -q "glab mr create" "$FAKE_LOG"
check "cross-link body references sibling MR" grep -q "merge_requests/9" "$FAKE_LOG"

# idempotent re-publish: nothing new to push
: > "$FAKE_LOG"
"$MGIT" feature publish --json > "$SANDBOX/publish2.json" 2>&1
json_check "re-publish skips up-to-date repos" \
    "obj['data']['summary']['skipped'] == 2" < "$SANDBOX/publish2.json"

cat > "$FAKE_DIR/gh_pr_view.json" <<'EOF'
{"number": 42, "url": "https://github.com/e2e/svc-api/pull/42", "state": "OPEN",
 "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
 "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]}
EOF
cat > "$FAKE_DIR/glab_mr_view.json" <<'EOF'
{"iid": 9, "web_url": "https://gitlab.com/e2e/web-ui/-/merge_requests/9",
 "state": "opened", "pipeline": {"status": "success"}}
EOF
"$MGIT" feature checks --json > "$SANDBOX/checks.json" 2>&1
json_check "checks aggregate CI per repo" \
    "{r['repo']: r for r in obj['data']['prs']}['svc-api']['checks'] == {'passed': 1, 'failed': 0, 'pending': 1}" < "$SANDBOX/checks.json"
json_check "gitlab pipeline mapped" \
    "{r['repo']: r for r in obj['data']['prs']}['web-ui']['checks']['passed'] == 1" < "$SANDBOX/checks.json"

# ---------- agent contract ----------
section "agent contract: exit codes, non-TTY guard, context"
expect_exit "domain error exits 2" 2 "$MGIT" feature show nope
expect_exit "usage error exits 3" 3 "$MGIT" bogus-command
expect_exit "json error envelope exits 2" 2 "$MGIT" feature brief --json -f nope
# The refusal only applies when a worktree is about to be created — an
# already-materialized repo is a no-op and must exit 0 instead
"$MGIT" feature start guard-test -r svc-api --no-activate >/dev/null
echo dirty >> svc-api/README.md
expect_exit "headless carry prompt refuses (exit 2)" 2 \
    env MGIT_FEATURE=guard-test "$MGIT" feature materialize svc-api < /dev/null
expect_exit "already-materialized repo is a no-op (exit 0)" 0 \
    "$MGIT" feature materialize svc-api < /dev/null
git -C svc-api checkout -q README.md
env MGIT_FEATURE=guard-test "$MGIT" feature delete guard-test >/dev/null
"$MGIT" context -f . > "$SANDBOX/context.json"
json_check "deep context has live repo facts" \
    "obj['mgit_schema'] == 1 and obj['feature']['repo_facts'][0]['materialized'] is True" < "$SANDBOX/context.json"

# ---------- upgrade + cleanup ----------
section "upgrade + delete cleanup"
echo custom >> AGENT.md
check "mgit upgrade" "$MGIT" upgrade
check "AGENT.md.bak preserved" grep -q custom AGENT.md.bak
check "delete child feature" "$MGIT" feature delete pay-flow-v2
check "delete parent feature" "$MGIT" feature delete pay-flow
check "worktrees cleaned" test ! -d .mgit/worktrees/pay-flow
check "memory sidecar cleaned" test ! -d .mgit/features/pay-flow
check "checkpoints cleaned" test ! -d .mgit/checkpoints/pay-flow
REFS=$(git -C svc-api for-each-ref refs/mgit/ | wc -l)
[ "$REFS" -eq 0 ] && ok "all refs/mgit/* pruned" || fail "all refs/mgit/* pruned ($REFS left)"

# ---------- summary ----------
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
