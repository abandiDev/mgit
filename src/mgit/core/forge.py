"""Forge integration: pull/merge requests via gh (GitHub) or glab (GitLab).

The forge is auto-detected per repo from its recorded remote URL, so a mixed
workspace (some repos on GitHub, some on GitLab) publishes each repo through
the right CLI. Repos whose forge can't be determined are skipped with a hint,
never guessed.

All network interaction goes through the official CLIs (gh/glab), which own
authentication — mgit never touches tokens.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from mgit.utils.errors import MgitError


def detect_forge(url: str | None) -> str | None:
    """Map a remote URL to a forge kind ("github" | "gitlab") or None."""
    if not url:
        return None
    host = ""
    m = re.match(r"^[a-z+]+://([^/@]*@)?([^/:]+)", url)
    if m:
        host = m.group(2)
    else:
        m = re.match(r"^([^@]+@)?([^:/]+):", url)  # scp-like git@host:path
        if m:
            host = m.group(2)
    host = host.lower()
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


def _run(binary: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [binary, *args], cwd=cwd, capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise MgitError(
            f"'{binary}' is not installed or not in PATH "
            f"(required to publish this repo's forge)"
        )


class GitHubForge:
    """PRs via the gh CLI."""

    kind = "github"
    term = "PR"
    binary = "gh"

    def find_pr(self, cwd: Path, head: str) -> dict | None:
        """Find an open PR whose head is `head`; None if absent."""
        result = _run(self.binary, [
            "pr", "list", "--head", head, "--state", "open",
            "--json", "number,url,state", "--limit", "1",
        ], cwd)
        if result.returncode != 0:
            raise MgitError(f"gh pr list failed: {result.stderr.strip()}")
        try:
            prs = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            raise MgitError(f"gh pr list returned unparsable output")
        if not prs:
            return None
        pr = prs[0]
        return {"number": pr.get("number"), "url": pr.get("url"),
                "state": pr.get("state", "open")}

    def create_pr(self, cwd: Path, head: str, base: str, title: str,
                  body: str, draft: bool = False) -> dict:
        args = ["pr", "create", "--head", head, "--base", base,
                "--title", title, "--body", body]
        if draft:
            args.append("--draft")
        result = _run(self.binary, args, cwd)
        if result.returncode != 0:
            raise MgitError(f"gh pr create failed: {result.stderr.strip()}")
        url = result.stdout.strip().splitlines()[-1].strip()
        number = None
        m = re.search(r"/pull/(\d+)", url)
        if m:
            number = int(m.group(1))
        return {"number": number, "url": url}

    def update_body(self, cwd: Path, number: int, body: str) -> None:
        result = _run(self.binary, ["pr", "edit", str(number), "--body", body], cwd)
        if result.returncode != 0:
            raise MgitError(f"gh pr edit failed: {result.stderr.strip()}")

    def status(self, cwd: Path, head: str) -> dict | None:
        """CI/review/merge status of the PR whose head is `head`; None if no PR."""
        result = _run(self.binary, [
            "pr", "view", head,
            "--json", "number,url,state,mergeable,reviewDecision,statusCheckRollup",
        ], cwd)
        if result.returncode != 0:
            if "no pull requests found" in (result.stderr or "").lower():
                return None
            raise MgitError(f"gh pr view failed: {result.stderr.strip()}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise MgitError("gh pr view returned unparsable output")

        passed = failed = pending = 0
        for check in data.get("statusCheckRollup") or []:
            # CheckRun: status/conclusion; StatusContext: state
            state = (check.get("conclusion") or check.get("state")
                     or check.get("status") or "").upper()
            if state in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                passed += 1
            elif state in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT",
                           "ACTION_REQUIRED", "STARTUP_FAILURE"):
                failed += 1
            else:
                pending += 1
        return {
            "number": data.get("number"),
            "url": data.get("url"),
            "state": (data.get("state") or "").lower(),
            "mergeable": data.get("mergeable"),
            "review": data.get("reviewDecision"),
            "checks": {"passed": passed, "failed": failed, "pending": pending},
        }


class GitLabForge:
    """MRs via the glab CLI. Best-effort field mapping; degrades to unknowns."""

    kind = "gitlab"
    term = "MR"
    binary = "glab"

    def find_pr(self, cwd: Path, head: str) -> dict | None:
        result = _run(self.binary, [
            "mr", "list", "--source-branch", head, "--output", "json",
        ], cwd)
        if result.returncode != 0:
            raise MgitError(f"glab mr list failed: {result.stderr.strip()}")
        try:
            mrs = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        open_mrs = [m for m in mrs if m.get("state") in (None, "opened", "open")]
        if not open_mrs:
            return None
        mr = open_mrs[0]
        return {"number": mr.get("iid"), "url": mr.get("web_url"),
                "state": mr.get("state", "opened")}

    def create_pr(self, cwd: Path, head: str, base: str, title: str,
                  body: str, draft: bool = False) -> dict:
        args = ["mr", "create", "--source-branch", head, "--target-branch", base,
                "--title", title, "--description", body, "--yes"]
        if draft:
            args.append("--draft")
        result = _run(self.binary, args, cwd)
        if result.returncode != 0:
            raise MgitError(f"glab mr create failed: {result.stderr.strip()}")
        url = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if "://" in line:
                url = line
        number = None
        if url:
            m = re.search(r"/merge_requests/(\d+)", url)
            if m:
                number = int(m.group(1))
        return {"number": number, "url": url or ""}

    def update_body(self, cwd: Path, number: int, body: str) -> None:
        result = _run(self.binary, [
            "mr", "update", str(number), "--description", body,
        ], cwd)
        if result.returncode != 0:
            raise MgitError(f"glab mr update failed: {result.stderr.strip()}")

    def status(self, cwd: Path, head: str) -> dict | None:
        result = _run(self.binary, ["mr", "view", head, "--output", "json"], cwd)
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        pipeline = (data.get("pipeline") or {}).get("status", "")
        checks = {"passed": 0, "failed": 0, "pending": 0}
        if pipeline == "success":
            checks["passed"] = 1
        elif pipeline in ("failed", "canceled"):
            checks["failed"] = 1
        elif pipeline:
            checks["pending"] = 1
        return {
            "number": data.get("iid"),
            "url": data.get("web_url"),
            "state": data.get("state", ""),
            "mergeable": data.get("detailed_merge_status") == "mergeable" or None,
            "review": None,
            "checks": checks,
        }


_FORGES = {"github": GitHubForge(), "gitlab": GitLabForge()}


def get_forge(url: str | None):
    """Forge instance for a repo's remote URL, or None if undetectable."""
    kind = detect_forge(url)
    return _FORGES.get(kind) if kind else None
